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
        api_url = "[https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking](https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking)"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        try:
            async with session.post(api_url, json=payload) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("gc") or []
        except Exception:
            pass
        return []

    @commands.command(name="排名", help="例如: !排名 幻影劍士, !排名 25 萊涅01 咒文刻印使")
    async def get_ranking(self, ctx, *args):
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids:
            return 

        count = 10
        args_list = list(args)
        
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 100: count = 100
        if count < 1: count = 10

        target_server = "全服"
        target_class = None
        class_parts = []

        for arg in args_list:
            if arg in SERVER_MAP or arg in ["全服", "全部", "global"]:
                target_server = arg
            else:
                class_parts.append(arg)

        if class_parts:
            target_class = "".join(class_parts)

        is_global = target_server in ["全服", "全部", "global"]
        
        filter_msg = f"【{target_class}】" if target_class else " "
        if is_global:
            display_title = f"全伺服器{filter_msg}TOP {count}"
        else:
            if target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await ctx.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")
            
            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】{filter_msg}TOP {count}"

        processing_msg = await ctx.send(f"🔍 正在潛入橘子主機，彙整 {display_title} 即時戰情...")

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

            if target_class:
                all_players = [p for p in all_players if target_class in str(p.get("class_name", ""))]

            if not all_players:
                return await processing_msg.edit(content=f"❌ 找不到符合【{target_class}】職業條件的即時排名資料。")

            all_players.sort(key=lambda x: x.get("gc_exp", 0), reverse=True)
            top_list = all_players[:count]
            
            # ==========================================
            # 💖 10名以內：移除醜陋灰塊，單位換成「億」保留整數精度
            # ==========================================
            if count <= 10:
                embed = discord.Embed(title=f"🏆 {display_title}", color=0xf1c40f)
                for idx, p in enumerate(top_list, 1):
                    # 改用「億」為單位
                    exp_yi = p.get("gc_exp", 0) / 100_000_000
                    server_info = f"({p.get('world_name', '未知')}) " if is_global else ""
                    
                    name = str(p.get('gc_name', '未知'))
                    class_name = str(p.get('class_name', '未知')) 
                    tag = self.get_member_info(name) 
                    display_name = f"{name}{tag}"
                    
                    # 乾淨的粗體字，不再用 code block
                    embed.add_field(
                        name=f"第 {idx} 名：{display_name}",
                        value=f"{server_info}[{class_name}] | Lv.{p.get('gc_level')} | **{exp_yi:,.0f} 億**",
                        inline=False
                    )
                embed.set_footer(text="系統：即時雷達過濾引擎")
                await processing_msg.delete()
                await ctx.send(embed=embed)
                
            # ==========================================
            # 10名以上：維持彩色 yaml，單位同樣換成「億」
            # ==========================================
            else:
                description = "```yaml\n" 
                for idx, p in enumerate(top_list, 1):
                    # 改用「億」為單位
                    exp_yi = p.get("gc_exp", 0) / 100_000_000
                    server_info = f"({p.get('world_name', '未知')})" if is_global else ""
                    
                    name = str(p.get('gc_name') or "未知")
                    class_name = str(p.get('class_name', '未知')) 
                    tag = self.get_member_info(name) 
                    display_name = f"{name}{tag}"
                    
                    level_str = f"Lv.{p.get('gc_level', '?')}"
                    
                    name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in display_name)
                    name_padded = display_name + " " * max(0, 16 - name_width)
                    
                    line = f"{idx:02d}. {name_padded} [{class_name:<6}] | {level_str:<5} | {exp_yi:>9,.0f} 億 {server_info}\n"
                    
                    if len(description) + len(line) > 1900:
                        description += "```"
                        embed = discord.Embed(title=f"🏆 {display_title} (續)", description=description, color=0xf1c40f)
                        await ctx.send(embed=embed)
                        description = "```yaml\n"
                    description += line
                
                description += "```"
                embed = discord.Embed(title=f"🏆 {display_title}", description=description, color=0xf1c40f)
                embed.set_footer(text="單位：億經驗值 | 系統：即時雷達過濾引擎")
                await processing_msg.delete()
                await ctx.send(embed=embed)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生嚴重錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))