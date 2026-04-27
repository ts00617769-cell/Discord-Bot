import discord
from discord.ext import commands
import requests
import re

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 🎯 伺服器對照表 (已移除沒有資料的克隆)
        self.server_map = {
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

    @commands.command(name="排名", aliases=["排行榜", "前十名"], help="查詢排行。例如: !排名 全服, !排名 25 萊涅01")
    async def get_ranking(self, ctx, *args):
        # 預設值設定：預設查 10 人、全服
        count = 10
        group_name = "全服"
        realm_num = "01"

        args_list = list(args)
        
        # 1. 判斷第一個參數是不是數字 (例如輸入 25)
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        # 數量防呆限制 (最高 50 名)
        if count > 50:
            count = 50
        if count < 1:
            count = 10

        # 2. 取得要查詢的伺服器 (大區)
        if len(args_list) > 0:
            group_name = args_list.pop(0)
            
        # 3. 取得分流編號
        if len(args_list) > 0:
            realm_num = args_list.pop(0)

        # 處理「全服」查詢邏輯
        is_global = False
        if group_name in ["全服", "全部"]:
            is_global = True
            target_group_id = None
            target_world_id = None
            display_title = f"全伺服器 TOP {count}"
        else:
            # 防呆：自動拆解沒打空格的伺服器名稱 (例如 "萊涅01")
            match = re.match(r"([^\d]+)(\d+)", group_name)
            if match:
                group_name = match.group(1)
                realm_num = match.group(2)

            target_group_id = self.server_map.get(group_name)
            if not target_group_id:
                valid_list = "、".join(self.server_map.keys())
                return await ctx.send(f"❌ 找不到大區「{group_name}」。支援清單：{valid_list} 或輸入「全服」")
            
            try:
                r_num = str(int(realm_num))
                target_world_id = f"{target_group_id}_r{r_num}"
            except ValueError:
                return await ctx.send("❌ 分流編號請輸入數字 (例如: 01)")
            
            display_title = f"【{group_name}{realm_num}】 TOP {count}"

        processing_msg = await ctx.send(f"🔍 正在撈取 {display_title} 的戰情數據...")

        try:
            api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
            payload = {
                "world_group_id": target_group_id,
                "world_id": target_world_id,
                "class": None
            }
            
            response = requests.post(api_url, json=payload)
            
            if response.status_code == 200:
                json_data = response.json()
                player_list = json_data.get("data", {}).get("gc", [])
                
                if not player_list:
                    return await processing_msg.edit(content=f"❌ 找不到 {display_title} 的資料。")

                top_list = player_list[:count]
                
                # 數量 <= 10 使用精美卡片欄位排版
                if count <= 10:
                    embed = discord.Embed(title=f"🏆 {display_title}", color=0xffd700)
                    for idx, p in enumerate(top_list, 1):
                        exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                        server_info = f"({p.get('world_name')}) " if is_global else ""
                        embed.add_field(
                            name=f"第 {idx} 名：{p.get('gc_name')}",
                            value=f"{server_info}{p.get('class_name')} | Lv.{p.get('gc_level')} | **{exp_zhao:,.2f} 兆**",
                            inline=False
                        )
                    await processing_msg.delete()
                    await ctx.send(embed=embed)
                
                # 數量 > 10 使用緊湊清單排版，避免畫面洗頻
                else:
                    description = ""
                    for idx, p in enumerate(top_list, 1):
                        exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                        server_info = f"[{p.get('world_name')}]" if is_global else ""
                        line = f"`{idx:02d}.` {p.get('gc_name')} (Lv.{p.get('gc_level')}) | {exp_zhao:,.2f} 兆 {server_info}\n"
                        
                        # 分頁防呆 (Discord 單則訊息限 2000 字)
                        if len(description) + len(line) > 1900:
                            embed = discord.Embed(title=f"🏆 {display_title} (續)", description=description, color=0xffd700)
                            await ctx.send(embed=embed)
                            description = ""
                        description += line
                    
                    embed = discord.Embed(title=f"🏆 {display_title}", description=description, color=0xffd700)
                    embed.set_footer(text="單位：兆經驗值")
                    await processing_msg.delete()
                    await ctx.send(embed=embed)

            else:
                await processing_msg.edit(content=f"⚠️ API 連線失敗 (狀態碼: {response.status_code})")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))