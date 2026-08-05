"""經驗測速／警報／伺服器探活指令（快照迴圈見 cogs.exp_tracker）。"""
from __future__ import annotations

import asyncio
import datetime
import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from db.connection import read_db
from game_data import SERVER_MAP
from services.command_args import parse_alert_toggle, parse_count_server
from services.error_handler import allowed_channel, min_complete_snapshot_servers
from services.exp_snapshots import fetch_recent_complete_snapshot_times
from services.exp_speed import collect_speed_ranking
from services.ranking_api import get_ranking_client
from services.text_display import pad_text

logger = logging.getLogger(__name__)


class ExpCommands(commands.Cog):
    """依賴 ExpTracker cog 的警報狀態；測速本身只讀 DB。"""

    def __init__(self, bot):
        self.bot = bot

    def _tracker(self):
        return self.bot.get_cog("ExpTracker")

    def _db(self):
        return read_db(self.bot)
    @commands.command(
        name="警報",
        help="開關指定伺服器、旅團的測速警報 (用法: !警報 開 50 萊涅01 旅團名稱)",
    )
    @allowed_channel()
    async def toggle_alerts(self, ctx, *args):
        tracker = self._tracker()
        if tracker is None:
            return await ctx.send("❌ 經驗雷達模組尚未載入。")

        state, cs = parse_alert_toggle(args)
        if state is None:
            current_state = "🟢 開啟中" if tracker.alerts_enabled else "🔴 關閉中"
            guild = tracker.alert_guild or "未設定"
            return await ctx.send(
                f"目前警報狀態為：**{current_state}** "
                f"（{tracker.alert_server}、旅團：{guild}、前 {tracker.alert_count} 名）\n"
                f"👉 請輸入 `!警報 開 [數量] [伺服器] [旅團名稱]` "
                f"或 `!警報 關` 切換。"
            )

        if state == "關":
            tracker.alerts_enabled = False
            await tracker._save_alert_settings()
            return await ctx.send("🔕 **【自動超速警報】已關閉！**（設定已持久化）")

        tracker.alerts_enabled = True
        tracker.alert_count = cs.count
        tracker.alert_server = cs.server
        tracker.alert_guild = cs.rest[0] if cs.rest else ""
        await tracker._save_alert_settings()
        return await ctx.send(
            f"🚨 **【自動測速警報】已開啟！** "
            f"(設定: {tracker.alert_server}、旅團：{tracker.alert_guild}、"
            f"門檻 ≥{tracker.SPEED_LIMIT:,.0f}億、"
            f"前 {tracker.alert_count} 名、每 {tracker.alert_interval_minutes} 分鐘輸出、"
            f"監控週期 {tracker.alert_speed_window_minutes} 分鐘)\n"
            f"💾 設定已寫入資料庫，重啟後仍會保持開啟。"
        )

    @commands.hybrid_command(name="測速", help="用法: !測速 全服 或 !測速 50 萊涅01")
    @app_commands.describe(args="例如：50 萊涅01 或 全服")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    @allowed_channel()
    async def check_exp_speed(self, ctx: commands.Context, *, args: str = ""):
        parts = args.split() if args.strip() else []
        cs = parse_count_server(parts, default_count=15, max_count=100)
        count = cs.count
        target_server = cs.server
        is_global = cs.is_global

        processing_msg = await ctx.send(
            f"📡 正在調閱測速照相機，計算 "
            f"{'全台服' if is_global else target_server} 練功時速 TOP {count}..."
        )

        try:
            min_servers = min_complete_snapshot_servers()
            if is_global:
                times = await fetch_recent_complete_snapshot_times(
                    self._db(), min_servers, limit=2
                )
            else:
                sql_times = (
                    "SELECT DISTINCT record_time FROM exp_history "
                    "WHERE server_name = ? ORDER BY record_time DESC LIMIT 2"
                )
                async with self._db().execute(sql_times, (target_server,)) as cursor:
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

            sql = """
                SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                FROM exp_history t1
                JOIN exp_history t2
                  ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                WHERE t1.record_time = ? AND t2.record_time = ?
            """
            params: list = [time_now, time_prev]
            if not is_global:
                sql += " AND t1.server_name = ?"
                params.append(target_server)

            async with self._db().execute(sql, tuple(params)) as cursor:
                speed_records = await cursor.fetchall()

            top_list = collect_speed_ranking(speed_records, minutes_diff)[:count]
            if not top_list:
                return await processing_msg.edit(content="💤 大家都沒在練功，或資料抓取空隙中。")

            desc = (
                f"**區間：{time_prev[11:16]} ➡️ {time_now[11:16]} "
                f"(約 {int(minutes_diff)} 分鐘)**\n```yaml\n"
            )
            embeds = []
            for idx, p in enumerate(top_list, 1):
                name_padded = pad_text(str(p["name"]), 14)
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
                            color=0x00FF00,
                        )
                    )
                    desc = "```yaml\n"
                desc += line

            if desc != "```yaml\n":
                desc += "```"
                embeds.append(
                    discord.Embed(
                        title=(
                            f"🏎️ {'全台服' if is_global else target_server} "
                            f"練功時速 TOP {count}"
                        ),
                        description=desc,
                        color=0x00FF00,
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
    await bot.add_cog(ExpCommands(bot))
