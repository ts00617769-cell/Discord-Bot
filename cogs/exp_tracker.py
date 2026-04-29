import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import sqlite3
import datetime
import unicodedata
from game_data import SERVER_MAP

class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_conn = sqlite3.connect('prasia_data.db')
        self.cursor = self.db_conn.cursor()
        self.setup_database()
        self.auto_fetch_exp.start()

    def setup_database(self):
        """強制檢查並補建資料表"""
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                class_name TEXT,
                level INTEGER,
                exp REAL
            )
        ''')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_time_server ON exp_history(record_time, server_name)')
        self.db_conn.commit()

    def cog_unload(self):
        self.auto_fetch_exp.cancel()
        self.db_conn.close()

    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        try:
            async with session.post(api_url, json=payload, timeout=10) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("gc", [])[:100]
        except Exception:
            pass
        return []

    @tasks.loop(hours=1.0)
    async def auto_fetch_exp(self):
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 啟動例行抄表...")
        now_time = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
        
        async with aiohttp.ClientSession() as session:
            for server_name, (g_id, w_id) in SERVER_MAP.items():
                players = await self.fetch_server_data(session, g_id, w_id)
                for p in players:
                    self.cursor.execute('''
                        INSERT INTO exp_history (record_time, server_name, player_name, class_name, level, exp)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (now_time, server_name, p.get('gc_name'), p.get('class_name'), p.get('gc_level'), p.get('gc_exp', 0)))
                self.db_conn.commit()
                await asyncio.sleep(1)
        print("✅ 抄表完成！")

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()

    @commands.command(name="測速", help="!測速 全服 或 !測速 萊涅01")
    async def check_exp_speed(self, ctx, target_server: str = "全服"):
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids: return

        self.setup_database()
        is_global = target_server in ["全服", "全部", "global"]

        if not is_global and target_server not in SERVER_MAP:
            valid_list = "、".join(SERVER_MAP.keys())
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")

        processing_msg = await ctx.send(f"📡 正在調閱測速照相機，計算 {'全服' if is_global else target_server} 練功時速...")

        # 找尋資料庫中最新的兩個全球記錄時間
        self.cursor.execute('SELECT DISTINCT record_time FROM exp_history ORDER BY record_time DESC LIMIT 2')
        times = self.cursor.fetchall()

        if len(times) < 2:
            return await processing_msg.edit(content="⚠️ 樣本不足！巡邏兵才剛啟動，請等待至少 1 小時。")

        time_now, time_prev = times[0][0], times[1][0]

        # 執行 SQL JOIN 進行差異計算
        sql = '''
            SELECT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp 
            FROM exp_history t1
            JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
            WHERE t1.record_time = ? AND t2.record_time = ?
        '''
        params = [time_now, time_prev]
        if not is_global:
            sql += " AND t1.server_name = ?"
            params.append(target_server)

        self.cursor.execute(sql, tuple(params))
        records = self.cursor.fetchall()
        
        speed_data = []
        for name, server, level, exp_now, exp_prev in records:
            diff = exp_now - exp_prev
            if diff > 0:
                speed_data.append({"name": name, "server": server, "level": level, "speed": diff})
        
        speed_data.sort(key=lambda x: x['speed'], reverse=True)
        top_list = speed_data[:15]

        if not top_list:
            return await processing_msg.edit(content="💤 大家都沒在練功，或資料抓取空隙中。")

        # 格式化輸出
        desc = f"**區間：{time_prev[11:16]} ➡️ {time_now[11:16]}**\n```yaml\n"
        for idx, p in enumerate(top_list, 1):
            speed_yi = p['speed'] / 100_000_000 # 億
            name = str(p['name'])
            name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in name)
            name_padded = name + " " * max(0, 14 - name_width)
            
            srv_info = f"({p['server']})" if is_global else ""
            desc += f"{idx:02d}. {name_padded} | Lv.{p['level']:<2} | {speed_yi:>6.2f}億 {srv_info}\n"
        desc += "```"

        embed = discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 TOP 15", description=desc, color=0x00ff00)
        embed.set_footer(text="系統：全自動經驗值測速雷達 | 單位：時速/億")
        
        await processing_msg.delete()
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ExpTracker(bot))