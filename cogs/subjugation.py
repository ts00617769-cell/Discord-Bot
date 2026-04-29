import discord
from discord.ext import commands
import aiohttp
import asyncio
import unicodedata
from game_data import SERVER_MAP
class SubjugationCog(commands.Cog):
    
    async def fetch_server_data(self, session, group_id, world_id):
        """向官方 API 請求資料的底層方法"""
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        
        try:
            async with session.post(api_url, json=payload, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", {}).get("gc", [])
        except Exception as e:
            print(f"抓取 {world_id} 失敗: {e}")
        return []

    @commands.command(name="討伐排名", aliases=["討伐"], help="查詢全服前 100 名討伐等級排行")
    async def get_subjugation_ranking(self, ctx):
        """主指令：全服討伐雷達"""
        processing_msg = await ctx.send("📡 啟動全服討伐雷達掃描中，請稍候...")

        all_players = []
        async with aiohttp.ClientSession() as session:
            tasks = []
            # 遍歷所有大區與伺服器分流
            for g_name, g_id in self.server_map.items():
                realms = self.realm_map.get(g_name, [])
                for r in realms:
                    w_id = f"{g_id}_r{r}"
                    tasks.append(self.fetch_server_data(session, g_id, w_id))
            
            # 併發執行，大幅縮短等待時間
            results = await asyncio.gather(*tasks)
            for res in results:
                all_players.extend(res)

        if not all_players:
            return await processing_msg.edit(content="❌ 資料抓取異常，請確認官方 API 狀態。")

        # 🧠 排序邏輯：優先比討伐等級 (grade)，等級相同則比經驗值 (gc_exp)
        def sort_key(player):
            grade_val = (player.get("string_map") or {}).get("grade", "0")
            try:
                grade = int(grade_val)
            except:
                grade = 0
            exp = player.get("gc_exp", 0)
            return (grade, exp)

        # 由大到小排序
        all_players.sort(key=sort_key, reverse=True)
        top_100 = all_players[:100]

        # 📦 封裝訊息 (處理 Discord 訊息長度限制)
        description = "```yaml\n"
        embeds = []
        
        for i, p in enumerate(top_100, 1):
            name = str(p.get('gc_name') or "未知")
            world = str(p.get('world_name') or "未知")
            level = p.get('gc_level', '?')
            grade = (p.get("string_map") or {}).get("grade", "0")
            
            # 使用 unicodedata 對齊中英文寬度
            name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in name)
            name_padded = name + " " * max(0, 14 - name_width)
            
            world_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in world)
            world_padded = world + " " * max(0, 10 - world_width)

            # 格式：[伺服器] 玩家姓名 | 等級 | 討伐等級
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
            embeds.append(embed)

        # 輸出結果
        await processing_msg.delete()
        for e in embeds:
            await ctx.send(embed=e)

async def setup(bot):
    await bot.add_cog(SubjugationCog(bot))