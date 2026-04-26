import discord
from discord.ext import commands
import requests

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="全服前十", aliases=["排行榜", "神人榜"], help="查詢全伺服器經驗值最高的前 10 名玩家")
    async def top_10_exp(self, ctx):
        # 顯示處理中的提示
        processing_msg = await ctx.send("🔍 正在駭入官方資料庫，撈取全伺服器排行榜...")

        try:
            # 官方 API 網址
            api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
            
            # 📦 Payload 參數設定
            # 為了拿到「全部伺服器」，通常 API 的寫法是把伺服器 ID 留空，或者傳送空包裹。
            # 如果測試後發現只有單一伺服器，請去網頁端選擇「全伺服器」後，再抓一次 F12 的 Payload 填入這裡。
            payload = {
                "world_group_id": "", 
                "class": 0 
            }
            
            # 發送 POST 請求
            response = requests.post(api_url, json=payload)
            
            if response.status_code == 200:
                json_data = response.json()
                # 取得所有玩家的名單陣列
                player_list = json_data.get("data", {}).get("gc", [])
                
                if not player_list:
                    await processing_msg.edit(content="❌ 取得資料失敗，請檢查 Payload 參數是否正確。")
                    return

                # 🎯 陣列切片：只拿前 10 名
                top_10 = player_list[:10]
                
                # 製作戰情大看板
                embed = discord.Embed(title="🏆 波拉西亞戰記 - 全服經驗值 TOP 10", color=0xffd700)
                
                # 迴圈依序把前 10 名塞進卡片裡
                for idx, player in enumerate(top_10, 1):
                    name = player.get("gc_name", "未知")
                    level = player.get("gc_level", "?")
                    exp = player.get("gc_exp", 0)
                    server = player.get("world_name", "未知伺服器") # 跨服看榜，伺服器名稱很重要
                    
                    # 每一名玩家佔據一行 (inline=False 確保排版不會亂掉)
                    embed.add_field(
                        name=f"第 {idx} 名：{name}",
                        value=f"🌐 {server} | 📈 Lv.{level} | 💎 經驗：**{exp:,}**",
                        inline=False
                    )
                
                embed.set_footer(text="資料來源：波拉西亞戰記官方 API 即時攔截")
                
                # 刪除提示訊息，發送正式大看板
                await processing_msg.delete()
                await ctx.send(embed=embed)
            else:
                await processing_msg.edit(content=f"⚠️ API 連線失敗 (狀態碼: {response.status_code})")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))