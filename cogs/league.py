import discord
from discord.ext import commands
import aiohttp
import unicodedata
import os
import logging

logger = logging.getLogger(__name__)

class LeagueTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # ✨ 在啟動時就把頻道清單算好，存進 self 裡面 (最高效能寫法)
        allowed_channels_str = os.getenv("ALLOWED_COMMAND_CHANNELS", "")
        self.allowed_channel_ids = [int(cid.strip()) for cid in allowed_channels_str.split(",") if cid.strip()]

    # 📏 輔助函數：計算中英文等寬對齊
    def pad_text(self, text, target_width):
        text_str = str(text)
        current_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text_str)
        return text_str + " " * max(0, target_width - current_width)

    # ✨ 這裡加上了參數接收與預設值，解決變數未定義的問題！
    @commands.command(name="聯賽", help="查詢宇宙聯賽分數。格式: !聯賽 [季] [回合] [級別] (預設: 3 3 1)")
    async def get_league_score(self, ctx, season: str = "3", round_num: str = "3", league_id: str = "1"):
        
        # 🛡️ 資安防護網：如果不是在戰情室頻道，機器人就裝死
        if ctx.channel.id not in self.allowed_channel_ids:
            return 
        
        # ✅ 新增參數驗證
        try:
            season_int = int(season)
            round_int = int(round_num)
            league_int = int(league_id)
            
            # 合理性檢查
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

        # 💡 友善的提示訊息 (參數回顯機制)
        hint_msg = (
            f"🛰️ **啟動宇宙聯賽觀測站**\n"
            f"🔍 您的查詢條件為：\n"
            f"> 🏆 **第 {season} 賽季**\n"
            f"> ⚔️ **第 {round_num} 回合**\n"
            f"> 🛡️ **第 {league_id} 組** (如挑戰者1)\n"
            f"正在潛入橘子主機撈取戰況，請稍候..."
        )
        processing_msg = await ctx.send(hint_msg)

        api_url = "https://warsofprasia.beanfun.com/api/UniverseLeague/Ranking"
        
        # 動態生成 Payload 參數
        payload = {
            "season": f"UniverseLeague_TW_Live_Season0{season}",
            "roundId": f"Live_CrossRealmRound_UniverseLeague_TW_S0{season}_R{round_num}",
            "leagueId": str(league_id)
        }
        
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://warsofprasia.beanfun.com",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }

        try:
            # ✨ 直接使用全域 session，不重複建立連線！
            async with self.bot.session.post(api_url, json=payload, headers=headers, timeout=10) as response:
                if response.status != 200:
                    return await processing_msg.edit(content=f"❌ API 連線失敗 (狀態碼: {response.status})。請確認 API 網址是否正確。")
                
                json_data = await response.json()
                    
            league_ranking = json_data.get("data", {}).get("league_ranking", [])
            if not league_ranking:
                return await processing_msg.edit(content="❌ 找不到該季/回合的聯賽資料，可能尚未開打或參數輸入錯誤。")

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
                
                # 為每個賽區建立獨立的 Embed 卡片
                embed = discord.Embed(title=f"🌌 宇宙聯賽 - {match_name}", description=description, color=0x9B59B6)
                
                # 💡 在報表底部加上查詢參數，看起來更專業
                embed.set_footer(text=f"查詢參數：S0{season} 賽季 | 第 {round_num} 回合 | 第 {league_id} 組")
                embeds.append(embed)

            await processing_msg.delete()
            
            # 依序發送所有賽區的卡片
            for emb in embeds:
                await ctx.send(embed=emb)

        # ==========================================
        # 👇 替換這段：加上雙重保護的錯誤攔截機制
        # ==========================================
        except asyncio.TimeoutError as e:
            logger.error(f"Timeout fetching league data for S{season} R{round_num}: {e}")
            try:
                await processing_msg.edit(content="❌ 連線逾時：抓取聯賽資料花時過久，請重試")
            except discord.NotFound:
                await ctx.send("❌ 連線逾時：抓取聯賽資料花時過久，請重試")
        except aiohttp.ClientError as e:
            logger.error(f"HTTP client error while fetching league data: {e}")
            try:
                await processing_msg.edit(content="❌ 網路連線失敗：無法連接到遊戲伺服器")
            except discord.NotFound:
                await ctx.send("❌ 網路連線失敗：無法連接到遊戲伺服器")
        except ValueError as e:
            logger.error(f"JSON parsing error while fetching league data: {e}")
            try:
                await processing_msg.edit(content="❌ 資料解析錯誤：伺服器回傳的資料格式異常")
            except discord.NotFound:
                await ctx.send("❌ 資料解析錯誤：伺服器回傳的資料格式異常")
        except KeyError as e:
            logger.error(f"Missing required field in league data: {e}")
            try:
                await processing_msg.edit(content=f"❌ 模組發生錯誤：資料欄位異常")
            except discord.NotFound:
                await ctx.send("❌ 模組發生錯誤：資料欄位異常")
        except Exception as e:
            logger.error(f"Unexpected error while fetching league data: {e}")
            try:
                await processing_msg.edit(content=f"❌ 模組發生錯誤：{type(e).__name__}")
            except discord.NotFound:
                await ctx.send(f"❌ 模組發生錯誤：{type(e).__name__}")
        # ==========================================
async def setup(bot):
    await bot.add_cog(LeagueTracker(bot))