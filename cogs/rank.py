import discord
from discord.ext import commands
import aiohttp
import asyncio
import re
import unicodedata

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 🎯 大區對照表
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
        
        # 🎯 活躍分流名單 (優化爬蟲效率)
        self.realm_map = {
            "戴摩爾克": ["3", "4"],
            "亞羅格": ["5"],
            "貝爾姆特": ["1", "3"],
            "萊涅": ["1", "2", "3", "4", "5"],
            "困特": ["3"],
            "修連": ["5"],
            "伊奈司": ["1", "3"],
            "基安": ["5"],
            "扎爾巴爾": ["2"],
            "黛庫爾": ["1"],
            "耶拉普斯": ["1"],
            "賽爾齊歐": ["1", "2"]
        }

    # 使用你驗證過正常運作的連線邏輯
    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        try:
            async with session.post(api_url, json=payload) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("gc") or []
        except Exception:
            pass
        return []

    # 👇 這裡才是指令的大門口！
    @commands.command(name="排名", help="例如: !排名 全服, !排名 25 萊涅01")
    async def get_ranking(self, ctx, *args):
        
        # 🛡️ 【資安防護網】貼在這裡！一進門就先攔截檢查！
        allowed_channel_id = 1477966312411107493 # 👉 換成你們的頻道 ID
        
        if ctx.channel.id != allowed_channel_id:
            return # 如果不是指定頻道，直接退回不理他
        count = 10
        group_name = "全服"
        realm_num = "01"

        args_list = list(args)
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 50: count = 50
        if count < 1: count = 10

        if len(args_list) > 0: group_name = args_list.pop(0)
        if len(args_list) > 0: realm_num = args_list.pop(0)

        is_global = False
        if group_name in ["全服", "全部"]:
            is_global = True
            display_title = f"全伺服器 TOP {count}"
        else:
            match = re.match(r"([^\d]+)(\d+)", group_name)
            if match:
                group_name = match.group(1)
                realm_num = match.group(2)

            target_group_id = self.server_map.get(group_name)
            if not target_group_id:
                valid_list = "、".join(self.server_map.keys())
                return await ctx.send(f"❌ 找不到大區「{group_name}」。支援：{valid_list} 或 全服")
            
            try:
                r_num = str(int(realm_num))
                target_world_id = f"{target_group_id}_r{r_num}"
            except ValueError:
                return await ctx.send("❌ 分流編號請輸入數字")
            
            display_title = f"【{group_name}{realm_num}】 TOP {count}"

        processing_msg = await ctx.send(f"🔍 正在潛入橘子主機，彙整 {display_title} 戰情數據...")

        try:
            all_players = []
            async with aiohttp.ClientSession() as session:
                if is_global:
                    tasks = []
                    for g_name, g_id in self.server_map.items():
                        realms = self.realm_map.get(g_name, ["1", "2", "3", "4", "5"])
                        for r in realms:
                            w_id = f"{g_id}_r{r}"
                            tasks.append(self.fetch_server_data(session, g_id, w_id))
                    
                    results = await asyncio.gather(*tasks)
                    for r in results:
                        all_players.extend(r)
                else:
                    players = await self.fetch_server_data(session, target_group_id, target_world_id)
                    all_players.extend(players)

            if not all_players:
                return await processing_msg.edit(content=f"❌ 撈取失敗，找不到資料。")

            all_players.sort(key=lambda x: x.get("gc_exp", 0), reverse=True)
            top_list = all_players[:count]
            
            if count <= 10:
                embed = discord.Embed(title=f"🏆 {display_title}", color=0xffd700)
                for idx, p in enumerate(top_list, 1):
                    exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                    server_info = f"({p.get('world_name', '未知')}) " if is_global else ""
                    embed.add_field(
                        name=f"第 {idx} 名：{p.get('gc_name', '未知')}",
                        value=f"{server_info}{p.get('class_name')} | Lv.{p.get('gc_level')} | **{exp_zhao:,.2f} 兆**",
                        inline=False
                    )
                await processing_msg.delete()
                await ctx.send(embed=embed)
            else:
                description = "```yaml\n" 
                for idx, p in enumerate(top_list, 1):
                    exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                    server_info = f"[{p.get('world_name', '未知')}]" if is_global else ""
                    
                    # 🛡️ 關鍵防蟲：強制將 name 轉換為字串，避免 API 吐 null 導致崩潰
                    name = str(p.get('gc_name') or "未知")
                    level_str = f"Lv.{p.get('gc_level', '?')}"
                    
                    # 計算寬度並補空白
                    name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in name)
                    name_padded = name + " " * max(0, 16 - name_width)
                    
                    line = f"{idx:02d}. {name_padded} | {level_str:<5} | {exp_zhao:>8,.2f} 兆 {server_info}\n"
                    
                    if len(description) + len(line) > 1900:
                        description += "```"
                        embed = discord.Embed(title=f"🏆 {display_title} (續)", description=description, color=0xffd700)
                        await ctx.send(embed=embed)
                        description = "```yaml\n"
                    description += line
                
                description += "```"
                embed = discord.Embed(title=f"🏆 {display_title}", description=description, color=0xffd700)
                embed.set_footer(text="單位：兆經驗值 | 系統：全域聚合爬蟲 (等寬對齊版)")
                await processing_msg.delete()
                await ctx.send(embed=embed)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生嚴重錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))