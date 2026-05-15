import discord
from discord.ext import commands
import aiohttp
import asyncio
import unicodedata
from game_data import SERVER_MAP

class SubjugationCog(commands.Cog):
    # ✅ 加回引擎
    def __init__(self, bot):
        self.bot = bot

    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        try:
            async with session.post(api_url, json=payload, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {}).get("gc", [])
        except Exception:
            pass
        return []
    

    @commands.command(name="討伐排名", aliases=["討伐"], help="查詢全服前 100 名討伐等級排行")
    async def get_subjugation_ranking(self, ctx):
        # 🛡️ 【資安防護網】(順便幫你加上來了)
        # allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        # if ctx.channel.id not in allowed_channel_ids: return 

        processing_msg = await ctx.send("📡 啟動全服討伐雷達掃描中，請稍候...")

        all_players = []
        async with aiohttp.ClientSession() as session:
            # ✅ 新版極速掃描
            tasks = [self.fetch_server_data(session, g_id, w_id) for _, (g_id, w_id) in SERVER_MAP.items()]
            results = await asyncio.gather(*tasks)
            for res in results: all_players.extend(res)

        if not all_players:
            return await processing_msg.edit(content="❌ 資料抓取異常，請確認官方 API 狀態。")

        def sort_key(player):
            grade_val = (player.get("string_map") or {}).get("grade", "0")
            try: grade = int(grade_val)
            except: grade = 0
            return (grade, player.get("gc_exp", 0))

        all_players.sort(key=sort_key, reverse=True)
        top_100 = all_players[:100]

        description = "```yaml\n"
        embeds = []
        
        for i, p in enumerate(top_100, 1):
            name = str(p.get('gc_name') or "未知")
            world = str(p.get('world_name') or "未知")
            level = p.get('gc_level', '?')
            grade = (p.get("string_map") or {}).get("grade", "0")
            
            name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in name)
            name_padded = name + " " * max(0, 14 - name_width)
            
            world_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in world)
            world_padded = world + " " * max(0, 10 - world_width)

            line = f"{i:03d}. [{world_padded}] {name_padded} | Lv.{level:<3} | 討伐 {grade}\n"
            
            if len(description) + len(line) > 1900:
                description += "```"
                embed = discord.Embed(title="⚔️ 全服討伐前 100 名戰情報表", description=description, color=0xffa500)
                embeds.append(embed)
                description = "```yaml\n"
            
            description += line
        
        if description != "```yaml\n":
            description += "```"
            embed = discord.Embed(title="⚔️ 全服討伐前 100 名戰情報表", description=description, color=0xffa500)
            embed.set_footer(text="系統：O(1) 極速伺服器雷達")
            embeds.append(embed)

        await processing_msg.delete()
        for e in embeds: await ctx.send(embed=e)

async def setup(bot):
    await bot.add_cog(SubjugationCog(bot))