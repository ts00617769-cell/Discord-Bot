import discord
from discord.ext import commands
import aiohttp
import asyncio
import re
import unicodedata

class CastleTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 🎯 大區對照表 (與排行榜共用最新情報)
        self.server_map = {
            "戴摩爾克": "livegm_w02", "亞羅格": "livegm_w03", "萊涅": "livegm_w04",
            "貝爾姆特": "livegm_w05", "困特": "livegm_w06", "修連": "livegm_w07",
            "伊奈司": "livegm_w08", "基安": "livegm_w09", "扎爾巴爾": "livegm_w10",
            "黛庫爾": "livegm_w11", "耶拉普斯": "livegm_w13", "賽爾齊歐": "livegm_w14"
        }
        
        # 🎯 活躍分流名單 (確保爬蟲全速運作)
        self.realm_map = {
            "戴摩爾克": ["3", "4"], "亞羅格": ["5"], "貝爾姆特": ["1", "3"],
            "萊涅": ["1", "2", "3", "4", "5"], "困特": ["3"], "修連": ["5"],
            "伊奈司": ["1", "3"], "基安": ["5"], "扎爾巴爾": ["2"],
            "黛庫爾": ["1"], "耶拉普斯": ["1"], "賽爾齊歐": ["1", "2"]
        }

    # 📏 輔助函數：計算中英文等寬對齊
    def get_display_width(self, text):
        return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(text))

    def pad_text(self, text, target_width):
        text_str = str(text)
        current_width = self.get_display_width(text_str)
        return text_str + " " * max(0, target_width - current_width)

    # 📡 異步撈取據點資料 (配備防護罩穿透)
    async def fetch_territory_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiTerritoryByWorldId"
        payload = {"world_group_id": group_id, "world_id": world_id, "territory_grade": None, "guild_id": None}
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://warsofprasia.beanfun.com",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }
        
        try:
            async with session.post(api_url, json=payload, headers=headers, ssl=False) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("territory") or []
        except Exception:
            pass
        return []

    @commands.command(name="稅收",help="查詢據點鑽石稅收。例如: !稅收 全服, !稅收 20 萊涅01")
    async def get_castle_tax(self, ctx, *args):
        count = 15 # 預設顯示前 15 名肥羊城
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
            display_title = f"全伺服器 據點稅收 TOP {count}"
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
            
            display_title = f"【{group_name}{realm_num}】 據點稅收 TOP {count}"

        processing_msg = await ctx.send(f"📡 啟動鑽石雷達，掃描 {display_title} 庫房中...")

        try:
            all_territories = []
            async with aiohttp.ClientSession() as session:
                if is_global:
                    tasks = []
                    for g_name, g_id in self.server_map.items():
                        realms = self.realm_map.get(g_name, ["1", "2", "3", "4", "5"])
                        for r in realms:
                            w_id = f"{g_id}_r{r}"
                            tasks.append(self.fetch_territory_data(session, g_id, w_id))
                    
                    results = await asyncio.gather(*tasks)
                    for r in results:
                        all_territories.extend(r)
                else:
                    territories = await self.fetch_territory_data(session, target_group_id, target_world_id)
                    all_territories.extend(territories)

            if not all_territories:
                return await processing_msg.edit(content=f"❌ 掃描失敗，該區目前沒有據點資料。")

            # 💰 核心邏輯：依照「鑽石稅收 (tax_dia)」由高到低大排序！
            all_territories.sort(key=lambda x: x.get("tax_dia", 0), reverse=True)
            
            # 過濾掉那些鑽石為 0 的窮困駐紮地 (除非全服都沒鑽石)
            rich_territories = [t for t in all_territories if t.get("tax_dia", 0) > 0]
            if not rich_territories:
                rich_territories = all_territories # 如果大家都是 0，就還是全顯示

            top_list = rich_territories[:count]

            # 🌟 戰情報表排版 (等寬對齊，視覺舒爽)
            description = "```yaml\n" 
            for idx, t in enumerate(top_list, 1):
                dia = t.get("tax_dia", 0)
                ruby = t.get("tax_ruby", 0)
                guild = str(t.get("guild_name") or "無人佔領")
                t_name = str(t.get("territory_name") or "未知據點")
                t_grade = str(t.get("territory_grade_name") or "據點")
                
                # 如果是全服查詢，加上伺服器名稱前綴
                server_prefix = f"[{t.get('world_name', '未知')}] " if is_global else ""
                full_name = f"{server_prefix}{t_name} ({t_grade})"
                
                # 計算寬度，強迫對齊
                name_padded = self.pad_text(full_name, 32)
                guild_padded = self.pad_text(f"佔領: {guild}", 20)
                
                line = f"{idx:02d}. {name_padded} | {guild_padded} | 💎鑽石: {dia:>6} | 🔴紅寶: {ruby:>6}\n"
                
                # 避免超過 Discord 文字上限
                if len(description) + len(line) > 1900:
                    description += "```"
                    embed = discord.Embed(title=f"🏰 {display_title} (續)", description=description, color=0x00FFFF)
                    await ctx.send(embed=embed)
                    description = "```yaml\n"
                description += line
            
            description += "```"
            embed = discord.Embed(title=f"🏰 {display_title}", description=description, color=0x00FFFF)
            embed.set_footer(text="系統：全域據點歲收掃描器 (按鑽石量排序)")
            await processing_msg.delete()
            await ctx.send(embed=embed)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 雷達發生故障：{str(e)}")

async def setup(bot):
    await bot.add_cog(CastleTracker(bot))