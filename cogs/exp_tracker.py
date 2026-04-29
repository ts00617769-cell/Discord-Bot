import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import sqlite3
import datetime
from game_data import SERVER_MAP

class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 1. 啟動並連線資料庫
        self.db_conn = sqlite3.connect('prasia_data.db')
        self.cursor = self.db_conn.cursor()
        self.setup_database()
        # 👇 確保這行有在裡面！
        self.setup_database()
        # 2. 啟動背景巡邏兵
        self.auto_fetch_exp.start()

    def setup_database(self):
        """建立用來存放經驗值履歷的資料表"""
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
        # 建立索引加快搜尋速度
        self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_time_server ON exp_history(record_time, server_name)')
        self.db_conn.commit()

    def cog_unload(self):
        """模組關閉時安全停止任務與資料庫"""
        self.auto_fetch_exp.cancel()
        self.db_conn.close()

    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        try:
            async with session.post(api_url, json=payload) as response:
                if response.status == 200:
                    json_data = await response.json()
                    # 為了效能，我們只追蹤每服前 100 名的練功動態
                    return json_data.get("data", {}).get("gc", [])[:100]
        except Exception:
            pass
        return []

    # 🤖 【背景排程任務】每小時執行一次 (hours=1)
    @tasks.loop(hours=1.0)
    async def auto_fetch_exp(self):
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 啟動例行經驗值抄表作業...")
        now_time = datetime.datetime.now().replace(minute=0, second=0, microsecond=0) # 整點記錄
        
        async with aiohttp.ClientSession() as session:
            for server_name, (g_id, w_id) in SERVER_MAP.items():
                players = await self.fetch_server_data(session, g_id, w_id)
                
                # 寫入資料庫
                for p in players:
                    self.cursor.execute('''
                        INSERT INTO exp_history (record_time, server_name, player_name, class_name, level, exp)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (now_time, server_name, p.get('gc_name'), p.get('class_name'), p.get('gc_level'), p.get('gc_exp', 0)))
                
                self.db_conn.commit()
                await asyncio.sleep(1) # 避免對橘子發送太快被擋
        print("✅ 抄表完成！")

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        """確保機器人完全啟動後才開始背景任務"""
        await self.bot.wait_until_ready()

    # 📊 【查詢指令】調閱測速照相機
    @commands.command(name="測速", help="查詢伺服器練功時速 (例: !測速 萊涅01)")
    async def check_exp_speed(self, ctx, target_server: str = None):
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids: return
        # 🛠️ 【防呆機制】強制補建資料表，防止 NAS 讀寫太慢導致遺失
        self.setup_database()
        if not target_server or target_server not in SERVER_MAP:
            valid_list = "、".join(SERVER_MAP.keys())
            return await ctx.send(f"❌ 請指定一個伺服器。支援：{valid_list}\n用法範例：`!測速 萊涅01`")

        processing_msg = await ctx.send("📡 正在調閱資料庫，計算最新練功時速...")

        # 找尋資料庫中最新的兩個時間點 (也就是最新跟前一個小時)
        self.cursor.execute('''
            SELECT DISTINCT record_time FROM exp_history 
            WHERE server_name = ? ORDER BY record_time DESC LIMIT 2
        ''', (target_server,))
        times = self.cursor.fetchall()

        if len(times) < 2:
            return await processing_msg.edit(content="⚠️ 資料庫樣本不足！巡邏兵才剛啟動，請等待至少 1 小時後再來測速。")

        time_now = times[0][0]
        time_prev = times[1][0]

        # 提取這兩個時間點的資料進行比對
        self.cursor.execute('''
            SELECT t1.player_name, t1.level, t1.exp, t2.exp 
            FROM exp_history t1
            JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
            WHERE t1.server_name = ? AND t1.record_time = ? AND t2.record_time = ?
        ''', (target_server, time_now, time_prev))
        
        records = self.cursor.fetchall()
        
        # 計算時速並排序 (現在經驗 - 過去經驗)
        speed_data = []
        for name, level, exp_now, exp_prev in records:
            exp_diff = exp_now - exp_prev
            if exp_diff > 0: # 排除掉級或沒練功的人
                speed_data.append({
                    "name": name,
                    "level": level,
                    "speed": exp_diff
                })
        
        # 依據時速高低排序，取前 15 名的農夫
        speed_data.sort(key=lambda x: x['speed'], reverse=True)
        top_farmers = speed_data[:15]

        if not top_farmers:
            return await processing_msg.edit(content="💤 大家都沒在練功，或者資料異常。")

        # 格式化輸出
        description = f"**比對區間：{time_prev[:16]} ➡️ {time_now[:16]}**\n```yaml\n"
        for idx, p in enumerate(top_farmers, 1):
            speed_yi = p['speed'] / 100_000_000 # 轉換成「億」為單位
            
            # 對齊排版
            name = str(p['name'])
            name_padded = name + " " * max(0, 14 - len(name.encode('big5', 'ignore')))
            
            description += f"{idx:02d}. {name_padded} | Lv.{p['level']} | 時速: {speed_yi:>6.2f} 億\n"
        description += "```"

        embed = discord.Embed(title=f"🏎️ 【{target_server}】 練功時速排行榜", description=description, color=0x00ff00)
        embed.set_footer(text="系統：全自動經驗值測速雷達")
        
        await processing_msg.delete()
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ExpTracker(bot))