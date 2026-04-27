import discord
from discord.ext import commands
import aiohttp
import unicodedata

class LeagueTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 📏 輔助函數：計算中英文等寬對齊
    def pad_text(self, text, target_width):
        text_str = str(text)
        current_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text_str)
        return text_str + " " * max(0, target_width - current_width)

    @commands.command(name="聯賽", aliases=["宇宙聯賽", "league"], help="查詢宇宙聯賽分數。格式: !聯賽 [季] [回合] [級別] (預設: 3 3 1)")
    async def get_league_score(self, ctx, season: str = "3", round_num: str = "3", league_id: str = "1"):
        
        processing_msg = await ctx.send(f"🛰️ 正在連線至宇宙聯賽資料庫 (S0{season} - R{round_num} - 級別 {league_id})...")

        # ⚠️ 注意：這裡的 API 網址是我根據命名規則推測的。
        # 如果執行後顯示連線失敗，請你在 F12 中確認該請求的 Request URL 並替換掉這行！
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiUniverseLeagueRanking"
        
        # 動態生成 Payload 參數
        payload = {
            "season": f"UniverseLeague_TW_Live_Season0{season}",
            "roundId": f"Live_CrossRealmRound_UniverseLeague_TW_S0{season}_R{round_num}",
            "leagueId": str(league_id)
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://warsofprasia.beanfun.com",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=payload, headers=headers, ssl=False) as response:
                    if response.status != 200:
                        return await processing_msg.edit(content=f"❌ API 連線失敗 (狀態碼: {response.status})。請確認 API 網址是否正確。")
                    
                    json_data = await response.json()
                    
            league_ranking = json_data.get("data", {}).get("league_ranking", [])
            if not league_ranking:
                return await processing_msg.edit(content="❌ 找不到該季/回合的聯賽資料，可能尚未開打或參數錯誤。")

            match_list = league_ranking[0].get("match", [])
            if not match_list:
                return await processing_msg.edit(content="❌ 資料庫回傳為空，沒有對戰紀錄。")

            embeds = []
            
            # 遍歷每一個賽區 (例如：挑戰者 1)
            for match in match_list:
                match_name = match.get("league_name", "未知賽區")
                teams = match.get("team", [])
                
                # 排序：把總分最高的陣營排在前面
                teams.sort(key=lambda x: x.get("team_score", 0), reverse=True)
                
                description = "```yaml\n"
                
                # 遍歷賽區內的每一個陣營
                for team_idx, team in enumerate(teams, 1):
                    team_score = team.get("team_score", 0)
                    members = team.get("team_member", [])
                    
                    description += f"🏆 陣營 {team_idx} (總分: {team_score})\n"
                    
                    # 排序：把陣營內貢獻分數最高的公會排在前面
                    members.sort(key=lambda x: x.get("ranking_value", 0), reverse=True)
                    
                    for member in members:
                        is_leader = member.get("team_leader_flag", False)
                        icon = "👑" if is_leader else "🔸"
                        world = member.get("world_name", "未知")
                        guild = member.get("guild_name", "未知公會")
                        score = member.get("ranking_value", 0)
                        territory = member.get("territory_name", "無據點")
                        
                        # 組合字串並對齊
                        guild_display = f"[{world}] {guild}"
                        guild_padded = self.pad_text(guild_display, 22)
                        
                        description += f"   {icon} {guild_padded} | {score:>5}分 | {territory}\n"
                    
                    description += "\n" # 陣營之間空一行
                
                description += "```"
                
                # 因為每個賽區(Match)的資料量較大，我們為每個賽區建立一個獨立的 Embed 卡片
                embed = discord.Embed(title=f"🌌 宇宙聯賽 - {match_name}", description=description, color=0x9B59B6)
                embeds.append(embed)

            await processing_msg.delete()
            
            # 依序發送所有賽區的卡片
            for emb in embeds:
                await ctx.send(embed=emb)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 模組發生錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(LeagueTracker(bot))