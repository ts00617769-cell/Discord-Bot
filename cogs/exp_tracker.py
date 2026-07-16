import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import datetime
import unicodedata
from game_data import SERVER_MAP
import logging
import sqlite3
from .error_handler import parse_env_channel_ids, is_allowed_command_channel, parse_env_float

logger = logging.getLogger(__name__)

class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ALERT_CHANNEL_IDS = parse_env_channel_ids(env_name="EXP_ALERT_CHANNEL_ID")
        self.TRANSFER_ALERT_CHANNEL_IDS = parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")
        # 時速門檻（億）：優先讀 EXP_ALERT_THRESHOLD（原始經驗/小時），否則預設 4000 億
        threshold_raw = parse_env_float("EXP_ALERT_THRESHOLD", 4000 * 100_000_000)
        self.SPEED_LIMIT = threshold_raw / 100_000_000
        self.alerts_enabled = False 
        self.alert_count = 30
        self.alert_server = "全服"
        self.allowed_channel_ids = parse_env_channel_ids(env_name="ALLOWED_COMMAND_CHANNELS")
        # 注意：__init__ 是同步的，不能在這裡執行 await，所以資料庫初始化移到 cog_load

    # ✨ discord.py 提供的非同步初始化入口
    async def cog_load(self):
        await self.setup_database()
        self.auto_fetch_exp.start()

    def cog_unload(self):
        self.auto_fetch_exp.cancel()
        # 不需要再關閉 db_conn，因為 bot.py 關閉時會統一處理

    async def setup_database(self):
        """非同步初始化資料庫"""
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                level INTEGER,
                exp REAL
            )
        ''')
        
        # 檢查欄位是否存在 (使用 sqlite 原生 pragma 比較安全)
        async with self.bot.db.execute("PRAGMA table_info(exp_history)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if 'class_name' not in columns:
                await self.bot.db.execute("ALTER TABLE exp_history ADD COLUMN class_name TEXT DEFAULT '未知'")
            if 'subjugation_grade' not in columns:
                await self.bot.db.execute("ALTER TABLE exp_history ADD COLUMN subjugation_grade INTEGER DEFAULT 0")
                
        await self.bot.db.execute('CREATE INDEX IF NOT EXISTS idx_time_server ON exp_history(record_time, server_name)')
        await self.bot.db.execute('CREATE INDEX IF NOT EXISTS idx_player_server ON exp_history(player_name, server_name)')
        await self.bot.db.execute('CREATE INDEX IF NOT EXISTS idx_exp ON exp_history(exp)')
        await self.bot.db.execute('CREATE INDEX IF NOT EXISTS idx_class_exp_time ON exp_history(class_name, exp, record_time)')

        # ✨ 新增：用於記錄已發送過轉移警報的資料表，防止無限洗頻
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS transfer_alerts_log (
                old_name TEXT,
                old_server TEXT,
                new_name TEXT,
                new_server TEXT,
                alert_time TIMESTAMP,
                PRIMARY KEY (old_name, old_server, new_name, new_server)
            )
        ''')

        # 初始化玩家標記資料庫
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS member_registry (
                player_name TEXT PRIMARY KEY,
                original_identity TEXT
            )
        ''')

        await self.bot.db.commit()

    async def get_member_info(self, name):
        """改為非同步的讀取標記函數"""
        try:
            async with self.bot.db.execute('SELECT original_identity FROM member_registry WHERE player_name = ?', (name,)) as cursor:
                result = await cursor.fetchone()
                return f"({result[0]})" if result else ""
        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while fetching member info for '{name}': {e}")
            return ""
        except Exception as e:
            logger.error(f"Unexpected error while fetching member info for '{name}': {e}")
            return ""

    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://warsofprasia.beanfun.com",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }
        classes = [None, "abyssrevenant", "SolarSentinel", "MirageBlade", "IncenseArcher", "RuneScribe", "Enforcer"]

        async def fetch_class(c):
            payload = {"world_group_id": group_id, "world_id": world_id, "class": c}
            try:
                async with session.post(api_url, json=payload, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        json_data = await response.json()
                        return json_data.get("data", {}).get("gc", [])[:100]
            except asyncio.TimeoutError as e:
                logger.error(f"API timeout while fetching data for group {group_id}, world {world_id}, class {c}: {e}")
            except aiohttp.ClientError as e:
                logger.error(f"HTTP client error while fetching server data (class {c}): {e}")
            except ValueError as e:
                logger.error(f"JSON parsing error (class {c}): {e}")
            except Exception as e:
                logger.error(f"Unexpected error while fetching server data (class {c}): {e}")
            return []

        tasks = [fetch_class(c) for c in classes]
        results = await asyncio.gather(*tasks)

        unique_players = {}
        for res in results:
            for p in res:
                name = p.get('gc_name')
                if name and name not in unique_players:
                    unique_players[name] = p

        return list(unique_players.values())

    @tasks.loop(minutes=10.0)
    async def auto_fetch_exp(self):
        try:
            now_time = datetime.datetime.now().replace(second=0, microsecond=0)
            logger.info(f"[{now_time.strftime('%H:%M:%S')}] 哨兵出動：掃描全服前100名...")
            
            # 👇 直接移除 async with aiohttp.ClientSession() 的區塊，改用 self.bot.session
            for server_name, (g_id, w_id) in SERVER_MAP.items():
                try:
                    # 這裡傳入 self.bot.session
                    players = await self.fetch_server_data(self.bot.session, g_id, w_id) 

                    # 建立一個空列表來收集所有玩家的資料參數
                    insert_batch = []
                    for p in players:
                        # 擷取討伐等級
                        grade_val = (p.get("string_map") or {}).get("grade", "0")
                        try:
                            grade = int(grade_val)
                        except (ValueError, TypeError):
                            grade = 0

                        # 將每筆資料打包成 Tuple 並加入列表
                        insert_batch.append((
                            now_time,
                            server_name,
                            p.get('gc_name'),
                            p.get('gc_level'),
                            p.get('gc_exp', 0),
                            p.get('class_name', '未知'),
                            grade
                        ))

                    # 使用 executemany 進行一次性批次寫入 (效能大幅提升！)
                    await self.bot.db.executemany('''
                        INSERT INTO exp_history (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', insert_batch)
                    await self.bot.db.commit()
                except Exception as e:
                    logger.error(f"Error processing server {server_name}: {e}")
                    continue
                await asyncio.sleep(0.5)
            
            await self.check_for_alerts(now_time)
        except Exception as e:
            logger.error(f"🚨 [經驗值雷達] 發生未預期錯誤，已攔截以防崩潰：{e}")

    async def check_for_alerts(self, current_time):
        # 確保挑選出的時間是「已完成全服抓取」的時間 (避免抓取空隙導致誤判)
        sql_times = '''
            SELECT record_time
            FROM exp_history
            GROUP BY record_time
            HAVING COUNT(DISTINCT server_name) >= 4
            ORDER BY record_time DESC LIMIT 2
        '''
        async with self.bot.db.execute(sql_times) as cursor:
            times = await cursor.fetchall()
            
        if len(times) < 2: return
        time_now, time_prev = times[0][0], times[1][0]
        
        fmt = '%Y-%m-%d %H:%M:%S'
        t1 = datetime.datetime.strptime(time_now, fmt)
        t2 = datetime.datetime.strptime(time_prev, fmt)
        minutes_diff = (t1 - t2).total_seconds() / 60
        if minutes_diff <= 0: return

        if self.alerts_enabled:
            if self.alert_server == "全服":
                sql = '''
                    SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                    FROM exp_history t1
                    JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                    WHERE t1.record_time = ? AND t2.record_time = ?
                '''
                params = (time_now, time_prev)
            else:
                sql = '''
                    SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                    FROM exp_history t1
                    JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                    WHERE t1.record_time = ? AND t2.record_time = ? AND t1.server_name = ?
                '''
                params = (time_now, time_prev, self.alert_server)

            async with self.bot.db.execute(sql, params) as cursor:
                records = await cursor.fetchall()

            # 超速警報：先過濾達門檻者，再取 Top-N
            alert_list = []
            for name, server, level, exp_now, exp_prev in records:
                diff = exp_now - exp_prev
                if diff > 0:
                    hourly_speed = (diff / minutes_diff) * 60
                    speed_yi = hourly_speed / 100_000_000
                    if speed_yi >= self.SPEED_LIMIT:
                        alert_list.append({"name": name, "server": server, "level": level, "speed": speed_yi})

            if alert_list:
                alert_list.sort(key=lambda x: x['speed'], reverse=True)
                alert_list = alert_list[:self.alert_count]

                chunk_size = 50
                for i in range(0, len(alert_list), chunk_size):
                    chunk = alert_list[i:i+chunk_size]
                    embed = discord.Embed(
                        title=f"🚨 超速警報 ({self.alert_server} ≥{self.SPEED_LIMIT:,.0f}億 Top {self.alert_count})",
                        color=0xff0000
                    )

                    desc = ""
                    if i == 0:
                        desc += f"以下是時速超過 **{self.SPEED_LIMIT:,.0f} 億** 的前 {len(alert_list)} 名玩家：\n"
                    desc += "```yaml\n"

                    for p in chunk:
                        name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in p['name'])
                        name_padded = p['name'] + " " * max(0, 14 - name_width)
                        desc += f"[{p['server']}] {name_padded} | Lv.{p['level']} | 時速: {p['speed']:,.0f}億\n"
                    desc += "```"

                    embed.description = desc
                    if i + chunk_size >= len(alert_list):
                        embed.set_footer(text=f"掃描時間: {time_now} (監控週期: {int(minutes_diff)}min)")

                    for channel_id in self.ALERT_CHANNEL_IDS:
                        channel = self.bot.get_channel(channel_id)
                        if channel:
                            try:
                                await channel.send(embed=embed)
                            except Exception as e:
                                logger.error(f"Failed to send alert to channel {channel_id}: {e}")

        # ✨ 新增：自動轉服/改名偵測
        await self.check_for_transfers(time_now, time_prev)

    async def _get_potential_transfers(self, time_now, time_prev, exp_margin):
        """取出所有可能為轉服或改名的紀錄。

        配對條件收緊：
        1. 同名跨服（最可信），或
        2. 雙方職業皆為已知且完全相同（不可再以「未知」當萬用字元）
        舊資料以 subquery + MAX(record_time) 對齊，避免 GROUP BY 任意欄位。
        """
        sql = '''
            SELECT DISTINCT t_now.exp, t_now.player_name, t_now.server_name, t_now.level, t_now.class_name,
                            t_old.player_name, t_old.server_name, t_old.level, t_old.class_name,
                            t_old.exp, t_now.subjugation_grade
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
                t_now.player_name = t_old.player_name
                OR (
                    t_now.class_name = t_old.class_name
                    AND t_now.class_name IS NOT NULL
                    AND t_old.class_name IS NOT NULL
                    AND t_now.class_name NOT IN ('', 'None', '未知')
                    AND t_old.class_name NOT IN ('', 'None', '未知')
                )
            )
            WHERE t_now.record_time = ? AND t_now.exp > 1000000000000
              AND t_now.level >= t_old.level
              AND COALESCE(t_now.subjugation_grade, 0) >= COALESCE(t_old.subjugation_grade, 0)
              AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?)
              AND t_now.server_name != t_old.server_name
              AND NOT EXISTS (
                  SELECT 1 FROM exp_history t_check
                  WHERE t_check.record_time = ?
                    AND t_check.player_name = t_now.player_name
                    AND t_check.server_name = t_now.server_name
              )
        '''
        async with self.bot.db.execute(sql, (time_prev, time_prev, time_now, exp_margin, time_prev)) as cursor:
            return await cursor.fetchall()

    async def _send_transfer_alert(self, time_now, new_name, new_server, old_name, old_server, new_lvl, new_cls, new_sub_grade, status_str, exp_diff):
        """格式化轉服/改名訊息並發送到各個警報頻道"""
        if exp_diff == 0:
            diff_str = "+0.00% (完美吻合)"
        else:
            diff_str = f"+{(exp_diff/100000000):,.0f} 億 (轉移期間偷練)"

        embed = discord.Embed(
            title="【波拉西亞戰記】轉移/旅團變動警報",
            description=f"時間：{time_now}\n{'-'*30}\n"
                        f"✨ [即時轉移辨識] **{old_name}** ({old_server}) ➔\n"
                        f"**{new_name}** ({new_server})\n"
                        f"[狀態]: {status_str} | [EXP變動]: {diff_str}\n"
                        f"[屬性]: Lv.{new_lvl} / {new_cls} / 討伐 {new_sub_grade}",
            color=0xf1c40f
        )
        for channel_id in self.TRANSFER_ALERT_CHANNEL_IDS:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Failed to send transfer alert to channel {channel_id}: {e}")

    async def check_for_transfers(self, time_now, time_prev):
        """自動偵測轉服與改名 (V4 雙引擎升級版)"""
        try:
            EXP_MARGIN = 1.0 * 1000000000000
            transfer_records = await self._get_potential_transfers(time_now, time_prev, EXP_MARGIN)

            if not transfer_records:
                return

            # Fetch all known aliases to prioritize matches based on registry
            async with self.bot.db.execute('SELECT player_name, original_identity FROM member_registry') as cursor:
                registry_rows = await cursor.fetchall()

            alias_map = {}
            for row in registry_rows:
                name = row[0]
                identities = [i.strip() for i in row[1].split(',')] if row[1] else []
                alias_map[name] = set(identities)

            def is_known_alias(new_name, old_name):
                if old_name in alias_map.get(new_name, set()):
                    return True
                if new_name in alias_map.get(old_name, set()):
                    return True
                return False

            # Greedy 1-to-1 matching: Sort by multiple priorities
            # Priority 1: Exact Name Match
            # Priority 2: Known Alias Match
            # Priority 3: Exact Level Match
            # Priority 4: EXP diff (new_exp - old_exp) ascending
            transfer_records.sort(key=lambda x: (
                0 if x[1] == x[5] else 1,
                0 if is_known_alias(x[1], x[5]) else 1,
                0 if x[3] == x[7] else 1,
                x[0] - x[9]
            ))

            matched_old = set()
            matched_new = set()
            reported_pairs = set()

            for row in transfer_records:
                new_exp = row[0]
                new_name, new_server, new_lvl, new_cls = row[1], row[2], row[3], row[4]
                old_name, old_server, old_lvl, old_cls = row[5], row[6], row[7], row[8]
                old_exp = row[9]
                new_sub_grade = row[10]

                old_key = (old_name, old_server)
                new_key = (new_name, new_server)
                if old_key in matched_old or new_key in matched_new:
                    continue

                pair_key = (old_name, old_server, new_name, new_server)
                if pair_key in reported_pairs:
                    continue

                async with self.bot.db.execute('''
                    SELECT 1 FROM transfer_alerts_log
                    WHERE old_name = ? AND old_server = ? AND new_name = ? AND new_server = ?
                ''', pair_key) as check_log_cursor:
                    already_alerted = await check_log_cursor.fetchone()

                if already_alerted:
                    continue

                async with self.bot.db.execute('SELECT 1 FROM exp_history WHERE record_time = ? AND player_name = ? AND server_name = ?', (time_now, old_name, old_server)) as check_cursor:
                    is_old_still_active = await check_cursor.fetchone()

                if not is_old_still_active:
                    reported_pairs.add(pair_key)
                    matched_old.add(old_key)
                    matched_new.add(new_key)

                    await self.bot.db.execute('''
                        INSERT INTO transfer_alerts_log (old_name, old_server, new_name, new_server, alert_time)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (old_name, old_server, new_name, new_server, time_now))
                    await self.bot.db.commit()

                    status_str = "跨服轉移並改名" if new_name != old_name else "跨服轉移"
                    exp_diff = new_exp - old_exp

                    await self._send_transfer_alert(
                        time_now, new_name, new_server, old_name, old_server,
                        new_lvl, new_cls, new_sub_grade, status_str, exp_diff
                    )

        except Exception as e:
            logger.error(f"Error in automatic transfer check: {e}")

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()

        # 為了配合官方 10, 20, 30... 更新，我們將啟動時間對齊到下個 10 分鐘，並加上 30 秒的緩衝時間
        # (例如 10:00:30, 10:10:30...)
        now = datetime.datetime.now()
        # 算出下一個 10 分鐘的時間點
        next_run = now.replace(minute=(now.minute // 10) * 10, second=30, microsecond=0) + datetime.timedelta(minutes=10)
        # 如果當前時間才剛過 10 的倍數（例如 10:00:15），next_run 會算到 10:10:30。如果我們想要在當下的 10:00:30 執行，就可以減少 10 分鐘。
        # 這裡我們統一使用安全的等待方式
        if now.minute % 10 == 0 and now.second < 30:
             next_run = now.replace(second=30, microsecond=0)

        seconds_to_wait = (next_run - now).total_seconds()
        if seconds_to_wait > 0:
            logger.info(f"等待 {seconds_to_wait:.1f} 秒以對齊官方每 10 分鐘更新時間...")
            await asyncio.sleep(seconds_to_wait)

    @commands.command(name="警報", help="開啟或關閉自動測速警報 (用法: !警報 開 或 !警報 開 50 萊涅01 或 !警報 關)")
    async def toggle_alerts(self, ctx, *args):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        args_list = [arg for arg in args if arg.strip()]
        if not args_list:
            current_state = "🟢 開啟中" if self.alerts_enabled else "🔴 關閉中"
            return await ctx.send(f"目前警報狀態為：**{current_state}**\n👉 請輸入 `!警報 開 [數量] [伺服器]` 或 `!警報 關` 切換。")

        state = args_list.pop(0)
        if state in ["關", "off"]:
            self.alerts_enabled = False
            return await ctx.send("🔕 **【自動超速警報】已關閉！**")

        if state in ["開", "on"]:
            if len(args_list) > 0 and args_list[0].isdigit():
                temp_alert_count = int(args_list.pop(0))
            else:
                temp_alert_count = 30

            if temp_alert_count > 100: temp_alert_count = 100
            if temp_alert_count < 1: temp_alert_count = 10

            target_server = "".join(args_list) if args_list else "全服"
            if target_server not in ["全服", "全部", "global"] and target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

            # Validation passed, apply settings
            self.alerts_enabled = True
            self.alert_count = temp_alert_count
            self.alert_server = "全服" if target_server in ["全服", "全部", "global"] else target_server

            return await ctx.send(
                f"🚨 **【自動測速警報】已開啟！** "
                f"(設定: {self.alert_server}、門檻 ≥{self.SPEED_LIMIT:,.0f}億、前 {self.alert_count} 名)"
            )

        current_state = "🟢 開啟中" if self.alerts_enabled else "🔴 關閉中"
        await ctx.send(f"目前警報狀態為：**{current_state}**\n👉 請輸入 `!警報 開 [數量] [伺服器]` 或 `!警報 關` 切換。")

    @commands.command(name="測速", help="用法: !測速 全服 或 !測速 50 萊涅01")
    async def check_exp_speed(self, ctx, *args):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        # 移除原有的 self.setup_database() (因為已經在 cog_load 執行過了)
        count = 15 
        args_list = [arg for arg in args if arg.strip()]
        if len(args_list) > 0 and args_list[0].isdigit(): count = int(args_list.pop(0))
        if count > 100: count = 100
        if count < 1: count = 10

        target_server = "".join(args_list) if args_list else "全服"
        is_global = target_server in ["全服", "全部", "global"]

        if not is_global and target_server not in SERVER_MAP:
            valid_list = "、".join(SERVER_MAP.keys())
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

        processing_msg = await ctx.send(f"📡 正在調閱測速照相機，計算 {'全台服' if is_global else target_server} 練功時速 TOP {count}...")

        try:
            if is_global:
                # 針對全服測速，確保挑選出的時間是「已完成全服抓取」的時間 (避免抓取空隙)
                sql_times = '''
                    SELECT record_time
                    FROM exp_history
                    GROUP BY record_time
                    HAVING COUNT(DISTINCT server_name) >= 4
                    ORDER BY record_time DESC LIMIT 2
                '''
                params_times = []
            else:
                sql_times = 'SELECT DISTINCT record_time FROM exp_history WHERE server_name = ? ORDER BY record_time DESC LIMIT 2'
                params_times = [target_server]

            async with self.bot.db.execute(sql_times, tuple(params_times)) as cursor:
                times = await cursor.fetchall()

            if len(times) < 2: return await processing_msg.edit(content="⚠️ 樣本不足！請等待至少 10 分鐘。")

            time_now, time_prev = times[0][0], times[1][0]
            fmt = '%Y-%m-%d %H:%M:%S'
            t1, t2 = datetime.datetime.strptime(time_now, fmt), datetime.datetime.strptime(time_prev, fmt)
            minutes_diff = (t1 - t2).total_seconds() / 60
            if minutes_diff <= 0: minutes_diff = 10

            sql = '''
                SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                FROM exp_history t1
                JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
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
                    speed_data.append({"name": name, "server": server, "level": level, "speed": (diff / minutes_diff) * 60})
            
            # ... (排版邏輯保持原樣，沒有 SQL 變動) ...
            speed_data.sort(key=lambda x: x['speed'], reverse=True)
            top_list = speed_data[:count]

            if not top_list: return await processing_msg.edit(content="💤 大家都沒在練功，或資料抓取空隙中。")

            desc = f"**區間：{time_prev[11:16]} ➡️ {time_now[11:16]} (約 {int(minutes_diff)} 分鐘)**\n```yaml\n"
            embeds = []
            for idx, p in enumerate(top_list, 1):
                name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(p['name']))
                name_padded = str(p['name']) + " " * max(0, 14 - name_width)
                srv_info = f"({p['server']})" if is_global else ""
                line = f"{idx:02d}. {name_padded} | Lv.{p['level']:<2} | 時速:{p['speed']/100000000:>6.2f}億 {srv_info}\n"

                if len(desc) + len(line) > 1900:
                    desc += "```"
                    embeds.append(discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 (續)", description=desc, color=0x00ff00))
                    desc = "```yaml\n"
                desc += line

            if desc != "```yaml\n":
                desc += "```"
                embed = discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 TOP {count}", description=desc, color=0x00ff00)
                embed.set_footer(text="系統：全自動經驗值測速雷達")
                embeds.append(embed)

            await processing_msg.delete()
            for e in embeds: await ctx.send(embed=e)

        except sqlite3.DatabaseError as e:
            logger.error(f"Database error while checking exp speed: {e}")
            try:
                await processing_msg.edit(content="❌ 資料庫檔案損毀 (database disk image is malformed)，請聯絡管理員修復或刪除 prasia_data.db。")
            except discord.NotFound:
                pass
        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while checking exp speed: {e}")
            try:
                await processing_msg.edit(content="❌ 測速查詢逾時，請重試")
            except discord.NotFound:
                pass
        except Exception as e:
            logger.error(f"Error while checking exp speed: {e}")
            try:
                await processing_msg.edit(content=f"❌ 測速發生錯誤: {type(e).__name__}")
            except discord.NotFound:
                pass

    @commands.command(name="歷史排名", aliases=["查歷史", "歷史"], help="查詢過去的資料庫排名。用法: !歷史排名 100 2026-05-08 萊涅04 太陽監視者")
    async def historical_ranking(self, ctx, *args):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        count = 100
        tz = datetime.timezone(datetime.timedelta(hours=8))
        date_str = datetime.datetime.now(tz).strftime('%Y-%m-%d')
        target_server = "全服"
        target_class = None
        
        args_list = [arg for arg in args if arg.strip()]
        class_parts = []
        
        for arg in args_list:
            if arg.isdigit():
                count = int(arg)
            elif "-" in arg and len(arg) >= 8:
                date_str = arg
            elif arg in SERVER_MAP.keys() or arg in ["全服", "全部", "global"]:
                target_server = arg
            else:
                class_parts.append(arg)
                
        if class_parts:
            target_class = "".join(class_parts)

        is_global = target_server in ["全服", "全部", "global"]
        if not is_global and target_server not in SERVER_MAP:
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

        if count > 100: count = 100
        if count < 1: count = 10

        filter_msg = f" 【{target_class}】的" if target_class else " "
        processing_msg = await ctx.send(f"📊 正在潛入資料庫，調閱 `{date_str}` 的 {target_server}{filter_msg}歷史排行榜...")

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
                if target_class: err_msg = f"❌ 找不到符合條件的【{target_class}】玩家資料。"
                return await processing_msg.edit(content=err_msg)

            desc = f"**歷史快照日期：{date_str}**\n```yaml\n"
            embeds = []
            
            for idx, r in enumerate(rows, 1):
                name, server, level, exp, class_name = r
                
                # ✨ 這裡加上了重要的 await，防止備註顯示成亂碼物件
                tag = await self.get_member_info(name)
                
                display_name = f"{name}{tag}"
                name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in display_name)
                name_padded = display_name + " " * max(0, 16 - name_width)
                
                srv_info = f"({server})" if is_global else ""
                exp_zhao = exp / 1000000000000
                class_info = f"[{class_name}]"
                
                line = f"{idx:02d}. {name_padded} {class_info:<7} | Lv.{level:<2} | {exp_zhao:>7.2f} 兆 {srv_info}\n"
                
                if len(desc) + len(line) > 1900:
                    desc += "```"
                    embeds.append(discord.Embed(title=f"📜 {target_server}{filter_msg}歷史排名 (續)", description=desc, color=0x3498db))
                    desc = "```yaml\n"
                desc += line
                
            if desc != "```yaml\n":
                desc += "```"
                embed = discord.Embed(title=f"📜 {target_server}{filter_msg}歷史排名 TOP {len(rows)}", description=desc, color=0x3498db)
                embed.set_footer(text="系統：天眼資料庫歷史快照 (支援職業篩選)")
                embeds.append(embed)

            await processing_msg.delete()
            for e in embeds:
                await ctx.send(embed=e)

        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while fetching historical ranking: {e}")
            await processing_msg.edit(content="❌ 資料庫查詢逾時，請重試")
        except ValueError as e:
            logger.error(f"Invalid value in historical ranking query: {e}")
            await processing_msg.edit(content="❌ 資料格式錯誤，請檢查查詢參數")
        except Exception as e:
            logger.error(f"Error while fetching historical ranking: {e}")
            try:
                await processing_msg.edit(content=f"❌ 系統錯誤: {type(e).__name__}")
            except discord.NotFound:
                await ctx.send(f"❌ 系統錯誤: {type(e).__name__}")
    # ==========================================
    # 🕵️ 天眼追蹤系統：V4 雙引擎版 (絕對碰撞 + 無縫接軌)
    # ==========================================
    @commands.command(name="尋人回報", help="手動標記玩家前身身分。用法: !尋人回報 驕傲o 某某某 艾雲o 或 !尋人回報 驕傲o 清除")
    async def report_identity(self, ctx, *args):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        args_list = [arg for arg in args if arg.strip()]
        if len(args_list) < 2:
            return await ctx.send("❌ 參數不足！用法範例：`!尋人回報 驕傲o 某某某 艾雲o` 或 `!尋人回報 驕傲o 清除`")

        current_name = args_list[0]
        original_names = args_list[1:]

        if len(original_names) == 1 and original_names[0] == "清除":
            try:
                await self.bot.db.execute('DELETE FROM member_registry WHERE player_name = ?', (current_name,))
                await self.bot.db.commit()
                await ctx.send(f"✅ 已成功清除【{current_name}】的身分標記。")
            except Exception as e:
                logger.error(f"Error clearing member info for '{current_name}': {e}")
                await ctx.send(f"❌ 清除失敗: {e}")
        else:
            try:
                # 取得目前已有的標記
                async with self.bot.db.execute('SELECT original_identity FROM member_registry WHERE player_name = ?', (current_name,)) as cursor:
                    result = await cursor.fetchone()

                existing_identities = []
                if result and result[0]:
                    existing_identities = [x.strip() for x in result[0].split(',')]

                # 把新輸入的名字加進去，並排除重複
                added_names = []
                for name in original_names:
                    if name not in existing_identities:
                        existing_identities.append(name)
                        added_names.append(name)

                if not added_names:
                    return await ctx.send(f"⚠️ 你輸入的名字都已經標記過了。目前的標記為：({result[0]})")

                new_identity_str = ", ".join(existing_identities)

                await self.bot.db.execute('''
                    INSERT INTO member_registry (player_name, original_identity)
                    VALUES (?, ?)
                    ON CONFLICT(player_name) DO UPDATE SET original_identity=excluded.original_identity
                ''', (current_name, new_identity_str))
                await self.bot.db.commit()
                await ctx.send(f"✅ 已成功為【{current_name}】新增身分標記！目前累計的身分：【{new_identity_str}】")
            except Exception as e:
                logger.error(f"Error updating member info for '{current_name}': {e}")
                await ctx.send(f"❌ 標記失敗: {e}")

    def _is_unknown_class(self, cls_name):
        return cls_name in (None, '', 'None', '未知')

    async def _fetch_name_profiles(self, player_name):
        """以最新一筆紀錄的職業為準，取出該角色名在各服的生涯摘要。"""
        sql = '''
            SELECT e.player_name, e.server_name,
                   MAX(e.level), MIN(e.record_time), MAX(e.record_time),
                   MIN(e.exp), MAX(e.exp),
                   (SELECT e2.class_name FROM exp_history e2
                    WHERE e2.player_name = e.player_name AND e2.server_name = e.server_name
                    ORDER BY e2.record_time DESC LIMIT 1) AS class_name,
                   MAX(e.subjugation_grade)
            FROM exp_history e
            WHERE e.player_name = ?
            GROUP BY e.player_name, e.server_name
        '''
        async with self.bot.db.execute(sql, (player_name,)) as cursor:
            return await cursor.fetchall()

    async def _fetch_single_profile(self, player_name, server_name):
        sql = '''
            SELECT e.player_name, e.server_name,
                   MAX(e.level), MIN(e.record_time), MAX(e.record_time),
                   MIN(e.exp), MAX(e.exp),
                   (SELECT e2.class_name FROM exp_history e2
                    WHERE e2.player_name = e.player_name AND e2.server_name = e.server_name
                    ORDER BY e2.record_time DESC LIMIT 1) AS class_name,
                   MAX(e.subjugation_grade)
            FROM exp_history e
            WHERE e.player_name = ? AND e.server_name = ?
            GROUP BY e.player_name, e.server_name
        '''
        async with self.bot.db.execute(sql, (player_name, server_name)) as cursor:
            return await cursor.fetchone()

    async def _get_related_names(self, target_name):
        """從 member_registry 展開已知前身/後身別名。"""
        names = {target_name}
        async with self.bot.db.execute(
            'SELECT player_name, original_identity FROM member_registry'
        ) as cursor:
            rows = await cursor.fetchall()
        for player_name, identity in rows:
            aliases = [x.strip() for x in (identity or '').split(',') if x.strip()]
            group = {player_name, *aliases}
            if target_name in group:
                names.update(group)
        return names

    async def _recent_exp_anchors(self, player_name, server_name, limit=8):
        async with self.bot.db.execute('''
            SELECT DISTINCT exp FROM exp_history
            WHERE player_name = ? AND server_name = ?
            ORDER BY record_time DESC
            LIMIT ?
        ''', (player_name, server_name, limit)) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def _find_seamless_candidates(self, profile, exp_margin, window_days=30, limit=8):
        """尋找轉服/改名候選（含同服改名），回傳評分後的列表。"""
        t_name, t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub = profile
        unknown_cls = self._is_unknown_class(t_cls)
        candidates = []

        # 跨服 + 同服（改名）都查；職業已知時優先同職，未知時放寬但稍後靠分數過濾
        class_filter = '' if unknown_cls else 'AND class_name = ?'
        base_params_class = [] if unknown_cls else [t_cls]

        # 職業已知時 WHERE 已鎖定 class_name，MAX(class_name) 即可（避免相關子查詢）
        sql_fwd = f'''
            SELECT player_name, server_name,
                   MAX(level) AS lvl,
                   MAX(class_name) AS cls,
                   MIN(record_time) AS first_seen,
                   MAX(record_time) AS last_seen,
                   MIN(exp) AS min_exp,
                   MAX(exp) AS max_exp,
                   MAX(subjugation_grade) AS sub_grade
            FROM exp_history
            WHERE NOT (player_name = ? AND server_name = ?)
              {class_filter}
              AND exp BETWEEN ? AND ?
            GROUP BY player_name, server_name
            HAVING first_seen >= datetime(?, '-1 days')
               AND first_seen <= datetime(?, '+{int(window_days)} days')
               AND min_exp >= ? AND min_exp <= ?
               AND lvl >= ?
               AND (sub_grade >= ? OR sub_grade IS NULL OR ? IS NULL)
            ORDER BY (min_exp - ?) ASC
            LIMIT 40
        '''
        params_fwd = [
            t_name, t_server, *base_params_class,
            t_max_exp, t_max_exp + exp_margin,
            t_last, t_last, t_max_exp, t_max_exp + exp_margin,
            t_lvl, t_sub, t_sub, t_max_exp,
        ]
        def _gap_hours(anchor_str, point_str):
            try:
                fmt = '%Y-%m-%d %H:%M:%S'
                return abs(
                    (datetime.datetime.strptime(point_str, fmt) -
                     datetime.datetime.strptime(anchor_str, fmt)).total_seconds()
                ) / 3600
            except (TypeError, ValueError):
                return 9999.0

        def _confidence(c_cls, exp_diff, c_sub, gap_hours):
            # 高信心：同職 + 偷練 < 100億 + 討伐不矛盾 + 72 小時內銜接
            if (
                not unknown_cls and c_cls == t_cls
                and exp_diff < 1e10
                and (c_sub is None or t_sub is None or c_sub == t_sub)
                and gap_hours <= 72
            ):
                return "high"
            return "medium"

        async with self.bot.db.execute(sql_fwd, tuple(params_fwd)) as cursor:
            for row in await cursor.fetchall():
                c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
                exp_diff = c_min - t_max_exp
                same_server = c_server == t_server
                gap_hours = _gap_hours(t_last, c_first)
                score = exp_diff + gap_hours * 1e8  # 時間越近分數越好
                if not self._is_unknown_class(t_cls) and c_cls == t_cls:
                    score -= 5e11  # 同職加分
                if c_sub == t_sub:
                    score -= 1e11
                if c_lvl == t_lvl:
                    score -= 5e10
                if same_server:
                    score -= 2e10  # 同服改名略加分
                label = "✏️ 疑似同服改名" if same_server else "✈️ 疑似轉服/改名後"
                candidates.append({
                    "direction": "forward",
                    "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                    "first": c_first, "last": c_last, "exp_val": c_min, "sub_grade": c_sub,
                    "match_type": label,
                    "diff_text": f"空窗偷練 +{exp_diff/100000000:,.0f} 億",
                    "score": score,
                    "confidence": _confidence(c_cls, exp_diff, c_sub, gap_hours),
                })

        sql_back = f'''
            SELECT player_name, server_name,
                   MAX(level) AS lvl,
                   MAX(class_name) AS cls,
                   MIN(record_time) AS first_seen,
                   MAX(record_time) AS last_seen,
                   MIN(exp) AS min_exp,
                   MAX(exp) AS max_exp,
                   MAX(subjugation_grade) AS sub_grade
            FROM exp_history
            WHERE NOT (player_name = ? AND server_name = ?)
              {class_filter}
              AND exp BETWEEN ? AND ?
            GROUP BY player_name, server_name
            HAVING last_seen >= datetime(?, '-{int(window_days)} days')
               AND last_seen <= datetime(?, '+1 days')
               AND max_exp <= ? AND max_exp >= ?
               AND lvl <= ?
               AND (sub_grade <= ? OR sub_grade IS NULL OR ? IS NULL)
            ORDER BY (? - max_exp) ASC
            LIMIT 40
        '''
        params_back = [
            t_name, t_server, *base_params_class,
            max(0, t_min_exp - exp_margin), t_min_exp,
            t_first, t_first, t_min_exp, max(0, t_min_exp - exp_margin),
            t_lvl, t_sub, t_sub, t_min_exp,
        ]
        async with self.bot.db.execute(sql_back, tuple(params_back)) as cursor:
            for row in await cursor.fetchall():
                c_name, c_server, c_lvl, c_cls, c_first, c_last, c_min, c_max, c_sub = row
                exp_diff = t_min_exp - c_max
                same_server = c_server == t_server
                gap_hours = _gap_hours(t_first, c_last)
                score = exp_diff + gap_hours * 1e8
                if not self._is_unknown_class(t_cls) and c_cls == t_cls:
                    score -= 5e11
                if c_sub == t_sub:
                    score -= 1e11
                if c_lvl == t_lvl or c_lvl == t_lvl - 1:
                    score -= 5e10
                label = "✏️ 疑似同服改名前身" if same_server else "🔍 疑似前身"
                candidates.append({
                    "direction": "backward",
                    "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_cls,
                    "first": c_first, "last": c_last, "exp_val": c_max, "sub_grade": c_sub,
                    "match_type": label,
                    "diff_text": f"空窗偷練 +{exp_diff/100000000:,.0f} 億",
                    "score": score,
                    "confidence": _confidence(c_cls, exp_diff, c_sub, gap_hours),
                })

        candidates.sort(key=lambda x: x["score"])
        # 每個方向各保留前 N 名，避免單一假陽性吃掉整條軌跡
        forward = [c for c in candidates if c["direction"] == "forward"][:limit]
        backward = [c for c in candidates if c["direction"] == "backward"][:limit]
        return forward + backward

    @commands.command(name="尋人", help="利用經驗值特徵，精準追蹤改名或轉服的玩家。用法: !尋人 驕傲o")
    async def track_player(self, ctx, target_name: str):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        processing_msg = await ctx.send("🔍 啟動天眼雙引擎，正在進行【絕對碰撞】與【無縫接軌】掃描...")

        try:
            related_names = await self._get_related_names(target_name)
            target_profiles = []
            for name in related_names:
                target_profiles.extend(await self._fetch_name_profiles(name))

            if not target_profiles:
                return await processing_msg.edit(content=f"❌ 天眼系統找不到「{target_name}」的任何歷史紀錄。")

            EXP_MARGIN = 1.0 * 1000000000000
            timeline_entries = []
            soft_candidates = []
            seen_profiles = set()
            queue = []

            async def add_to_queue(p_name, p_server, m_type, d_text, e_val, profile=None, confidence="high"):
                if (p_name, p_server) in seen_profiles:
                    return
                seen_profiles.add((p_name, p_server))
                if profile is None:
                    profile = await self._fetch_single_profile(p_name, p_server)
                if not profile:
                    return
                queue.append({
                    "profile": profile,
                    "match_type": m_type,
                    "diff_text": d_text,
                    "exp_val": e_val,
                    "confidence": confidence,
                })

            for tp in target_profiles:
                label = "🎯 查詢目標" if tp[0] == target_name else "🏷️ 登錄別名"
                await add_to_queue(tp[0], tp[1], label, "", tp[6], profile=tp)

            bfs_limit = 30
            hops = 0

            while queue and hops < bfs_limit:
                current = queue.pop(0)
                profile = current["profile"]
                t_name, t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub_grade = profile

                timeline_entries.append({
                    "name": t_name, "server": t_server, "lvl": t_lvl, "cls": t_cls,
                    "first": t_first, "last": t_last,
                    "match_type": current["match_type"],
                    "diff_text": current["diff_text"],
                    "exp_val": current["exp_val"] or t_max_exp,
                    "sub_grade": t_sub_grade,
                    "confidence": current.get("confidence", "high"),
                })

                # 1) 絕對碰撞：用最近數個經驗錨點，不只 MIN/MAX
                anchors = await self._recent_exp_anchors(t_name, t_server, limit=8)
                if t_min_exp not in anchors:
                    anchors.append(t_min_exp)
                if t_max_exp not in anchors:
                    anchors.append(t_max_exp)

                if anchors:
                    placeholders = ",".join("?" for _ in anchors)
                    sql_exact = f'''
                        SELECT exp, player_name, server_name, MAX(level),
                               (SELECT e2.class_name FROM exp_history e2
                                WHERE e2.player_name = exp_history.player_name
                                  AND e2.server_name = exp_history.server_name
                                ORDER BY e2.record_time DESC LIMIT 1),
                               MIN(record_time), MAX(record_time), MAX(subjugation_grade)
                        FROM exp_history
                        WHERE exp IN ({placeholders})
                          AND NOT (player_name = ? AND server_name = ?)
                        GROUP BY exp, player_name, server_name
                        LIMIT 30
                    '''
                    async with self.bot.db.execute(sql_exact, tuple(anchors + [t_name, t_server])) as cursor:
                        exact_matches = await cursor.fetchall()

                    for exp, p_name, s_name, lvl, cls_name, first_seen, last_seen, sub_grade in exact_matches:
                        known = (not self._is_unknown_class(t_cls) and not self._is_unknown_class(cls_name))
                        if known and cls_name != t_cls:
                            continue
                        if t_sub_grade is not None and sub_grade is not None:
                            if first_seen >= t_last and sub_grade < t_sub_grade:
                                continue
                            if last_seen <= t_first and t_sub_grade < sub_grade:
                                continue
                        await add_to_queue(
                            p_name, s_name, "🔗 絕對經驗值碰撞", "EXP 完全一致", exp, confidence="high"
                        )

                # 2) 無縫接軌：30 天窗、每方向保留多名候選
                seamless = await self._find_seamless_candidates(
                    profile, EXP_MARGIN, window_days=30, limit=5
                )
                for cand in seamless:
                    soft_candidates.append(cand)
                    # 高信心才繼續 BFS 展開，避免圖譜爆炸
                    if cand["confidence"] == "high":
                        await add_to_queue(
                            cand["name"], cand["server"], cand["match_type"],
                            cand["diff_text"], cand["exp_val"], confidence="high"
                        )
                    elif cand["confidence"] == "medium" and hops == 0:
                        # 第一層中信心候選也納入軌跡顯示，但不繼續向外擴張
                        if (cand["name"], cand["server"]) not in seen_profiles:
                            seen_profiles.add((cand["name"], cand["server"]))
                            timeline_entries.append({
                                "name": cand["name"], "server": cand["server"],
                                "lvl": cand["lvl"], "cls": cand["cls"],
                                "first": cand["first"], "last": cand["last"],
                                "match_type": cand["match_type"],
                                "diff_text": cand["diff_text"],
                                "exp_val": cand["exp_val"],
                                "sub_grade": cand["sub_grade"],
                                "confidence": "medium",
                            })

                hops += 1

            unique_entries = []
            seen = set()
            for entry in timeline_entries:
                key = (entry["name"], entry["server"])
                if key not in seen:
                    seen.add(key)
                    unique_entries.append(entry)
            unique_entries.sort(key=lambda x: x["first"])

            only_self = len(unique_entries) <= 1 and all(x["name"] == target_name for x in unique_entries)
            target_last_exp = max(p[6] for p in target_profiles)

            if only_self:
                # 沒有高信心軌跡時，改顯示可疑候選（依分數）
                soft_unique = []
                soft_seen = set()
                for cand in sorted(soft_candidates, key=lambda x: x["score"]):
                    key = (cand["name"], cand["server"])
                    if key in soft_seen or key in seen:
                        continue
                    soft_seen.add(key)
                    soft_unique.append(cand)
                    if len(soft_unique) >= 8:
                        break

                if not soft_unique:
                    # 仍在榜上且經驗凍結時給更明確說明
                    still_active = any(p[0] == target_name for p in target_profiles)
                    tip = ""
                    if still_active:
                        tip = "\n（若目標仍持續出現在原服榜上，可能尚未轉服/改名。）"
                    return await processing_msg.edit(
                        content=(
                            f"⚠️ 目標最後紀錄為 {target_last_exp/1000000000000:.2f} 兆。\n"
                            f"雙引擎未找到符合條件的轉服/改名軌跡。{tip}\n"
                            f"提示：可用 `!尋人回報 {target_name} 前身名` 手動標記後再查。"
                        )
                    )

                desc = (
                    f"⚠️ **未找到高信心軌跡**，以下是「{target_name}」"
                    f"（最後 {target_last_exp/1000000000000:.2f} 兆）的**可疑候選**：\n\n```yaml\n"
                )
                embeds = []
                for idx, p in enumerate(soft_unique, 1):
                    exp_zhao = p["exp_val"] / 1_000_000_000_000
                    entry = (
                        f"{idx}. {p['name']} [{p['server']}]\n"
                        f"   ▶ {p['match_type']} ({p['confidence']})\n"
                        f"   ▶ 職業: {p['cls']} | Lv.{p['lvl']} | 討伐 {p.get('sub_grade', 0)}\n"
                        f"   ▶ 觀測: {p['first'][5:16]} ~ {p['last'][5:16]}\n"
                        f"   ▶ 關聯: {p['diff_text']} (特徵: {exp_zhao:,.2f}兆)\n\n"
                    )
                    if len(desc) + len(entry) > 3800:
                        desc += "```"
                        embeds.append(discord.Embed(
                            title=f"👁️ 天眼追蹤（可疑候選）- {target_name}",
                            description=desc, color=0xf39c12
                        ))
                        desc = "```yaml\n" + entry
                    else:
                        desc += entry
                if desc != "```yaml\n":
                    desc += "```"
                    embeds.append(discord.Embed(
                        title=f"👁️ 天眼追蹤（可疑候選）- {target_name}",
                        description=desc, color=0xf39c12
                    ))
                embeds[-1].set_footer(text="僅供參考：請用 !尋人回報 確認後可提升後續追蹤精度")
                await processing_msg.delete()
                for embed in embeds:
                    await ctx.send(embed=embed)
                return

            embeds = []
            desc = f"🚨 **啟動雙引擎掃描，成功捕捉「{target_name}」的軌跡！**\n\n```yaml\n"
            for idx, p in enumerate(unique_entries, 1):
                exp_zhao = p["exp_val"] / 1_000_000_000_000
                conf = p.get("confidence", "high")
                conf_tag = "" if conf == "high" else f" ({conf})"
                entry_text = f"{idx}. {p['name']} [{p['server']}]\n"
                entry_text += f"   ▶ {p['match_type']}{conf_tag}\n"
                entry_text += f"   ▶ 職業: {p['cls']} | Lv.{p['lvl']} | 討伐 {p.get('sub_grade', 0)}\n"
                entry_text += f"   ▶ 觀測: {p['first'][5:16]} ~ {p['last'][5:16]}\n"
                if p["diff_text"]:
                    entry_text += f"   ▶ 關聯: {p['diff_text']} (特徵: {exp_zhao:,.2f}兆)\n\n"
                else:
                    entry_text += f"   ▶ EXP : {exp_zhao:,.2f} 兆\n\n"

                if len(desc) + len(entry_text) > 3800:
                    desc += "```"
                    embeds.append(discord.Embed(
                        title=f"👁️ 天眼追蹤系統 (V5) - {target_name}",
                        description=desc, color=0xff0000
                    ))
                    desc = "```yaml\n" + entry_text
                else:
                    desc += entry_text

            if desc != "```yaml\n":
                desc += "```"
                embeds.append(discord.Embed(
                    title=f"👁️ 天眼追蹤系統 (V5) - {target_name}",
                    description=desc, color=0xff0000
                ))
            embeds[-1].set_footer(text="V5：30天窗・多候選・同服改名・別名聯查・可疑候選回退")

            await processing_msg.delete()
            for embed in embeds:
                await ctx.send(embed=embed)

        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while tracking player '{target_name}': {e}")
            try:
                await processing_msg.edit(content="❌ 天眼系統查詢逾時，請重試")
            except discord.NotFound:
                pass
        except KeyError as e:
            logger.error(f"Missing required field in player tracking: {e}")
            try:
                await processing_msg.edit(content="❌ 尋人系統資料欄位異常")
            except discord.NotFound:
                pass
        except Exception as e:
            logger.error(f"Error while tracking player '{target_name}': {e}")
            try:
                await processing_msg.edit(content=f"❌ 尋人系統發生錯誤: {type(e).__name__}")
            except discord.NotFound:
                pass
    # ==========================================
    # 👇 從這裡開始複製，把這整段貼到 track_player 的下面 👇
    # ==========================================
    @commands.command(name="轉服掃描", aliases=["移民清單", "抓包"], help="全服掃描近期利用轉服空窗期改名或移動的玩家")
    async def global_transfer_scan(self, ctx):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        processing_msg = await ctx.send("📡 正在進行全資料庫特徵碰撞比對，這可能需要幾秒鐘...")

        try:
            # 1. 找出所有「被兩個以上不同(玩家+伺服器)組合」共用的經驗值 (為了防誤判，僅限大於 1 兆的活躍玩家)
            async with self.bot.db.execute('''
                SELECT exp
                FROM exp_history
                WHERE exp > 1000000000000
                GROUP BY exp
                HAVING COUNT(DISTINCT server_name) > 1
                ORDER BY MAX(record_time) DESC
                LIMIT 10
            ''') as cursor:
                shared_exps = await cursor.fetchall()
            
            if not shared_exps:
                return await processing_msg.edit(content="💤 目前資料庫中沒有偵測到任何轉服或改名的活動軌跡。")

            exp_list = [row[0] for row in shared_exps]
            placeholders = ','.join('?' for _ in exp_list)
            
            # 2. 把這些有碰撞的經驗值詳細資料抓出來
            async with self.bot.db.execute(f'''
                SELECT exp, player_name, server_name, MIN(record_time), MAX(record_time)
                FROM exp_history
                WHERE exp IN ({placeholders})
                GROUP BY exp, player_name, server_name
                ORDER BY exp DESC, MIN(record_time) ASC
            ''', tuple(exp_list)) as cursor:
                records = await cursor.fetchall()

            # 3. 組合報表
            grouped_data = {}
            for exp, p_name, s_name, first_seen, last_seen in records:
                if exp not in grouped_data:
                    grouped_data[exp] = []
                grouped_data[exp].append({"name": p_name, "server": s_name, "first": first_seen, "last": last_seen})

            embeds = []
            desc = "🔍 **以下玩家被系統偵測到經驗值完全重疊：**\n\n"
            
            for exp, players in grouped_data.items():
                exp_zhao = exp / 1_000_000_000_000
                desc += f"🔗 **特徵碼：{exp_zhao:.3f} 兆**\n```yaml\n"
                
                for idx, p in enumerate(players, 1):
                    desc += f"{idx}. {p['name']} [{p['server']}]\n"
                    desc += f"   (觀測區間: {p['first'][5:16]} ~ {p['last'][5:16]})\n"
                desc += "```\n"

                # 分頁處理避免超過 Discord 字數限制
                if len(desc) > 1500:
                    embeds.append(discord.Embed(title="✈️ 全服轉服與改名掃描報告", description=desc, color=0xe67e22))
                    desc = ""
                    
            if desc:
                embeds.append(discord.Embed(title="✈️ 全服轉服與改名掃描報告", description=desc, color=0xe67e22))

            await processing_msg.delete()
            for e in embeds:
                e.set_footer(text="※ 原理：轉服期間經驗值會凍結，利用相同特徵追蹤移動軌跡。")
                await ctx.send(embed=e)

        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout during transfer scan: {e}")
            try:
                await processing_msg.edit(content="❌ 掃描查詢逾時，請重試")
            except discord.NotFound:
                pass
        except ValueError as e:
            logger.error(f"Invalid value during transfer scan: {e}")
            try:
                await processing_msg.edit(content="❌ 掃描資料格式錯誤")
            except discord.NotFound:
                pass
        except Exception as e:
            logger.error(f"Error during transfer scan: {e}")
            try:
                await processing_msg.edit(content=f"❌ 掃描發生錯誤: {type(e).__name__}")
            except discord.NotFound:
                pass
    @commands.command(name="測試轉移警報", help="發送測試訊息以確認轉移警報頻道設定是否正確。")
    async def test_transfer_alert(self, ctx):
        if not is_allowed_command_channel(ctx.channel.id, self.allowed_channel_ids):
            return
        channel_ids = self.TRANSFER_ALERT_CHANNEL_IDS
        if not channel_ids:
            return await ctx.send("❌ 系統尚未設定 `TRANSFER_ALERT_CHANNEL_ID` 環境變數，請確認 `.env` 檔案設定。")

        channels = []
        for cid in channel_ids:
            ch = self.bot.get_channel(cid)
            if ch:
                channels.append(ch)
            else:
                await ctx.send(f"⚠️ 找不到頻道 ID：`{cid}`。請確認 ID 是否正確，且機器人是否在該頻道擁有權限。")

        if not channels:
            return

        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        embed = discord.Embed(
            title="【波拉西亞戰記】轉移/旅團變動警報 (測試)",
            description=f"時間：{now}\n{'-'*30}\n"
                        f"✨ [即時轉移辨識] **測試玩家_舊** (測試伺服器_舊) ➔\n"
                        f"**測試玩家_新** (測試伺服器_新)\n"
                        f"[狀態]: 跨服轉移並改名 | [EXP變動]: +999 億 (轉移期間偷練)\n"
                        f"[屬性]: Lv.99 / 測試職業 / 討伐 99\n\n"
                        f"✅ **如果您看到此訊息，表示轉移警報頻道設定與權限皆正常運作中！**",
            color=0xf1c40f
        )

        success_count = 0
        for channel in channels:
            try:
                await channel.send(embed=embed)
                success_count += 1
            except discord.Forbidden:
                await ctx.send(f"❌ 機器人沒有權限在頻道 `{channel.id}` 發送訊息或嵌入連結 (Embed Links)。")
            except Exception as e:
                await ctx.send(f"❌ 在頻道 `{channel.id}` 發送警報時發生錯誤：{e}")

        if success_count > 0:
            await ctx.send(f"✅ 測試轉移警報已成功發送到 {success_count} 個頻道！請檢查警報頻道。")

    # ==========================================
    # 👆 複製到這裡結束 👆
    # ==========================================        

# setup 獨立在最外層
async def setup(bot):
    await bot.add_cog(ExpTracker(bot))
