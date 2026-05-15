import discord
from discord.ext import commands
import aiohttp
import asyncio
import unicodedata
import sqlite3
from game_data import SERVER_MAP

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_conn = sqlite3.connect('prasia_data.db', check_same_thread=False)
        self.cursor = self.db_conn.cursor()

    def get_member_info(self, name):
        try:
            self.cursor.execute('SELECT original_identity FROM member_registry WHERE player_name = ?', (name,))
            result = self.cursor.fetchone()
            return f"({result[0]})" if result else ""
        except:
            return ""

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

    @commands.command(name="排名", help="例如: !排名 全服, !排名 25 萊涅01")
    async def get_ranking(self, ctx, *args):
        # 🛡️ 【資安防護網】
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids:
            return 

        count = 10
        args_list = list(args)
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 100: count = 100
        if count < 1: count = 10

        target_server = "".join(args_list) if args_list else "全服"

        is_global = False
        if target_server in ["全服", "全部", ""]:
            is_global = True
            display_title = f"全伺服器 TOP {count}"
        else:
            if target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await ctx.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")
            
            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】 TOP {count}"

        processing_msg = await ctx.send(f"🔍 正在潛入橘子主機，彙整 {display_title} 戰情數據...")

        try:
            all_players = []
            async with aiohttp.ClientSession() as session:
                if is_global:
                    tasks = [self.fetch_server_data(session, g_id, w_id) for _, (g_id, w_id) in SERVER_MAP.items()]
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
            
            # ==========================================
            # ✨ 雙行排版：徹底解決手機與小視窗跑版問題
            # ==========================================
            description = "```yaml\n" 
            for idx, p in enumerate(top_list, 1):
                exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                server_info = f"({p.get('world_name', '未知')})" if is_global else ""
                
                name = str(p.get('gc_name') or "未知")
                class_name = str(p.get('class_name', '未知'))
                tag = self.get_member_info(name) 
                display_name = f"{name}{tag}"
                
                level_str = f"Lv.{p.get('gc_level', '?')}"
                
                # 計算名稱對齊寬度 (中文字元佔2，英數佔1)
                name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in display_name)
                name_padded = display_name + " " * max(0, 16 - name_width)
                
                # 第一行：名次. 名字 [職業] 等級 (伺服器)
                line1 = f"{idx:02d}. {name_padded} [{class_name}] {level_str} {server_info}\n"
                # 第二行：縮排顯示經驗值
                line2 = f"    ▶ 經驗值: {exp_zhao:,.2f} 兆\n"
                
                # 組合兩行
                full_line = line1 + line2
                
                # 🛡️ 檢查字數，若超過 Discord 單條 Embed 限制 (約 2000 字) 則先發送
                if len(description) + len(full_line) > 1900:
                    description += "```"
                    embed = discord.Embed(title=f"🏆 {display_title} (續)", description=description, color=0xffd700)
                    await ctx.send(embed=embed)
                    description = "```yaml\n"
                    
                description += full_line
            
            # 迴圈結束後，發送最後的 Embed
            description += "```"
            embed = discord.Embed(title=f"🏆 {display_title}", description=description, color=0xffd700)
            embed.set_footer(text="單位：兆經驗值 | 系統：O(1) 極速伺服器雷達 (雙行防跑版)")
            
            await processing_msg.delete()
            await ctx.send(embed=embed)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生嚴重錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))