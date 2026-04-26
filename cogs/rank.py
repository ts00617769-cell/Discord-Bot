import discord
from discord.ext import commands
import requests
import re

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 🎯 2026 最新前線情報網 (已加入貝爾姆特)
        self.server_map = {
            "克隆": "livegm_w01",
            "戴摩爾克": "livegm_w02",
            "亞羅格": "livegm_w03",
            "萊涅": "livegm_w04",
            "貝爾姆特": "livegm_w05",
            "困特": "livegm_w06",
            "修連": "livegm_w07",
            "伊奈司": "livegm_w08",
            "基安": "livegm_w09",
            "扎爾巴爾": "livegm_w10",
            "黛庫爾": "livegm_w11",
            "耶拉普斯": "livegm_w13",
            "賽爾齊歐": "livegm_w14"
        }

    @commands.command(name="前十名", aliases=["排行榜", "神人榜"], help="查詢指定伺服器排行。格式: !前十名 [大區] [編號] (例如: !前十名 亞羅格 05)")
    async def top_10_exp(self, ctx, group_name: str = "萊涅", realm_num: str = "01"):
        # 防呆機制：如果成員忘記打空格，直接連著打 (例如: !前十名 亞羅格05)
        # 我們用正規表達式把它拆開
        match = re.match(r"([^\d]+)(\d+)", group_name)
        if match and realm_num == "01": # 如果 group_name 夾帶數字，且 realm_num 是預設值
            group_name = match.group(1)
            realm_num = match.group(2)

        # 1. 檢查大區是否存在
        group_id = self.server_map.get(group_name)
        if not group_id:
            valid_servers = "、".join(self.server_map.keys())
            await ctx.send(f"❌ 找不到大區「{group_name}」。目前支援：{valid_servers}")
            return

        # 2. 格式化分流編號 (確保變成 r1, r5...)
        try:
            r_num = str(int(realm_num))
            world_id = f"{group_id}_r{r_num}"
        except ValueError:
            await ctx.send("❌ 分流編號請輸入數字 (例如: 05 或 5)")
            return

        processing_msg = await ctx.send(f"🔍 正在撈取【{group_name}{realm_num}】的數據...")

        try:
            api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
            
            payload = {
                "world_group_id": group_id,
                "world_id": world_id,
                "class": None
            }
            
            response = requests.post(api_url, json=payload)
            
            if response.status_code == 200:
                json_data = response.json()
                player_list = json_data.get("data", {}).get("gc", [])
                
                # 如果該分流不存在 (例如戴摩爾克01)，官方會回傳空陣列
                if not player_list:
                    await processing_msg.edit(content=f"❌ 找不到【{group_name}{realm_num}】的資料！(可能該伺服器尚未開放，或代碼輸入錯誤)")
                    return

                top_10 = player_list[:10]
                embed = discord.Embed(title=f"🏆 波拉西亞戰記 - 【{group_name}{realm_num}】TOP 10", color=0xffd700)
                
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
                
                embed.set_footer(text=f"資料來源：官方 API 即時攔截 ({group_name}{realm_num})")
                await processing_msg.delete()
                await ctx.send(embed=embed)
            else:
                await processing_msg.edit(content=f"⚠️ API 連線失敗 (代碼: {response.status_code})")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))