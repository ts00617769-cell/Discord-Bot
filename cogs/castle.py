import discord
from discord.ext import commands
import aiohttp
import asyncio
from game_data import SERVER_MAP
import logging

from services.beanfun_http import get_beanfun_client
from services.text_display import pad_text

logger = logging.getLogger(__name__)

TERRITORY_API_URL = (
    "https://warsofprasia.beanfun.com/api/Records/PostLiveapiTerritoryByWorldId"
)


class CastleTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def fetch_territory_data(self, server_name, group_id, world_id):
        payload = {
            "world_group_id": group_id,
            "world_id": world_id,
            "territory_grade": None,
            "guild_id": None,
        }
        client = get_beanfun_client(self.bot)
        result = await client.post_json(TERRITORY_API_URL, payload)
        if not result.ok:
            logger.warning(f"Territory API failed for {server_name}: {result.error}")
            return []
        json_data = result.data or {}
        territories = json_data.get("data", {}).get("territory") or []
        for t in territories:
            t["real_server_name"] = server_name
        return territories

    @commands.command(name="稅收", help="例如: !稅收 全服, !稅收 20 萊涅01")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    async def get_castle_tax(self, ctx, *args):
        count = 15
        args_list = [arg for arg in args if arg.strip()]
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))

        if count > 50:
            count = 50
        if count < 1:
            count = 10

        target_server = "".join(args_list) if args_list else "全服"

        is_global = False
        if target_server in ["全服", "全部", ""]:
            is_global = True
            display_title = f"全伺服器 據點稅收 TOP {count}"
        else:
            if target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await ctx.send(
                    f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服"
                )

            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】 據點稅收 TOP {count}"

        processing_msg = await ctx.send(f"📡 啟動鑽石雷達，掃描 {display_title} 庫房中...")

        try:
            all_territories = []
            if is_global:
                tasks = [
                    self.fetch_territory_data(s_name, g_id, w_id)
                    for s_name, (g_id, w_id) in SERVER_MAP.items()
                ]
                results = await asyncio.gather(*tasks)
                for r in results:
                    all_territories.extend(r)
            else:
                territories = await self.fetch_territory_data(
                    target_server, target_group_id, target_world_id
                )
                all_territories.extend(territories)

            if not all_territories:
                return await processing_msg.edit(content="❌ 掃描失敗，該區目前沒有據點資料。")

            all_territories.sort(key=lambda x: x.get("tax_dia", 0), reverse=True)
            rich_territories = [t for t in all_territories if t.get("tax_dia", 0) > 0]
            if not rich_territories:
                rich_territories = all_territories
            top_list = rich_territories[:count]

            description = "```yaml\n"
            for idx, t in enumerate(top_list, 1):
                dia, ruby = t.get("tax_dia", 0), t.get("tax_ruby", 0)
                guild = str(t.get("guild_name") or "無人佔領")
                t_name = str(t.get("territory_name") or "未知據點")
                t_grade = str(t.get("territory_grade_name") or "據點")

                server_name_display = t.get("real_server_name", "未知")
                server_prefix = f"[{server_name_display}] " if is_global else ""

                full_name = f"{server_prefix}{t_name} ({t_grade})"

                name_padded = pad_text(full_name, 32)
                guild_padded = pad_text(f"佔領: {guild}", 20)

                line = (
                    f"{idx:02d}. {name_padded} | {guild_padded} | "
                    f"💎鑽石: {dia:>6} | 🔴紅寶: {ruby:>6}\n"
                )

                if len(description) + len(line) > 1900:
                    description += "```"
                    embed = discord.Embed(
                        title=f"🏰 {display_title} (續)",
                        description=description,
                        color=0x00FFFF,
                    )
                    await ctx.send(embed=embed)
                    description = "```yaml\n"
                description += line

            description += "```"
            embed = discord.Embed(
                title=f"🏰 {display_title}", description=description, color=0x00FFFF
            )
            embed.set_footer(text="系統：O(1) 極速據點掃描器 (修正官方漏字Bug)")
            await processing_msg.delete()
            await ctx.send(embed=embed)

        except asyncio.TimeoutError:
            await processing_msg.edit(content="❌ 連線逾時：據點掃描花時過久，請重試")
        except aiohttp.ClientError as e:
            logger.error(f"Castle tax client error: {e}")
            await processing_msg.edit(content="❌ 網路連線失敗：無法連接到遊戲伺服器")
        except (ValueError, KeyError, TypeError) as e:
            logger.error(f"Castle tax parse error: {e}")
            await processing_msg.edit(content="❌ 資料解析錯誤：伺服器回傳格式異常")
        except discord.HTTPException as e:
            logger.error(f"Castle tax discord error: {e}")
            try:
                await processing_msg.edit(content="❌ 訊息發送失敗")
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(CastleTracker(bot))
