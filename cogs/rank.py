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
                    results = await asyncio