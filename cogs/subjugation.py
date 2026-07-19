import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

from game_data import SERVER_MAP
from services import error_handler
from services.ranking_api import get_ranking_client
from services.text_display import pad_text

logger = logging.getLogger(__name__)


class SubjugationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="討伐排名", aliases=["討伐"], help="查詢全服前 100 名討伐等級排行")
    @commands.cooldown(1, 20, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    async def get_subjugation_ranking(self, ctx):
        processing_msg = await ctx.send("📡 啟動全服討伐雷達掃描中，請稍候...")

        try:
            client = get_ranking_client(self.bot)
            all_players, failed_servers = await client.fetch_all_servers(
                SERVER_MAP, overall_only=True
            )

            if not all_players:
                return await processing_msg.edit(content="❌ 資料抓取異常，請確認官方 API 狀態。")

            partial_note = ""
            if failed_servers:
                partial_note = (
                    f"⚠️ 部分伺服器失敗（{len(failed_servers)}/"
                    f"{len(SERVER_MAP)}）：{'、'.join(failed_servers)}\n"
                )

            def sort_key(player):
                grade_val = (player.get("string_map") or {}).get("grade", "0")
                try:
                    grade = int(grade_val)
                except (ValueError, TypeError):
                    grade = 0
                return (grade, player.get("gc_exp", 0))

            all_players.sort(key=sort_key, reverse=True)
            top_100 = all_players[:100]

            description = "```yaml\n"
            embeds = []

            for i, p in enumerate(top_100, 1):
                name = str(p.get("gc_name") or "未知")
                world = str(p.get("world_name") or "未知")
                level = p.get("gc_level", "?")
                grade = (p.get("string_map") or {}).get("grade", "0")

                name_padded = pad_text(name, 14)
                world_padded = pad_text(world, 10)

                line = f"{i:03d}. [{world_padded}] {name_padded} | Lv.{level:<3} | 討伐 {grade}\n"

                if len(description) + len(line) > 1900:
                    description += "```"
                    embeds.append(
                        discord.Embed(
                            title="⚔️ 全服討伐前 100 名戰情報表",
                            description=description,
                            color=0xFFA500,
                        )
                    )
                    description = "```yaml\n"

                description += line

            if description != "```yaml\n":
                description += "```"
                embeds.append(
                    discord.Embed(
                        title="⚔️ 全服討伐前 100 名戰情報表",
                        description=description,
                        color=0xFFA500,
                    )
                )
                embeds[-1].set_footer(text="系統：共用 RankingClient 極速雷達")

            if failed_servers and embeds:
                embeds[-1].set_footer(
                    text=(
                        f"部分缺資料：{len(failed_servers)}/{len(SERVER_MAP)} 服失敗 | "
                        "系統：共用 RankingClient 極速雷達"
                    )
                )

            try:
                await processing_msg.edit(
                    content=partial_note or None, embed=embeds[0] if embeds else None
                )
            except discord.HTTPException:
                if embeds:
                    await ctx.send(content=partial_note or None, embed=embeds[0])
            for e in embeds[1:]:
                await ctx.send(embed=e)

        except asyncio.TimeoutError as e:
            await error_handler.handle_api_error(
                ctx, "連線逾時：抓取討伐排名花時過久，請重試", str(e)
            )
        except aiohttp.ClientError as e:
            await error_handler.handle_api_error(
                ctx, "網路連線失敗：無法連接到遊戲伺服器", str(e)
            )
        except (ValueError, KeyError, TypeError) as e:
            await error_handler.handle_api_error(
                ctx, "資料解析錯誤：伺服器回傳的資料格式異常", str(e)
            )
        except discord.HTTPException as e:
            error_handler.log_command_error(ctx, "討伐排名", e)
            await error_handler.handle_api_error(
                ctx, f"Discord 發送失敗：{type(e).__name__}", str(e)
            )


async def setup(bot):
    await bot.add_cog(SubjugationCog(bot))
