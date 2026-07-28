import asyncio
import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from game_data import SERVER_MAP
from services import error_handler
from services.command_args import parse_rank_args, parse_rank_filters
from services.error_handler import allowed_channel
from services.ranking_api import get_ranking_client, resolve_class_key

logger = logging.getLogger(__name__)


class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="排名",
        help="例如: !排名 幻影劍士, !排名 25 萊涅01 咒文刻印使",
    )
    @app_commands.describe(args="例如：25 萊涅01 或 幻影劍士")
    @commands.cooldown(1, 20, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    @allowed_channel()
    async def get_ranking(self, ctx: commands.Context, *, args: str = ""):
        parts = args.split() if args.strip() else []
        count_server, target_class = parse_rank_args(parts)
        count = count_server.count
        target_server = count_server.server
        is_global = count_server.is_global

        filter_msg = f"【{target_class}】" if target_class else " "
        if is_global:
            display_title = f"全伺服器{filter_msg}TOP {count}"
            target_group_id = target_world_id = None
        else:
            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】{filter_msg}TOP {count}"

        processing_msg = await ctx.send(f"🔍 正在潛入橘子主機，彙整 {display_title} 即時戰情...")

        try:
            client = get_ranking_client(self.bot)
            failed_servers: list[str] = []

            filters = parse_rank_filters(target_class)
            class_filter = filters.class_filter
            grade_filter = filters.grade_filter
            level_filter = filters.level_filter

            class_key = resolve_class_key(class_filter) if class_filter else None

            if is_global:
                fetch_kwargs = {}
                if class_key:
                    fetch_kwargs["classes"] = [class_key]
                all_players, failed_servers = await client.fetch_all_servers(
                    SERVER_MAP, **fetch_kwargs
                )
            else:
                assert target_group_id is not None and target_world_id is not None
                if class_key:
                    result = await client.fetch_server(
                        target_group_id,
                        target_world_id,
                        classes=[class_key],
                    )
                else:
                    result = await client.fetch_server(
                        target_group_id, target_world_id
                    )
                if not result.ok:
                    err = result.error or "未知錯誤"
                    return await processing_msg.edit(content=f"❌ 撈取失敗：{err}")
                all_players = result.players

            if not all_players:
                return await processing_msg.edit(content="❌ 撈取失敗，找不到資料。")

            partial_note = ""
            if failed_servers:
                partial_note = (
                    f"⚠️ 部分伺服器失敗（{len(failed_servers)}/"
                    f"{len(SERVER_MAP)}）：{'、'.join(failed_servers)}\n"
                )

            if target_class:
                filtered_players = []
                for p in all_players:
                    match = True
                    if class_filter and not class_key:
                        if class_filter not in str(p.get("class_name", "")):
                            match = False

                    if grade_filter > 0:
                        p_grade_val = (p.get("string_map") or {}).get("grade", "0")
                        try:
                            p_grade = int(p_grade_val)
                        except (ValueError, TypeError):
                            p_grade = 0
                        if p_grade < grade_filter:
                            match = False

                    if level_filter > 0:
                        try:
                            p_level = int(p.get("gc_level", 0))
                        except (ValueError, TypeError):
                            p_level = 0
                        if p_level < level_filter:
                            match = False

                    if match:
                        filtered_players.append(p)
                all_players = filtered_players

            if not all_players:
                return await processing_msg.edit(
                    content=f"❌ 找不到符合【{target_class}】條件的即時排名資料。"
                )

            all_players.sort(key=lambda x: x.get("gc_exp", 0), reverse=True)
            top_list = all_players[:count]

            description = "```yaml\n"
            for idx, p in enumerate(top_list, 1):
                exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                server_info = f"({p.get('world_name', '未知')})" if is_global else ""
                name = str(p.get("gc_name") or "未知")
                class_name = str(p.get("class_name", "未知"))
                grade_val = (p.get("string_map") or {}).get("grade", "0")
                level_str = p.get("gc_level", "?")

                line = (
                    f"{idx:02d}. [{name}] [{class_name}] Lv.{level_str} | "
                    f"討伐 {grade_val} {server_info}\n"
                )
                line += f"    ▶ 經驗值: {exp_zhao:,.2f} 兆\n"

                if len(description) + len(line) > 1900:
                    description += "```"
                    embed = discord.Embed(
                        title=f"🏆 {display_title} (續)",
                        description=description,
                        color=0xFFD700,
                    )
                    await ctx.send(embed=embed)
                    description = "```yaml\n"
                description += line

            description += "```"
            embed = discord.Embed(
                title=f"🏆 {display_title}", description=description, color=0xFFD700
            )
            footer = "單位：兆經驗值 | 系統：即時雷達過濾引擎"
            if failed_servers:
                footer = (
                    f"部分缺資料：{len(failed_servers)}/{len(SERVER_MAP)} 服失敗 | "
                    + footer
                )
            embed.set_footer(text=footer)

            await processing_msg.edit(
                content=partial_note or None, embed=embed
            )

        except asyncio.TimeoutError as e:
            await error_handler.handle_api_error(
                ctx, "連線逾時：抓取排名資料花時過久，請重試", str(e)
            )
        except aiohttp.ClientError as e:
            await error_handler.handle_api_error(
                ctx, "網路連線失敗：無法連接到遊戲伺服器", str(e)
            )
        except (ValueError, KeyError, TypeError) as e:
            await error_handler.handle_api_error(
                ctx, "資料解析錯誤：伺服器回傳的資料格式異常", str(e)
            )
        except discord.NotFound:
            logger.error("Processing message was deleted before we could edit it")
        except discord.HTTPException as e:
            error_handler.log_command_error(ctx, "排名", e)
            await error_handler.handle_api_error(
                ctx, f"Discord 發送失敗：{type(e).__name__}", str(e)
            )


async def setup(bot):
    await bot.add_cog(RankTracker(bot))
