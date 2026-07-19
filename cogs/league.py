import discord
from discord.ext import commands
import aiohttp
import logging
import asyncio
from services import error_handler
from services.beanfun_http import get_beanfun_client
from services.text_display import pad_text

logger = logging.getLogger(__name__)

LEAGUE_API_URL = "https://warsofprasia.beanfun.com/api/UniverseLeague/Ranking"


class LeagueTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="聯賽", help="查詢宇宙聯賽分數。格式: !聯賽 [季] [回合] [級別] (預設: 3 3 1)")
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(2, commands.BucketType.default, wait=False)
    async def get_league_score(self, ctx, season: str = "3", round_num: str = "3", league_id: str = "1"):
        try:
            season_int = int(season)
            round_int = int(round_num)
            league_int = int(league_id)

            if not (1 <= season_int <= 10):
                await ctx.send("❌ 賽季必須是 1-10 的數字")
                return
            if not (1 <= round_int <= 20):
                await ctx.send("❌ 回合必須是 1-20 的數字")
                return
            if not (1 <= league_int <= 5):
                await ctx.send("❌ 級別必須是 1-5 的數字 (1:挑戰者 2:菁英 3:超級菁英 4:傳奇 5:不朽)")
                return

        except ValueError:
            await ctx.send("❌ 請輸入數字。用法: `!聯賽 [季] [回合] [級別]` 例: `!聯賽 3 3 1`")
            return

        hint_msg = (
            f"🛰️ **啟動宇宙聯賽觀測站**\n"
            f"🔍 您的查詢條件為：\n"
            f"> 🏆 **第 {season} 賽季**\n"
            f"> ⚔️ **第 {round_num} 回合**\n"
            f"> 🛡️ **第 {league_id} 組** (如挑戰者1)\n"
            f"正在潛入橘子主機撈取戰況，請稍候..."
        )
        processing_msg = await ctx.send(hint_msg)

        payload = {
            "season": f"UniverseLeague_TW_Live_Season0{season}",
            "roundId": f"Live_CrossRealmRound_UniverseLeague_TW_S0{season}_R{round_num}",
            "leagueId": str(league_id),
        }

        try:
            client = get_beanfun_client(self.bot)
            result = await client.post_json(LEAGUE_API_URL, payload)
            if not result.ok:
                return await processing_msg.edit(
                    content=f"❌ API 連線失敗 ({result.error})。請稍後再試。"
                )

            json_data = result.data or {}
            league_ranking = json_data.get("data", {}).get("league_ranking", [])
            if not league_ranking:
                return await processing_msg.edit(
                    content="❌ 找不到該季/回合的聯賽資料，可能尚未開打或參數輸入錯誤。"
                )

            match_list = league_ranking[0].get("match", [])
            if not match_list:
                return await processing_msg.edit(content="❌ 資料庫回傳為空，沒有對戰紀錄。")

            embeds = []

            for match in match_list:
                match_name = match.get("league_name", "未知賽區")
                teams = match.get("team", [])
                teams.sort(key=lambda x: x.get("team_score", 0), reverse=True)

                description = "```yaml\n"

                for team_idx, team in enumerate(teams, 1):
                    team_score = team.get("team_score", 0)
                    members = team.get("team_member", [])

                    description += f"🏆 陣營 {team_idx} (總分: {team_score})\n"
                    members.sort(key=lambda x: x.get("ranking_value", 0), reverse=True)

                    for member in members:
                        is_leader = member.get("team_leader_flag", False)
                        icon = "👑" if is_leader else "🔸"
                        world = member.get("world_name", "未知")
                        guild = member.get("guild_name", "未知公會")
                        score = member.get("ranking_value", 0)
                        territory = member.get("territory_name", "無據點")

                        guild_display = f"[{world}] {guild}"
                        guild_padded = pad_text(guild_display, 22)

                        description += f"   {icon} {guild_padded} | {score:>5}分 | {territory}\n"

                    description += "\n"

                description += "```"

                embed = discord.Embed(
                    title=f"🌌 宇宙聯賽 - {match_name}",
                    description=description,
                    color=0x9B59B6,
                )
                embed.set_footer(
                    text=f"查詢參數：S0{season} 賽季 | 第 {round_num} 回合 | 第 {league_id} 組"
                )
                embeds.append(embed)

            await processing_msg.delete()

            for emb in embeds:
                await ctx.send(embed=emb)

        except asyncio.TimeoutError as e:
            await error_handler.handle_api_error(
                ctx, "連線逾時：抓取聯賽資料花時過久，請重試", str(e)
            )
        except aiohttp.ClientError as e:
            await error_handler.handle_api_error(
                ctx, "網路連線失敗：無法連接到遊戲伺服器", str(e)
            )
        except ValueError as e:
            await error_handler.handle_api_error(
                ctx, "資料解析錯誤：伺服器回傳的資料格式異常", str(e)
            )
        except KeyError as e:
            await error_handler.handle_api_error(ctx, "模組發生錯誤：資料欄位異常", str(e))
        except discord.HTTPException as e:
            error_handler.log_command_error(ctx, "聯賽", e)
            await error_handler.handle_api_error(
                ctx, f"Discord 發送失敗：{type(e).__name__}", str(e)
            )
        finally:
            try:
                await processing_msg.delete()
            except discord.NotFound:
                pass


async def setup(bot):
    await bot.add_cog(LeagueTracker(bot))
