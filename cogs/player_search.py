"""天眼尋人 / 轉服掃描指令（匹配邏輯見 services.player_search_engine）。"""
import asyncio
import logging
import sqlite3
import time
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from db.connection import read_db
from db.schema import (
    backfill_player_profile_denorm,
    denorm_coverage_stats,
    rebuild_player_profiles,
)
from game_data import SERVER_MAP
from services import player_matching as match
from services.error_handler import allowed_channel, parse_env_channel_ids
from services.member_registry import clear_member_identity, upsert_alias_links
from services.player_search_db import PlayerSearchStore
from services.player_search_engine import (
    build_causal_scan_sections,
    fetch_records_for_shared_exps,
    fetch_shared_exps,
    group_scan_records_by_exp,
    parse_track_target,
    run_track_search,
)
from services.timeutil import now_naive_taipei

logger = logging.getLogger(__name__)

_SEARCH_RESULT_TTL_SEC = 180.0
_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}


def invalidate_search_cache() -> None:
    _SEARCH_CACHE.clear()


class PlayerSearch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = PlayerSearchStore(read_db(bot))

    @property
    def TRANSFER_ALERT_CHANNEL_IDS(self):
        return parse_env_channel_ids(env_name="TRANSFER_ALERT_CHANNEL_ID")

    def _refresh_store(self):
        self.store = PlayerSearchStore(read_db(self.bot))

    @commands.command(
        name="尋人回報",
        help="手動標記玩家前身身分（雙向）。用法: !尋人回報 驕傲o 某某某 艾雲o 或 !尋人回報 驕傲o 清除",
    )
    @allowed_channel()
    async def report_identity(self, ctx, *args):
        args_list = [arg for arg in args if arg.strip()]
        if len(args_list) < 2:
            return await ctx.send(
                "❌ 參數不足！用法範例：`!尋人回報 驕傲o 某某某 艾雲o` 或 `!尋人回報 驕傲o 清除`"
            )

        current_name = args_list[0]
        original_names = args_list[1:]

        if len(original_names) == 1 and original_names[0] == "清除":
            try:
                await clear_member_identity(self.bot.db, current_name)
                invalidate_search_cache()
                await ctx.send(f"✅ 已成功清除【{current_name}】的身分標記（含反向別名）。")
            except sqlite3.DatabaseError as e:
                logger.error(f"Error clearing member info for '{current_name}': {e}")
                await ctx.send("❌ 清除失敗（資料庫錯誤）")
            return

        try:
            before = None
            async with read_db(self.bot).execute(
                "SELECT original_identity FROM member_registry WHERE player_name = ?",
                (current_name,),
            ) as cursor:
                row = await cursor.fetchone()
                before = row[0] if row else None

            new_identity_str = await upsert_alias_links(
                self.bot.db, current_name, list(original_names)
            )
            if before == new_identity_str:
                return await ctx.send(
                    f"⚠️ 你輸入的名字都已經標記過了。目前的標記為：({new_identity_str})"
                )
            invalidate_search_cache()
            await ctx.send(
                f"✅ 已成功為【{current_name}】新增雙向身分標記！目前累計的身分：【{new_identity_str}】"
            )
        except sqlite3.DatabaseError as e:
            logger.error(f"Error updating member info for '{current_name}': {e}")
            await ctx.send("❌ 標記失敗（資料庫錯誤）")

    @commands.hybrid_command(
        name="尋人",
        help="利用經驗值特徵追蹤改名或轉服。用法: !尋人 驕傲o 或 !尋人 驕傲o 萊涅01",
    )
    @app_commands.describe(target_name="玩家名稱，可加伺服器（例：驕傲o 萊涅01）")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.user, wait=False)
    @allowed_channel()
    async def track_player(self, ctx: commands.Context, *, target_name: str):
        raw = (target_name or "").strip()
        name, server = parse_track_target(raw, set(SERVER_MAP.keys()))
        if not name:
            await ctx.send("❌ 請輸入玩家名稱。用法：`!尋人 小碎冰` 或 `!尋人 小碎冰 萊涅01`")
            return

        display = f"{name}" + (f" @{server}" if server else "")
        logger.info(
            f"!尋人 start user={ctx.author.id} channel={ctx.channel.id} "
            f"parent={getattr(ctx.channel, 'parent_id', None)} name={name!r} server={server!r}"
        )

        if ctx.message is not None:
            try:
                await ctx.message.add_reaction("🔍")
            except discord.HTTPException:
                pass

        try:
            if ctx.interaction is not None:
                processing_msg = await ctx.send(
                    f"🔍 **天眼啟動**，正在掃描「**{display}**」…\n"
                    f"⏳ 比對經驗特徵中，請稍候（通常數秒～數十秒）。"
                )
            else:
                processing_msg = await ctx.reply(
                    f"🔍 **天眼啟動**，正在掃描「**{display}**」…\n"
                    f"⏳ 比對經驗特徵中，請稍候（通常數秒～數十秒）。",
                    mention_author=False,
                )
        except discord.HTTPException as e:
            logger.error(f"!尋人 無法在頻道 {ctx.channel.id} 發送訊息: {e}")
            return

        try:
            async with ctx.typing():
                cache_key = f"{name.casefold()}|{server or ''}"
                cached = _SEARCH_CACHE.get(cache_key)
                if cached and (time.monotonic() - cached[0]) < _SEARCH_RESULT_TTL_SEC:
                    payload = cached[1]
                    if payload.get("kind") == "embeds":
                        await self._deliver_embeds(ctx, processing_msg, payload["embeds"])
                        return
                    if payload.get("kind") == "text":
                        return await processing_msg.edit(content=payload["content"])

                self._refresh_store()

                async def on_progress(step, limit, locked):
                    try:
                        await processing_msg.edit(
                            content=(
                                f"🔍 **天眼掃描中**「**{display}**」…\n"
                                f"⏳ 進度：第 {step}/{limit} 步"
                                f"（已鎖定 {locked} 筆軌跡）"
                            )
                        )
                    except discord.HTTPException:
                        pass

                result = await run_track_search(
                    self.store,
                    read_db(self.bot),
                    name,
                    server_name=server,
                    on_progress=on_progress,
                )

                if result.kind == "not_found":
                    return await processing_msg.edit(
                        content=f"❌ 天眼系統找不到「{display}」的任何歷史紀錄。"
                    )

                if result.kind == "soft":
                    embeds = self._build_track_embeds(
                        name,
                        list(result.unique_entries) + list(result.soft_unique),
                        header=(
                            f"⚠️ **未找到高信心軌跡**，以下是「{name}」的**可疑候選**：\n\n"
                        ),
                        title=f"👁️ 天眼追蹤（可疑候選）- {name}",
                        color=0xf39c12,
                        footer="僅供參考：請用 !尋人回報 確認後可提升後續追蹤精度",
                        show_confidence=True,
                    )
                    _SEARCH_CACHE[cache_key] = (
                        time.monotonic(),
                        {"kind": "embeds", "embeds": embeds},
                    )
                    await self._deliver_embeds(ctx, processing_msg, embeds)
                    return

                if result.kind == "no_link":
                    last_exp = result.target_last_exp or 0.0
                    content = (
                        f"⚠️ 目標最後紀錄為 {last_exp/1000000000000:.2f} 兆。\n"
                        f"雙引擎未找到符合條件的轉服/改名軌跡。{result.tip}\n"
                        f"提示：可用 `!尋人回報 {name} 前身名` 手動標記後再查。"
                    )
                    _SEARCH_CACHE[cache_key] = (
                        time.monotonic(),
                        {"kind": "text", "content": content},
                    )
                    return await processing_msg.edit(content=content)

                embeds = self._build_track_embeds(
                    name,
                    result.unique_entries,
                    header=f"🚨 **啟動雙引擎掃描，成功捕捉「{name}」的軌跡！**\n\n",
                    title=f"👁️ 天眼追蹤系統 (V6) - {name}",
                    color=0xff0000,
                    footer="V6：denorm stats・tiered margin・covering indexes",
                )
                _SEARCH_CACHE[cache_key] = (
                    time.monotonic(),
                    {"kind": "embeds", "embeds": embeds},
                )
                await self._deliver_embeds(ctx, processing_msg, embeds)

        except sqlite3.DatabaseError as e:
            logger.error(f"DB error tracking '{name}': {e}\n{traceback.format_exc()}")
            try:
                await processing_msg.edit(content="❌ 天眼系統資料庫錯誤")
            except discord.NotFound:
                pass
        except asyncio.TimeoutError:
            try:
                await processing_msg.edit(content="❌ 天眼系統查詢逾時，請重試")
            except discord.NotFound:
                pass
        except KeyError as e:
            logger.error(f"Missing field in player tracking: {e}")
            try:
                await processing_msg.edit(content="❌ 尋人系統資料欄位異常")
            except discord.NotFound:
                pass
        except discord.HTTPException as e:
            logger.error(f"!尋人 Discord 錯誤 '{name}': {e}")
        except (ValueError, TypeError) as e:
            logger.error(f"!尋人 資料錯誤 '{name}': {e}\n{traceback.format_exc()}")
            try:
                await processing_msg.edit(content=f"❌ 尋人系統資料異常: {type(e).__name__}")
            except discord.NotFound:
                pass

    def _build_track_embeds(
        self,
        target_name,
        entries,
        *,
        header,
        title,
        color,
        footer,
        show_confidence=False,
    ):
        embeds = []
        desc = f"{header}```yaml\n"
        for idx, p in enumerate(entries, 1):
            entry = match.format_track_entry(idx, p, show_confidence=show_confidence)
            if len(desc) + len(entry) > 3800:
                desc += "```"
                embeds.append(discord.Embed(title=title, description=desc, color=color))
                desc = "```yaml\n" + entry
            else:
                desc += entry
        if desc != "```yaml\n":
            desc += "```"
            embeds.append(discord.Embed(title=title, description=desc, color=color))
        if embeds:
            embeds[-1].set_footer(text=footer)
        return embeds

    async def _deliver_embeds(self, ctx, processing_msg, embeds):
        if not embeds:
            try:
                await processing_msg.edit(content="❌ 無結果可顯示。")
            except discord.HTTPException:
                pass
            return
        try:
            await processing_msg.edit(content=None, embed=embeds[0])
        except discord.HTTPException as e:
            logger.error(f"edit processing_msg failed: {e}")
            try:
                await ctx.send(embed=embeds[0])
            except discord.HTTPException:
                return
        for embed in embeds[1:]:
            try:
                await ctx.send(embed=embed)
            except discord.HTTPException as e:
                logger.error(f"send embed failed: {e}")
                break

    @track_player.error
    async def track_player_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            try:
                await ctx.send(
                    "❌ 請輸入玩家名稱。用法：`!尋人 小碎冰` 或 `!尋人 小碎冰 萊涅01`"
                )
            except discord.HTTPException:
                pass
            return

    @commands.command(
        name="轉服掃描",
        aliases=["移民清單", "抓包"],
        help="全服掃描近期利用轉服空窗期改名或移動的玩家",
    )
    @commands.cooldown(1, 30, commands.BucketType.guild)
    @commands.max_concurrency(1, commands.BucketType.default, wait=False)
    @allowed_channel()
    async def global_transfer_scan(self, ctx):
        processing_msg = await ctx.send("📡 正在進行全資料庫特徵碰撞比對，這可能需要幾秒鐘...")
        db = read_db(self.bot)

        try:
            shared_exps = await asyncio.wait_for(
                fetch_shared_exps(db),
                timeout=match.SEARCH_TIMEOUT_SEC,
            )
            if not shared_exps:
                return await processing_msg.edit(
                    content="💤 目前資料庫中沒有偵測到任何轉服或改名的活動軌跡。"
                )

            records = await asyncio.wait_for(
                fetch_records_for_shared_exps(db, shared_exps),
                timeout=match.SEARCH_TIMEOUT_SEC,
            )
            grouped = group_scan_records_by_exp(records)
            sections = build_causal_scan_sections(grouped)

            if not sections:
                return await processing_msg.edit(
                    content="💤 有共用 EXP，但沒有符合「先結束再開始、非雙活躍」的因果配對。"
                )

            embeds = []
            desc = "🔍 **以下玩家被系統偵測到經驗值完全重疊（含因果時間窗）：**\n\n"
            for section in sections:
                exp_zhao = section["exp"] / 1_000_000_000_000
                desc += f"🔗 **特徵碼：{exp_zhao:.3f} 兆**\n```yaml\n"
                for earlier, later, gap in section["pairs"]:
                    desc += (
                        f"• {earlier['name']} [{earlier['server']}] "
                        f"({earlier['first'][5:16]}~{earlier['last'][5:16]})\n"
                        f"  → {later['name']} [{later['server']}] "
                        f"({later['first'][5:16]}~{later['last'][5:16]})"
                        f" 空窗 {gap/24:.1f} 天\n"
                    )
                desc += "```\n"
                if len(desc) > 1500:
                    embeds.append(
                        discord.Embed(
                            title="✈️ 全服轉服與改名掃描報告",
                            description=desc,
                            color=0xe67e22,
                        )
                    )
                    desc = ""

            if desc:
                embeds.append(
                    discord.Embed(
                        title="✈️ 全服轉服與改名掃描報告",
                        description=desc,
                        color=0xe67e22,
                    )
                )

            for e in embeds:
                e.set_footer(text="※ 僅列出觀測窗不重疊且職業相容的配對。")
            await self._deliver_embeds(ctx, processing_msg, embeds)

        except sqlite3.DatabaseError as e:
            logger.error(f"DB error during transfer scan: {e}")
            try:
                await processing_msg.edit(content="❌ 掃描資料庫錯誤")
            except discord.NotFound:
                pass
        except asyncio.TimeoutError:
            try:
                await processing_msg.edit(content="❌ 掃描查詢逾時，請重試")
            except discord.NotFound:
                pass

    @commands.command(name="測試轉移警報", help="發送測試訊息以確認轉移警報頻道設定是否正確。")
    @allowed_channel("TRANSFER_ALERT_CHANNEL_ID")
    async def test_transfer_alert(self, ctx):
        channel_ids = self.TRANSFER_ALERT_CHANNEL_IDS
        if not channel_ids:
            return await ctx.send(
                "❌ 系統尚未設定 `TRANSFER_ALERT_CHANNEL_ID` 環境變數，請確認 `.env` 檔案設定。"
            )

        channels = []
        for cid in channel_ids:
            ch = self.bot.get_channel(cid)
            if ch:
                channels.append(ch)
            else:
                await ctx.send(f"⚠️ 找不到頻道 ID：`{cid}`。")

        if not channels:
            return

        now = now_naive_taipei().strftime("%Y-%m-%d %H:%M:%S")
        embed = discord.Embed(
            title="【波拉西亞戰記】轉移/旅團變動警報 (測試)",
            description=(
                f"時間：{now}\n{'-' * 30}\n"
                f"✨ [即時轉移辨識] **測試玩家_舊** (測試伺服器_舊) ➔\n"
                f"**測試玩家_新** (測試伺服器_新)\n"
                f"[狀態]: 跨服轉移並改名 | [EXP變動]: +999 億 (轉移期間偷練)\n"
                f"[屬性]: Lv.99 / 測試職業 / 討伐 99\n\n"
                f"✅ **如果您看到此訊息，表示轉移警報頻道設定與權限皆正常運作中！**"
            ),
            color=0xf1c40f,
        )

        success_count = 0
        for channel in channels:
            try:
                await channel.send(embed=embed)
                success_count += 1
            except discord.Forbidden:
                await ctx.send(f"❌ 機器人沒有權限在頻道 `{channel.id}` 發送訊息。")
            except discord.HTTPException as e:
                await ctx.send(f"❌ 在頻道 `{channel.id}` 發送警報時發生錯誤：{e}")

        if success_count > 0:
            await ctx.send(f"✅ 測試轉移警報已成功發送到 {success_count} 個頻道！")

    @commands.command(name="重建履歷", hidden=True, help="(擁有者) 增量或全量重建 player_profile denorm")
    @commands.is_owner()
    async def rebuild_profiles(self, ctx, mode: str = "增量"):
        mode = (mode or "增量").strip()
        try:
            if mode in ("全量", "full", "全部"):
                await ctx.send("⏳ 全量重建中（使用 bot 寫入連線，可能較久）…")
                n = await rebuild_player_profiles(self.bot.db)
                invalidate_search_cache()
                self._refresh_store()
                if hasattr(self.store, "invalidate_denorm_cache"):
                    self.store.invalidate_denorm_cache()
                await ctx.send(f"✅ 全量重建完成，共 {n:,} 筆 player_profile。")
            else:
                total, filled = await denorm_coverage_stats(read_db(self.bot))
                n = await backfill_player_profile_denorm(self.bot.db, batch_limit=2000)
                invalidate_search_cache()
                if hasattr(self.store, "invalidate_denorm_cache"):
                    self.store.invalidate_denorm_cache()
                self._refresh_store()
                await ctx.send(
                    f"✅ 增量回填 {n} 筆（先前覆蓋 {filled}/{total}）。"
                    f" 可再執行直到覆蓋完成；全量請 `!重建履歷 全量`。"
                )
        except (sqlite3.DatabaseError, OSError) as e:
            logger.error(f"rebuild profiles failed: {e}", exc_info=True)
            await ctx.send(f"❌ 重建失敗：{e}")


async def setup(bot):
    await bot.add_cog(PlayerSearch(bot))
