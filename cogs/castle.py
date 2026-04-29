import discord
from discord.ext import commands
import aiohttp
import asyncio
import unicodedata
from game_data import SERVER_MAP

class CastleTracker(commands.Cog):
    # ✅ 加回引擎
    def __init__(self, bot):
        self.bot = bot

    def get_display_width(self, text):
        return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(text))

    def pad_text(self, text, target_width):
        text_str = str(text)
        current_width = self.get_display_width(text_str)
        return text_str + " " * max(0, target_width - current_width)

    async def fetch_territory_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiTerritoryByWorldId"
        payload = {"world_group_id": group_id, "world_id": world_id, "territory_grade": None, "guild_id": None}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
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

    @commands.command(name="稅收", help="例如: !稅收 全服, !稅收 20 萊涅01")
    async def get_castle_tax(self, ctx, *args):
        # 🛡️ 【資安防護網】
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids: return 

        count = 15
        args_list = list(args)
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 50: count = 50
        if count < 1: count = 10

        target_server = "".join(args_list) if args_list else "全服"

        is_global = False
        if target_server in ["全服", "全部", ""]:
            is_global = True
            display_title = f"全伺服器 據點稅收 TOP {count}"
        else:
            if target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await ctx.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")
            
            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】 據點稅收 TOP {count}"

        processing_msg = await ctx.send(f"📡 啟動鑽石雷達，掃描 {display_title} 庫房中...")

        try:
            all_territories = []
            async with aiohttp.ClientSession() as session:
                if is_global:
                    tasks = [self.fetch_territory_data(session, g_id, w_id) for _, (g_id, w_id) in SERVER_MAP.items()]
                    results = await asyncio.gather(*tasks)
                    for r in results: all_territories.extend(r)
                else:
                    territories = await self.fetch_territory_data(session, target_group_id, target_world_id)
                    all_territories.extend(territories)

            if not all_territories:
                return await processing_msg.edit(content=f"❌ 掃描失敗，該區目前沒有據點資料。")

            all_territories.sort(key=lambda x: x.get("tax_dia", 0), reverse=True)
            rich_territories = [t for t in all_territories if t.get("tax_dia", 0) > 0]
            if not rich_territories: rich_territories = all_territories
            top_list = rich_territories[:count]

            description = "```yaml\n" 
            for idx, t in enumerate(top_list, 1):
                dia, ruby = t.get("tax_dia", 0), t.get("tax_ruby", 0)
                guild = str(t.get("guild_name") or "無人佔領")
                t_name = str(t.get("territory_name") or "未知據點")
                t_grade = str(t.get("territory_grade_name") or "據點")
                
                server_prefix = f"[{t.get('world_name', '未知')}] " if is_global else ""
                full_name = f"{server_prefix}{t_name} ({t_grade})"
                
                name_padded = self.pad_text(full_name, 32)
                guild_padded = self.pad_text(f"佔領: {guild}", 20)
                
                line = f"{idx:02d}. {name_padded} | {guild_padded} | 💎鑽石: {dia:>6} | 🔴紅寶: {ruby:>6}\n"
                
                if len(description) + len(line) > 1900:
                    description += "```"
                    embed = discord.Embed(title=f"🏰 {display_title} (續)", description=description, color=0x00FFFF)
                    await ctx.send(embed=embed)
                    description = "```yaml\n"
                description += line
            
            description += "```"
            embed = discord.Embed(title=f"🏰 {display_title}", description=description, color=0x00FFFF)
            embed.set_footer(text="系統：O(1) 極速據點掃描器")
            await processing_msg.delete()
            await ctx.send(embed=embed)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 雷達發生故障：{str(e)}")

async def setup(bot):
    await bot.add_cog(CastleTracker(bot))