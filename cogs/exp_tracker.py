import asyncio
import datetime
import logging
import sqlite3
import traceback
import unicodedata

import discord
from discord.ext import commands, tasks

from game_data import SERVER_MAP
from db import apply_migrations, ensure_search_indexes
from .error_handler import (
    min_complete_snapshot_servers,
    parse_env_channel_ids,
    parse_env_float,
    require_allowed_channel,
)
from .ranking_api import get_ranking_client

logger = logging.getLogger(__name__)


class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        threshold_raw = parse_env_float("EXP_ALERT_THRESHOLD", 4000 * 100_000_000)
        self.SPEED_LIMIT = threshold_raw / 100_000_000
        self.alerts_enabled = False
        self.alert_count = 30
        self.alert_server = "全服"
        self.alert_interval_minutes = 30

    @property
    def ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="EXP_ALERT_CHANNEL_ID")

    @property
    def TRANSFER_ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")

    async def cog_load(self):
        # schema 已在 bot.setup_hook 套用；此處再跑一次以支援 !reload
        await apply_migrations(self.bot.db)
        await self._load_alert_settings()
        self._index_task = asyncio.create_task(self._ensure_search_indexes())
        self.auto_fetch_exp.start()
        self._validate_task = asyncio.create_task(self._startup_validate_servers())

    def cog_unload(self):
        self.auto_fetch_exp.cancel()
        for attr in ("_index_task", "_validate_task"):
            task = getattr(self, attr, None)
            if task and not task.done():
                task.cancel()

    async def _load_alert_settings(self):
        """從 DB 還原警報開關，避免重啟後漏監控。"""
        try:
            async with self.bot.db.execute(
                "SELECT key, value FROM bot_settings WHERE key LIKE 'alert_%'"
            ) as cursor:
                rows = {k: v for k, v in await cursor.fetchall()}
            if "alert_enabled" in rows:
                self.alerts_enabled = rows["alert_enabled"] == "1"
            if "alert_count" in rows:
                try:
                    self.alert_count = max(1, min(100, int(rows["alert_count"])))
                except ValueError:
                    pass
            if "alert_server" in rows and rows["alert_server"]:
                self.alert_server = rows["alert_server"]
            logger.info(
                f"警報設定已載入: enabled={self.alerts_enabled} "
                f"count={self.alert_count} server={self.alert_server}"
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"載入警報設定失敗: {e}")

    async def _save_alert_settings(self):
        pairs = [
            ("alert_enabled", "1" if self.alerts_enabled else "0"),
            ("alert_count", str(self.alert_count)),
            ("alert_server", self.alert_server),
        ]
        await self.bot.db.executemany(
            '''
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''',
            pairs,
        )
        await self.bot.db.commit()

    async def _startup_validate_servers(self):
        try:
            for _ in range(60):
                if self.bot.is_closed():
                    return
                if self.bot.is_ready():
                    break
                await asyncio.sleep(1)
            if self.bot.is_closed():
                return
            client = get_ranking_client(self.bot)
            results = await client.validate_server_map(SERVER_MAP)
            dead = [name for name, r in results.items() if not r.get("ok")]
            if dead:
                logger.warning(
                    f"⚠️ SERVER_MAP 探活失敗（官網 Ranking API 無資料）: {', '.join(dead)}"
                )
            else:
                logger.info(f"✅ SERVER_MAP 探活通過，共 {len(results)} 服")
        except asyncio.CancelledError:
            return
        except (asyncio.TimeoutError, OSError) as e:
            logger.warning(f"啟動伺服器探活略過: {e}")

    async def _ensure_search_indexes(self):
        try:
            for _ in range(120):
                if self.bot.is_closed():
                    return
                if self.bot.is_ready():
                    break
                await asyncio.sleep(1)
            if self.bot.is_closed():
                return
            await ensure_search_indexes(self.bot.db)
        except asyncio.CancelledError:
            return
        except sqlite3.DatabaseError as e:
            logger.error(f"❌ 檢查/建立尋人索引時發生資料庫錯誤: {e}")

    async def get_member_info(self, name):
        try:
            async with self.bot.db.execute(
                "SELECT original_identity FROM member_registry WHERE player_name = ?", (name,)
            ) as cursor:
                result = await cursor.fetchone()
                return f"({result[0]})" if result else ""
        except sqlite3.DatabaseError as e:
            logger.error(f"DB error fetching member info for '{name}': {e}")
            return ""

    @tasks.loop(minutes=10.0)
    async def auto_fetch_exp(self):
        try:
            now_time = datetime.datetime.now().replace(second=0, microsecond=0)
            logger.info(f"[{now_time.strftime('%H:%M:%S')}] 哨兵出動：掃描全服前100名...")
            client = get_ranking_client(self.bot)

            servers_ok = []
            servers_failed = []

            for server_name, (g_id, w_id) in SERVER_MAP.items():
                try:
                    result = await client.fetch_server(g_id, w_id)
                    if not result.ok:
                        servers_failed.append(server_name)
                        logger.warning(
                            f"⚠️ 伺服器 {server_name} 抓取失敗，略過寫入: {result.error}"
                        )
                        continue

                    insert_batch = []
                    for p in result.players:
                        name = p.get("gc_name")
                        if not name:
                            continue
                        grade_val = (p.get("string_map") or {}).get("grade", "0")
                        try:
                            grade = int(grade_val)
                        except (ValueError, TypeError):
                            grade = 0
                        insert_batch.append((
                            now_time,
                            server_name,
                            name,
                            p.get("gc_level"),
                            p.get("gc_exp", 0),
                            p.get("class_name", "未知"),
                            grade,
                        ))

                    if insert_batch:
                        await self.bot.db.executemany('''
                            INSERT OR IGNORE INTO exp_history
                            (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', insert_batch)
                        await self.bot.db.commit()
                    servers_ok.append(server_name)
                except sqlite3.DatabaseError as e:
                    servers_failed.append(server_name)
                    logger.error(f"DB error processing server {server_name}: {e}")
                    continue
                except (asyncio.TimeoutError, OSError) as e:
                    servers_failed.append(server_name)
                    logger.error(f"Network/IO error processing server {server_name}: {e}")
                    continue
                await asyncio.sleep(0.5)

            snapshot_complete = (
                len(servers_failed) == 0
                and len(servers_ok) >= len(SERVER_MAP)
            )
            if not snapshot_complete:
                logger.warning(
                    f"⚠️ 本輪快照不完整 ok={len(servers_ok)}/{len(SERVER_MAP)} "
                    f"failed={servers_failed}；跳過轉服偵測"
                )
            await self.check_for_alerts(now_time, run_transfers=snapshot_complete)
        except sqlite3.DatabaseError as e:
            logger.error(f"🚨 [經驗值雷達] 資料庫錯誤：{e}\n{traceback.format_exc()}")
        except (asyncio.TimeoutError, OSError) as e:
            logger.error(f"🚨 [經驗值雷達] 網路/IO 錯誤：{e}\n{traceback.format_exc()}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"🚨 [經驗值雷達] 資料格式錯誤：{e}\n{traceback.format_exc()}")
        except discord.HTTPException as e:
            logger.error(f"🚨 [經驗值雷達] Discord 發送失敗：{e}")

    async def check_for_alerts(self, current_time, *, run_transfers: bool = True):
        min_servers = min_complete_snapshot_servers()
        sql_times = f'''
            SELECT record_time
            FROM exp_history
            GROUP BY record_time
            HAVING COUNT(DISTINCT server_name) >= {min_servers}
            ORDER BY record_time DESC LIMIT 10
        '''
        async with self.bot.db.execute(sql_times) as cursor:
            times = await cursor.fetchall()

        if len(times) < 2:
            return
        time_now, time_prev_scan = times[0][0], times[1][0]

        fmt = "%Y-%m-%d %H:%M:%S"
        t1 = datetime.datetime.strptime(time_now, fmt)

        should_alert = self.alerts_enabled
        if should_alert and isinstance(current_time, datetime.datetime):
            should_alert = (current_time.minute % self.alert_interval_minutes) == 0

        if should_alert:
            time_prev = time_prev_scan
            minutes_diff = (t1 - datetime.datetime.strptime(time_prev_scan, fmt)).total_seconds() / 60
            best_gap = abs(minutes_diff - self.alert_interval_minutes)
            for (rt,) in times[1:]:
                t2 = datetime.datetime.strptime(rt, fmt)
                gap = (t1 - t2).total_seconds() / 60
                if gap <= 0:
                    continue
                score = abs(gap - self.alert_interval_minutes)
                if score < best_gap:
                    best_gap = score
                    time_prev = rt
                    minutes_diff = gap

            if minutes_diff > 0:
                if self.alert_server == "全服":
                    sql = '''
                        SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                        FROM exp_history t1
                        JOIN exp_history t2
                          ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                        WHERE t1.record_time = ? AND t2.record_time = ?
                    '''
                    params = (time_now, time_prev)
                else:
                    sql = '''
                        SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                        FROM exp_history t1
                        JOIN exp_history t2
                          ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                        WHERE t1.record_time = ? AND t2.record_time = ? AND t1.server_name = ?
                    '''
                    params = (time_now, time_prev, self.alert_server)

                async with self.bot.db.execute(sql, params) as cursor:
                    records = await cursor.fetchall()

                alert_list = []
                for name, server, level, exp_now, exp_prev in records:
                    diff = exp_now - exp_prev
                    if diff > 0:
                        hourly_speed = (diff / minutes_diff) * 60
                        speed_yi = hourly_speed / 100_000_000
                        if speed_yi >= self.SPEED_LIMIT:
                            alert_list.append(
                                {"name": name, "server": server, "level": level, "speed": speed_yi}
                            )

                if alert_list:
                    alert_list.sort(key=lambda x: x["speed"], reverse=True)
                    alert_list = alert_list[: self.alert_count]
                    chunk_size = 50
                    for i in range(0, len(alert_list), chunk_size):
                        chunk = alert_list[i : i + chunk_size]
                        embed = discord.Embed(
                            title=f"🚨 超速警報 ({self.alert_server} ≥{self.SPEED_LIMIT:,.0f}億 Top {self.alert_count})",
                            color=0xff0000,
                        )
                        desc = ""
                        if i == 0:
                            desc += f"以下是時速超過 **{self.SPEED_LIMIT:,.0f} 億** 的前 {len(alert_list)} 名玩家：\n"
                        desc += "```yaml\n"
                        for p in chunk:
                            name_width = sum(
                                2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in p["name"]
                            )
                            name_padded = p["name"] + " " * max(0, 14 - name_width)
                            desc += f"[{p['server']}] {name_padded} | Lv.{p['level']} | 時速: {p['speed']:,.0f}億\n"
                        desc += "```"
                        embed.description = desc
                        if i + chunk_size >= len(alert_list):
                            embed.set_footer(
                                text=f"掃描時間: {time_now} (監控週期: {int(minutes_diff)}min)"
                            )
                        for channel_id in self.ALERT_CHANNEL_IDS:
                            channel = self.bot.get_channel(channel_id)
                            if channel:
                                try:
                                    await channel.send(embed=embed)
                                except discord.HTTPException as e:
                                    logger.error(f"Failed to send alert to {channel_id}: {e}")

        if run_transfers:
            await self.check_for_transfers(time_now, time_prev_scan, complete_times=times)
        else:
            logger.info("本輪略過轉服偵測（快照不完整）")

    async def _get_potential_transfers(self, time_now, time_prev, name_margin, class_margin):
        """轉服候選：同名跨服最優先；異名僅允許同職+同討伐+等級接近+較小經驗差。"""
        sql = '''
            SELECT DISTINCT t_now.exp, t_now.player_name, t_now.server_name, t_now.level, t_now.class_name,
                            t_old.player_name, t_old.server_name, t_old.level, t_old.class_name,
                            t_old.exp, t_now.subjugation_grade, t_old.subjugation_grade
            FROM exp_history t_now
            JOIN (
                SELECT e.player_name, e.server_name, e.class_name, e.level, e.subjugation_grade, e.exp, e.record_time
                FROM exp_history e
                INNER JOIN (
                    SELECT player_name, server_name, MAX(record_time) AS max_time
                    FROM exp_history
                    WHERE record_time <= ? AND record_time >= datetime(?, '-7 days')
                    GROUP BY player_name, server_name
                ) latest
                  ON e.player_name = latest.player_name
                 AND e.server_name = latest.server_name
                 AND e.record_time = latest.max_time
            ) t_old ON (
                (t_now.player_name = t_old.player_name
                 AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?))
                OR (
                    t_now.player_name != t_old.player_name
                    AND t_now.class_name = t_old.class_name
                    AND t_now.class_name IS NOT NULL
                    AND t_old.class_name IS NOT NULL
                    AND t_now.class_name NOT IN ('', 'None', '未知')
                    AND t_old.class_name NOT IN ('', 'None', '未知')
                    AND COALESCE(t_now.subjugation_grade, -1) = COALESCE(t_old.subjugation_grade, -2)
                    AND ABS(t_now.level - t_old.level) <= 1
                    AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?)
                )
            )
            WHERE t_now.record_time = ? AND t_now.exp > 1000000000000
              AND t_now.level >= t_old.level
              AND COALESCE(t_now.subjugation_grade, 0) >= COALESCE(t_old.subjugation_grade, 0)
              AND t_now.server_name != t_old.server_name
              AND NOT EXISTS (
                  SELECT 1 FROM exp_history t_check
                  WHERE t_check.record_time = ?
                    AND t_check.player_name = t_now.player_name
                    AND t_check.server_name = t_now.server_name
              )
        '''
        async with self.bot.db.execute(
            sql, (time_prev, time_prev, name_margin, class_margin, time_now, time_prev)
        ) as cursor:
            return await cursor.fetchall()

    async def _send_transfer_alert(
        self, time_now, new_name, new_server, old_name, old_server,
        new_lvl, new_cls, new_sub_grade, status_str, exp_diff,
    ) -> int:
        """發送轉服警報；回傳成功送達的頻道數。"""
        if exp_diff == 0:
            diff_str = "+0.00% (完美吻合)"
        else:
            diff_str = f"+{(exp_diff / 100000000):,.0f} 億 (轉移期間偷練)"

        embed = discord.Embed(
            title="【波拉西亞戰記】轉移/旅團變動警報",
            description=(
                f"時間：{time_now}\n{'-' * 30}\n"
                f"✨ [即時轉移辨識] **{old_name}** ({old_server}) ➔\n"
                f"**{new_name}** ({new_server})\n"
                f"[狀態]: {status_str} | [EXP變動]: {diff_str}\n"
                f"[屬性]: Lv.{new_lvl} / {new_cls} / 討伐 {new_sub_grade}"
            ),
            color=0xf1c40f,
        )
        success = 0
        for channel_id in self.TRANSFER_ALERT_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                    success += 1
                except discord.HTTPException as e:
                    logger.error(f"Failed to send transfer alert to {channel_id}: {e}")
        return success

    async def _player_present_at(self, record_time, player_name, server_name) -> bool:
        async with self.bot.db.execute(
            "SELECT 1 FROM exp_history WHERE record_time = ? AND player_name = ? AND server_name = ?",
            (record_time, player_name, server_name),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def check_for_transfers(self, time_now, time_prev, complete_times=None):
        try:
            # 同名 margin 收緊到 1000 億；異名（僅同職）100 億
            NAME_MARGIN = 1000 * 100000000
            CLASS_MARGIN = 100 * 100000000
            transfer_records = await self._get_potential_transfers(
                time_now, time_prev, NAME_MARGIN, CLASS_MARGIN
            )
            if not transfer_records:
                return

            # 連續缺席確認：需在最近兩個完整快照都找不到舊角
            miss_times = [time_now]
            if complete_times and len(complete_times) >= 2:
                miss_times = [complete_times[0][0], complete_times[1][0]]

            async with self.bot.db.execute(
                "SELECT player_name, original_identity FROM member_registry"
            ) as cursor:
                registry_rows = await cursor.fetchall()

            alias_map = {}
            for row in registry_rows:
                name = row[0]
                identities = [i.strip() for i in row[1].split(",")] if row[1] else []
                alias_map[name] = set(identities)

            def is_known_alias(new_name, old_name):
                return (
                    old_name in alias_map.get(new_name, set())
                    or new_name in alias_map.get(old_name, set())
                )

            transfer_records.sort(
                key=lambda x: (
                    0 if x[1] == x[5] else 1,
                    0 if is_known_alias(x[1], x[5]) else 1,
                    0 if x[3] == x[7] else 1,
                    0 if (x[10] is not None and x[11] is not None and x[10] == x[11]) else 1,
                    x[0] - x[9],
                )
            )

            matched_old = set()
            matched_new = set()
            reported_pairs = set()

            for row in transfer_records:
                new_exp = row[0]
                new_name, new_server, new_lvl, new_cls = row[1], row[2], row[3], row[4]
                old_name, old_server, old_lvl, old_cls = row[5], row[6], row[7], row[8]
                old_exp = row[9]
                new_sub_grade = row[10]

                # 異名且非登錄別名：必須同職（SQL 已保證）且討伐一致
                if new_name != old_name and not is_known_alias(new_name, old_name):
                    if row[10] is None or row[11] is None or row[10] != row[11]:
                        continue

                old_key = (old_name, old_server)
                new_key = (new_name, new_server)
                if old_key in matched_old or new_key in matched_new:
                    continue

                pair_key = (old_name, old_server, new_name, new_server)
                if pair_key in reported_pairs:
                    continue

                async with self.bot.db.execute(
                    '''
                    SELECT 1 FROM transfer_alerts_log
                    WHERE old_name = ? AND old_server = ? AND new_name = ? AND new_server = ?
                    ''',
                    pair_key,
                ) as check_log_cursor:
                    already_alerted = await check_log_cursor.fetchone()
                if already_alerted:
                    continue

                # 需連續兩個完整快照都缺席，降低掉出前100的假警報
                old_missing_all = True
                for miss_t in miss_times:
                    if await self._player_present_at(miss_t, old_name, old_server):
                        old_missing_all = False
                        break
                if not old_missing_all:
                    continue

                reported_pairs.add(pair_key)
                matched_old.add(old_key)
                matched_new.add(new_key)

                status_str = "跨服轉移並改名" if new_name != old_name else "跨服轉移"
                sent = await self._send_transfer_alert(
                    time_now, new_name, new_server, old_name, old_server,
                    new_lvl, new_cls, new_sub_grade, status_str, new_exp - old_exp,
                )
                if sent > 0:
                    await self.bot.db.execute(
                        '''
                        INSERT INTO transfer_alerts_log
                        (old_name, old_server, new_name, new_server, alert_time)
                        VALUES (?, ?, ?, ?, ?)
                        ''',
                        (old_name, old_server, new_name, new_server, time_now),
                    )
                    await self.bot.db.commit()
                else:
                    logger.warning(
                        f"轉服警報送出失敗，未寫入 dedupe："
                        f"{old_name}@{old_server} -> {new_name}@{new_server}"
                    )
                    # 送失敗時放開配對鎖，下輪可重試
                    reported_pairs.discard(pair_key)
                    matched_old.discard(old_key)
                    matched_new.discard(new_key)
        except sqlite3.DatabaseError as e:
            logger.error(f"DB error in transfer check: {e}\n{traceback.format_exc()}")
        except (ValueError, TypeError, KeyError) as e:
            logger.error(f"Data error in transfer check: {e}\n{traceback.format_exc()}")
        except discord.HTTPException as e:
            logger.error(f"Discord error in transfer check: {e}")

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now()
        next_run = now.replace(
            minute=(now.minute // 10) * 10, second=30, microsecond=0
        ) + datetime.timedelta(minutes=10)
        if now.minute % 10 == 0 and now.second < 30:
             next_run = now.replace(second=30, microsecond=0)
        seconds_to_wait = (next_run - now).total_seconds()
        if seconds_to_wait > 0:
            logger.info(f"等待 {seconds_to_wait:.1f} 秒以對齊官方每 10 分鐘更新時間...")
            await asyncio.sleep(seconds_to_wait)

    @commands.command(name="警報", help="開啟或關閉自動測速警報 (用法: !警報 開 或 !警報 開 50 萊涅01 或 !警報 關)")
    async def toggle_alerts(self, ctx, *args):
        if not await require_allowed_channel(ctx):
            return
        args_list = [arg for arg in args if arg.strip()]
        if not args_list:
            current_state = "🟢 開啟中" if self.alerts_enabled else "🔴 關閉中"
            return await ctx.send(
                f"目前警報狀態為：**{current_state}** "
                f"（{self.alert_server}、前 {self.alert_count} 名）\n"
                f"👉 請輸入 `!警報 開 [數量] [伺服器]` 或 `!警報 關` 切換。"
            )

        state = args_list.pop(0)
        if state in ["關", "off"]:
            self.alerts_enabled = False
            await self._save_alert_settings()
            return await ctx.send("🔕 **【自動超速警報】已關閉！**（設定已持久化）")

        if state in ["開", "on"]:
            if args_list and args_list[0].isdigit():
                temp_alert_count = int(args_list.pop(0))
            else:
                temp_alert_count = 30
            temp_alert_count = max(1, min(100, temp_alert_count))

            target_server = "".join(args_list) if args_list else "全服"
            if target_server not in ["全服", "全部", "global"] and target_server not in SERVER_MAP:
                return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

            self.alerts_enabled = True
            self.alert_count = temp_alert_count
            self.alert_server = (
                "全服" if target_server in ["全服", "全部", "global"] else target_server
            )
            await self._save_alert_settings()
            return await ctx.send(
                f"🚨 **【自動測速警報】已開啟！** "
                f"(設定: {self.alert_server}、門檻 ≥{self.SPEED_LIMIT:,.0f}億、前 {self.alert_count} 名、每 {self.alert_interval_minutes} 分鐘)\n"
                f"💾 設定已寫入資料庫，重啟後仍會保持開啟。"
            )

        current_state = "🟢 開啟中" if self.alerts_enabled else "🔴 關閉中"
        await ctx.send(
            f"目前警報狀態為：**{current_state}**\n👉 請輸入 `!警報 開 [數量] [伺服器]` 或 `!警報 關` 切換。"
        )

    @commands.command(name="測速", help="用法: !測速 全服 或 !測速 50 萊涅01")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    async def check_exp_speed(self, ctx, *args):
        if not await require_allowed_channel(ctx):
            return
        count = 15 
        args_list = [arg for arg in args if arg.strip()]
        if args_list and args_list[0].isdigit():
            count = int(args_list.pop(0))
        count = max(1, min(100, count))

        target_server = "".join(args_list) if args_list else "全服"
        is_global = target_server in ["全服", "全部", "global"]
        if not is_global and target_server not in SERVER_MAP:
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

        processing_msg = await ctx.send(
            f"📡 正在調閱測速照相機，計算 {'全台服' if is_global else target_server} 練功時速 TOP {count}..."
        )

        try:
            min_servers = min_complete_snapshot_servers()
            if is_global:
                sql_times = f'''
                    SELECT record_time
                    FROM exp_history
                    GROUP BY record_time
                    HAVING COUNT(DISTINCT server_name) >= {min_servers}
                    ORDER BY record_time DESC LIMIT 2
                '''
                params_times = []
            else:
                sql_times = (
                    "SELECT DISTINCT record_time FROM exp_history "
                    "WHERE server_name = ? ORDER BY record_time DESC LIMIT 2"
                )
                params_times = [target_server]

            async with self.bot.db.execute(sql_times, tuple(params_times)) as cursor:
                times = await cursor.fetchall()

            if len(times) < 2:
                return await processing_msg.edit(content="⚠️ 樣本不足！請等待至少 10 分鐘。")

            time_now, time_prev = times[0][0], times[1][0]
            fmt = "%Y-%m-%d %H:%M:%S"
            t1 = datetime.datetime.strptime(time_now, fmt)
            t2 = datetime.datetime.strptime(time_prev, fmt)
            minutes_diff = (t1 - t2).total_seconds() / 60
            if minutes_diff <= 0:
                minutes_diff = 10

            sql = '''
                SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                FROM exp_history t1
                JOIN exp_history t2
                  ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                WHERE t1.record_time = ? AND t2.record_time = ?
            '''
            params = [time_now, time_prev]
            if not is_global:
                sql += " AND t1.server_name = ?"
                params.append(target_server)

            async with self.bot.db.execute(sql, tuple(params)) as cursor:
                speed_records = await cursor.fetchall()

            speed_data = []
            for name, server, level, exp_now, exp_prev in speed_records:
                diff = exp_now - exp_prev
                if diff > 0:
                    speed_data.append(
                        {
                            "name": name,
                            "server": server,
                            "level": level,
                            "speed": (diff / minutes_diff) * 60,
                        }
                    )

            speed_data.sort(key=lambda x: x["speed"], reverse=True)
            top_list = speed_data[:count]
            if not top_list:
                return await processing_msg.edit(content="💤 大家都沒在練功，或資料抓取空隙中。")

            desc = f"**區間：{time_prev[11:16]} ➡️ {time_now[11:16]} (約 {int(minutes_diff)} 分鐘)**\n```yaml\n"
            embeds = []
            for idx, p in enumerate(top_list, 1):
                name_width = sum(
                    2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(p["name"])
                )
                name_padded = str(p["name"]) + " " * max(0, 14 - name_width)
                srv_info = f"({p['server']})" if is_global else ""
                line = (
                    f"{idx:02d}. {name_padded} | Lv.{p['level']:<2} | "
                    f"時速:{p['speed']/100000000:>6.2f}億 {srv_info}\n"
                )
                if len(desc) + len(line) > 1900:
                    desc += "```"
                    embeds.append(
                        discord.Embed(
                            title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 (續)",
                            description=desc,
                            color=0x00ff00,
                        )
                    )
                    desc = "```yaml\n"
                desc += line

            if desc != "```yaml\n":
                desc += "```"
                embeds.append(
                    discord.Embed(
                        title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 TOP {count}",
                        description=desc,
                        color=0x00ff00,
                    )
                )
                embeds[-1].set_footer(text="系統：全自動經驗值測速雷達")

            await processing_msg.delete()
            for e in embeds:
                await ctx.send(embed=e)

        except sqlite3.DatabaseError as e:
            logger.error(f"Database error while checking exp speed: {e}")
            try:
                await processing_msg.edit(content="❌ 資料庫錯誤，請聯絡管理員。")
            except discord.NotFound:
                pass
        except asyncio.TimeoutError:
            try:
                await processing_msg.edit(content="❌ 測速查詢逾時，請重試")
            except discord.NotFound:
                pass
        except (ValueError, TypeError) as e:
            logger.error(f"Value error in exp speed: {e}")
            try:
                await processing_msg.edit(content="❌ 測速資料格式異常")
            except discord.NotFound:
                pass

    @commands.command(
        name="歷史排名",
        aliases=["查歷史", "歷史"],
        help="查詢過去的資料庫排名。用法: !歷史排名 100 2026-05-08 萊涅04 太陽監視者",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def historical_ranking(self, ctx, *args):
        if not await require_allowed_channel(ctx):
            return
        count = 100
        tz = datetime.timezone(datetime.timedelta(hours=8))
        date_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")
        target_server = "全服"
        target_class = None
        
        args_list = [arg for arg in args if arg.strip()]
        class_parts = []
        for arg in args_list:
            if arg.isdigit():
                count = int(arg)
            elif "-" in arg and len(arg) >= 8:
                date_str = arg
            elif arg in SERVER_MAP or arg in ["全服", "全部", "global"]:
                target_server = arg
            else:
                class_parts.append(arg)
        if class_parts:
            target_class = "".join(class_parts)

        is_global = target_server in ["全服", "全部", "global"]
        if not is_global and target_server not in SERVER_MAP:
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

        count = max(1, min(100, count))
        filter_msg = f" 【{target_class}】的" if target_class else " "
        processing_msg = await ctx.send(
            f"📊 正在潛入資料庫，調閱 `{date_str}` 的 {target_server}{filter_msg}歷史排行榜..."
        )

        sql = '''
            SELECT h1.player_name, h1.server_name, h1.level, h1.exp, h1.class_name
            FROM exp_history h1
            INNER JOIN (
                SELECT player_name, server_name, MAX(record_time) as max_time
                FROM exp_history
                WHERE record_time LIKE ?
        '''
        params = [f"{date_str}%"]
        if not is_global:
            sql += " AND server_name = ?"
            params.append(target_server)
        if target_class:
            sql += " AND class_name LIKE ?"
            params.append(f"%{target_class}%")
        sql += '''
                GROUP BY player_name, server_name
            ) h2 ON h1.player_name = h2.player_name
                AND h1.server_name = h2.server_name
                AND h1.record_time = h2.max_time
            ORDER BY h1.exp DESC
            LIMIT ?
        '''
        params.append(count)

        try:
            async with self.bot.db.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                err_msg = f"❌ 資料庫中找不到 `{date_str}` 的排名資料。"
                if target_class:
                    err_msg = f"❌ 找不到符合條件的【{target_class}】玩家資料。"
                return await processing_msg.edit(content=err_msg)

            desc = f"**歷史快照日期：{date_str}**\n```yaml\n"
            embeds = []
            for idx, r in enumerate(rows, 1):
                name, server, level, exp, class_name = r
                tag = await self.get_member_info(name)
                display_name = f"{name}{tag}"
                name_width = sum(
                    2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in display_name
                )
                name_padded = display_name + " " * max(0, 16 - name_width)
                srv_info = f"({server})" if is_global else ""
                exp_zhao = exp / 1000000000000
                line = (
                    f"{idx:02d}. {name_padded} [{class_name}]{'':<1} | "
                    f"Lv.{level:<2} | {exp_zhao:>7.2f} 兆 {srv_info}\n"
                )
                if len(desc) + len(line) > 1900:
                    desc += "```"
                    embeds.append(
                        discord.Embed(
                            title=f"📜 {target_server}{filter_msg}歷史排名 (續)",
                            description=desc,
                            color=0x3498db,
                        )
                    )
                    desc = "```yaml\n"
                desc += line
                
            if desc != "```yaml\n":
                desc += "```"
                embeds.append(
                    discord.Embed(
                        title=f"📜 {target_server}{filter_msg}歷史排名 TOP {len(rows)}",
                        description=desc,
                        color=0x3498db,
                    )
                )
                embeds[-1].set_footer(text="系統：天眼資料庫歷史快照 (支援職業篩選)")

            await processing_msg.delete()
            for e in embeds:
                await ctx.send(embed=e)

        except sqlite3.DatabaseError as e:
            logger.error(f"DB error historical ranking: {e}")
            await processing_msg.edit(content="❌ 資料庫查詢失敗")
        except asyncio.TimeoutError:
            await processing_msg.edit(content="❌ 資料庫查詢逾時，請重試")
        except (ValueError, TypeError) as e:
            logger.error(f"Invalid value in historical ranking: {e}")
            await processing_msg.edit(content="❌ 資料格式錯誤，請檢查查詢參數")

    @commands.command(
        name="伺服器檢查",
        aliases=["檢查伺服器", "validate_servers"],
        help="對 SERVER_MAP 打官網 Ranking API 探活（維護用）",
    )
    @commands.is_owner()
    async def validate_servers(self, ctx):
        """資料來源與官網 https://warsofprasia.beanfun.com/ 即時戰況相同 API。"""
        msg = await ctx.send("🔎 正在對官網 Ranking API 探活 SERVER_MAP...")
        client = get_ranking_client(self.bot)
        results = await client.validate_server_map(SERVER_MAP)
        lines = []
        ok_n = 0
        for name, r in results.items():
            if r.get("ok"):
                ok_n += 1
                wn = r.get("world_name") or "?"
                sample = r.get("sample_name") or "?"
                lines.append(f"✅ {name} → API世界「{wn}」樣例:{sample}")
            else:
                lines.append(
                    f"❌ {name} → 無資料 ({r.get('group_id')}/{r.get('world_id')})"
                )
        body = "\n".join(lines)
        await msg.edit(
            content=(
                f"**伺服器探活結果**（{ok_n}/{len(results)} 通過）\n"
                f"來源：`PostLiveapiGCRanking`（與官網即時戰況同源）\n"
                f"```yaml\n{body}\n```"
            )
        )


async def setup(bot):
    await bot.add_cog(ExpTracker(bot))
