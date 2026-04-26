import discord
from discord.ext import commands
import requests

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="前十名", aliases=["排行榜", "神人榜"], help="查詢本伺服器經驗值最高的前 10 名玩家")
    async def top_10_exp(self, ctx):
        processing_msg = await ctx.send("🔍 正在駭入官方資料庫，撈取萊涅01排行榜...")

        try:
            api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
            
            # 🎯 填入你抓到的精準通關密語 (注意 null 要改成 Python 的 None)
            payload = {
                "world_group_id": "livegm_w04",
                "world_id": "livegm_w04_r1",
                "class": None
            }
            
            response = requests.post(api_url, json=payload)
            
            if response.status_code == 200:
                json_data = response.json()
                player_list = json_data.get("data", {}).get("gc", [])
                
                if not player_list:
                    await processing_msg.edit(content="❌ 取得資料失敗，橘子伺服器回傳空資料。")
                    return

                top_10 = player_list[:10]
                
                # 幫你看板換個標題，註明是萊涅01
                embed = discord.Embed(title="🏆 波拉西亞戰記 - 【萊涅01】經驗值 TOP 10", color=0xffd700)
                
                for idx, player in enumerate(top_10, 1):
                    name = player.get("gc_name", "未知")
                    level = player.get("gc_level", "?")
                    exp = player.get("gc_exp", 0)
                    job = player.get("class_name", "未知職業") 
                    
                    embed.add_field(
                        name=f"第 {idx} 名：{name}",
                        value=f"⚔️ {job} | 📈 Lv.{level} | 💎 經驗：**{exp:,}**",
                        inline=False
                    )
                
                embed.set_footer(text="資料來源：官方 API 即時攔截 (萊涅01)")
                
                await processing_msg.delete()
                await ctx.send(embed=embed)
            else:
                await processing_msg.edit(content=f"⚠️ API 連線失敗 (狀態碼: {response.status_code})")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))