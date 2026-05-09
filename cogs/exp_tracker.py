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
        # 加上 check_same_thread=False 防止多工衝突
        self.db_conn = sqlite3.connect('prasia_data.db', check_same_thread=False)
        self.cursor = self.db_conn.cursor()
        self.setup_database()
        
        # 🚨 警報設定
        self.ALERT_CHANNEL_ID = 1476506457032884328 
        self.SPEED_LIMIT = 4000 # 警報門檻：4000億
        
        # 🎛️ 警報開關 (預設為 False：關閉狀態，不會吵人)
        self.alerts_enabled = False 
        
        self.auto_fetch_exp.start()

    def setup_database(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
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
                    return json_data.get("data", {}).get("gc", [])[:50]
        except Exception:
            pass
        return []

    # 🤖 【自動化哨兵】每 10 分鐘背景靜默抄表
    @tasks.loop(minutes=10.0)
    async def auto_fetch_exp(self):
        now_time = datetime.datetime.now().replace(second=0, microsecond=0)
        print(f"[{now_time.strftime('%H:%M:%S')}] 哨兵出動：掃描全服前50名...")
        
        async with aiohttp.ClientSession() as session:
            for server_name, (g_id, w_id) in SERVER_MAP.items():
                players = await self.fetch_server_data(session, g_id, w_id)
                for p in players:
                    self.cursor.execute('''
                        INSERT INTO exp_history (record_time, server_name, player_name, level, exp)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (now_time, server_name, p.get('gc_name'), p.get('gc_level'), p.get('gc_exp', 0)))
                self.db_conn.commit()
                await asyncio.sleep(0.5)
        
        # 🛡️ 只有在指揮官「打開開關」時，才執行警報推播
        if self.alerts_enabled:
            await self.check_for_alerts(now_time)

    async def check_for_alerts(self, current_time):
        self.cursor.execute('SELECT DISTINCT record_time FROM exp_history ORDER BY record_time DESC LIMIT 2')
        times = self.cursor.fetchall()
        if len(times) < 2: return

        time_now, time_prev = times[0][0], times[1][0]
        
        fmt = '%Y-%m-%d %H:%M:%S'
        t1 = datetime.datetime.strptime(time_now, fmt)
        t2 = datetime.datetime.strptime(time_prev, fmt)
        minutes_diff = (t1 - t2).total_seconds() / 60
        if minutes_diff <= 0: return

        sql = '''
            SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp 
            FROM exp_history t1
            JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
            WHERE t1.record_time = ? AND t2.record_time = ?
        '''
        self.cursor.execute(sql, (time_now, time_prev))
        records = self.cursor.fetchall()
        
        alert_list = []
        for name, server, level, exp_now, exp_prev in records:
            diff = exp_now - exp_prev
            if diff > 0:
                hourly_speed = (diff / minutes_diff) * 60
                speed_yi = hourly_speed / 100_000_000 
                
                if speed_yi >= self.SPEED_LIMIT:
                    alert_list.append({
                        "name": name, "server": server, "level": level, "speed": speed_yi
                    })

        if alert_list:
            alert_list.sort(key=lambda x: x['speed'], reverse=True)
            channel = self.bot.get_channel(self.ALERT_CHANNEL_ID)
            if channel:
                embed = discord.Embed(title="🚨 偵測到練功超速玩家！", color=0xff0000)
                desc = "以下玩家時速超過 4000 億，可能正在強力衝等：\n```yaml\n"
                for p in alert_list:
                    name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in p['name'])
                    name_padded = p['name'] + " " * max(0, 14 - name_width)
                    desc += f"[{p['server']}] {name_padded} | Lv.{p['level']} | 時速: {p['speed']:,.0f}億\n"
                desc += "```"
                embed.description = desc
                embed.set_footer(text=f"掃描時間: {time_now} (監控週期: {int(minutes_diff)}min)")
                await channel.send(embed=embed)

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()

    # 🎛️ 【新增指令】雷達警報開關
    @commands.command(name="警報", help="開啟或關閉自動超速警報 (用法: !警報 開 或 !警報 關)")
    async def toggle_alerts(self, ctx, state: str = None):
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids: 
            return await ctx.send(f"❌ 頻道未授權，此頻道 ID 為：{ctx.channel.id}")

        if state == "開" or state == "on":
            self.alerts_enabled = True
            await ctx.send("🚨 **【自動超速警報】已開啟！** \n系統將每 10 分鐘巡邏一次，有人飆車破 4000 億就會立刻通報。")
        elif state == "關" or state == "off":
            self.alerts_enabled = False
            await ctx.send("🔕 **【自動超速警報】已關閉！** \n雷達已切換為「靜默收集模式」，不會再吵你，但你隨時可輸入 `!測速` 調閱資料。")
        else:
            current_state = "🟢 開啟中 (會推播)" if self.alerts_enabled else "🔴 關閉中 (靜默模式)"
            await ctx.send(f"目前警報狀態為：**{current_state}**\n👉 請輸入 `!警報 開` 或 `!警報 關` 來切換。")

    # 📊 手動測速指令
    @commands.command(name="測速", help="用法: !測速 全服 或 !測速 50 萊涅01")
    async def check_exp_speed(self, ctx, *args):
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids: 
            return await ctx.send(f"❌ 頻道未授權，此頻道 ID 為：{ctx.channel.id}")

        self.setup_database()
        count = 15 
        args_list = list(args)
        
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 100: count = 100
        if count < 1: count = 10

        target_server = "".join(args_list) if args_list else "全服"
        is_global = target_server in ["全服", "全部", "global"]

        if not is_global and target_server not in SERVER_MAP:
            valid_list = "、".join(SERVER_MAP.keys())
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")

        processing_msg = await ctx.send(f"📡 正在調閱測速照相機，計算 {'全台服' if is_global else target_server} 練功時速 TOP {count}...")

        self.cursor.execute('SELECT DISTINCT record_time FROM exp_history ORDER BY record_time DESC LIMIT 2')
        times = self.cursor.fetchall()

        if len(times) < 2:
            return await processing_msg.edit(content="⚠️ 樣本不足！巡邏兵才剛啟動，請等待至少 10 分鐘。")

        time_now, time_prev = times[0][0], times[1][0]
        
        fmt = '%Y-%m-%d %H:%M:%S'
        t1 = datetime.datetime.strptime(time_now, fmt)
        t2 = datetime.datetime.strptime(time_prev, fmt)
        minutes_diff = (t1 - t2).total_seconds() / 60
        if minutes_diff <= 0: minutes_diff = 10 

        sql = '''
            SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp 
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
                hourly_speed = (diff / minutes_diff) * 60
                speed_data.append({"name": name, "server": server, "level": level, "speed": hourly_speed})
        
        speed_data.sort(key=lambda x: x['speed'], reverse=True)
        top_list = speed_data[:count] 

        if not top_list:
            return await processing_msg.edit(content="💤 大家都沒在練功，或資料抓取空隙中。")

        desc = f"**區間：{time_prev[11:16]} ➡️ {time_now[11:16]} (約 {int(minutes_diff)} 分鐘)**\n```yaml\n"
        embeds = [] 
        
        for idx, p in enumerate(top_list, 1):
            speed_yi = p['speed'] / 100_000_000 
            name = str(p['name'])
            name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in name)
            name_padded = name + " " * max(0, 14 - name_width)
            
            srv_info = f"({p['server']})" if is_global else ""
            line = f"{idx:02d}. {name_padded} | Lv.{p['level']:<2} | 時速:{speed_yi:>6.2f}億 {srv_info}\n"
            
            if len(desc) + len(line) > 1900:
                desc += "```"
                embed = discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 (續)", description=desc, color=0x00ff00)
                embeds.append(embed)
                desc = "```yaml\n" 
                
            desc += line
            
        if desc != "```yaml\n":
            desc += "```"
            embed = discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 TOP {count}", description=desc, color=0x00ff00)
            embed.set_footer(text="系統：全自動經驗值測速雷達 | 單位：時速/億")
            embeds.append(embed)

        await processing_msg.delete()
        for e in embeds: 
            await ctx.send(embed=e)

    # 🕵️ 【終極指令】跨服改名專用雷達 (支援指定伺服器 + 全服排名鎖定)
    @commands.command(name="抓改名", help="用法: !抓改名 \"2026-05-10 00:30\" \"2026-05-10 01:50\" [舊伺服器] [新伺服器]")
    async def catch_name_changers(self, ctx, date1: str, date2: str, srv1: str = None, srv2: str = None):
        target_msg = "全伺服器"
        if srv1 and srv2:
            target_msg = f"「{srv1}」與「{srv2}」之間"
        elif srv1:
            target_msg = f"包含「{srv1}」"
            
        processing_msg = await ctx.send(f"🕵️ 啟動天眼系統，正在鎖定分析 {target_msg} 的跨服跳槽數據...")
        self.setup_database()

        def get_snapshot(date_str):
            if len(date_str) <= 10: target_time = f"{date_str} 23:59:59"
            elif len(date_str) == 13: target_time = f"{date_str}:59:59"
            elif len(date_str) == 16: target_time = f"{date_str}:00"
            else: target_time = date_str

            self.cursor.execute('''
                SELECT DISTINCT record_time 
                FROM exp_history 
                ORDER BY ABS(CAST(strftime('%s', record_time) AS INTEGER) - CAST(strftime('%s', ?) AS INTEGER)) ASC 
                LIMIT 1
            ''', (target_time,))
            
            time_row = self.cursor.fetchone()
            if not time_row: return None, None
            exact_time = time_row[0]
            
            # ✨ 關鍵修復：依據經驗值進行「全服大排名」，全服排名是跳服時最穩定的錨點！
            self.cursor.execute('''
                SELECT player_name, server_name, level, exp 
                FROM exp_history 
                WHERE record_time = ?
                ORDER BY exp DESC
            ''', (exact_time,))
            rows = self.cursor.fetchall()
            
            snapshot = {}
            for global_rank, r in enumerate(rows, 1):
                snapshot[r[0]] = {'server': r[1], 'level': r[2], 'exp': r[3], 'rank': global_rank}
            return snapshot, exact_time

        try:
            old_data, exact_time1 = get_snapshot(date1)
            new_data, exact_time2 = get_snapshot(date2)

            if not old_data or not new_data:
                return await processing_msg.edit(content=f"❌ 找不到資料。資料庫目前可能為空，或日期格式錯誤。")

            missing_players = set(old_data.keys()) - set(new_data.keys())
            appeared_players = set(new_data.keys()) - set(old_data.keys())

            suspects = []
            matched_new_names = set()

            for old_name in missing_players:
                old_info = old_data[old_name]
                best_match = None
                highest_confidence = 0
                closest_exp_diff = float('inf')
                
                for new_name in appeared_players:
                    if new_name in matched_new_names: continue
                    new_info = new_data[new_name]
                    
                    # 🚫 規則 1：遊戲機制不存在同服改名，如果伺服器一樣直接跳過
                    if old_info['server'] == new_info['server']:
                        continue
                        
                    # 🎯 規則 2：如果有輸入指定伺服器，只抓這兩個伺服器之間的流動
                    if srv1 and srv2:
                        valid_jumps = [(srv1, srv2), (srv2, srv1)]
                        if (old_info['server'], new_info['server']) not in valid_jumps:
                            continue
                    elif srv1:
                        if old_info['server'] != srv1 and new_info['server'] != srv1:
                            continue

                    if new_info['level'] not in (old_info['level'], old_info['level'] + 1):
                        continue

                    exp_diff = new_info['exp'] - old_info['exp']
                    rank_diff = abs(old_info['rank'] - new_info['rank'])
                    
                    confidence = 0
                    match_type = ""

                    # 判斷 A：精準跳服 (經驗合理微增 0~10兆)
                    if 0 <= exp_diff <= 10000000000000:
                        confidence = 3
                        match_type = "🟡 精準跳服 (經驗微增)"
                    # 判斷 B：死亡掉趴或贖回，但「全服總排名」死死咬住 (正負 5 名內)
                    elif rank_diff <= 5 and abs(exp_diff) <= 150000000000000:
                        confidence = 2
                        match_type = "🔴 幽靈跳服 (全服排名鎖定)"
                    # 判斷 C：排名稍微被擠走，但經驗還是相近 (-10兆 ~ +20兆)
                    elif -10000000000000 <= exp_diff <= 20000000000000:
                        confidence = 1
                        match_type = "🟠 疑似跳服 (經驗相近)"

                    if confidence > highest_confidence or (confidence == highest_confidence and abs(exp_diff) < abs(closest_exp_diff)):
                        highest_confidence = confidence
                        closest_exp_diff = exp_diff
                        best_match = {
                            'type': match_type,
                            'old_name': old_name, 'new_name': new_name,
                            'old_server': old_info['server'], 'new_server': new_info['server'],
                            'old_rank': old_info['rank'], 'new_rank': new_info['rank'],
                            'old_level': old_info['level'], 'new_level': new_info['level'],
                            'exp_diff': exp_diff
                        }
                            
                if best_match:
                    suspects.append(best_match)
                    matched_new_names.add(best_match['new_name'])

            if not suspects:
                return await processing_msg.edit(content=f"✅ 比對完畢：在 {target_msg} 中沒有發現跨服改名跡象。")

            CHUNK_SIZE = 15
            chunks = [suspects[i:i + CHUNK_SIZE] for i in range(0, len(suspects), CHUNK_SIZE)]
            
            embeds = []
            for idx, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"🚨 跨服改名雷達追蹤報告 ({idx+1}/{len(chunks)})", 
                    description=f"✅ **快照時間：**\n`{exact_time1}` ➡️ `{exact_time2}`\n🎯 **追蹤範圍：**{target_msg}", 
                    color=0xe74c3c
                )
                
                for s in chunk:
                    detail = f"全服排名: {s['old_rank']} ➡️ {s['new_rank']} | 波動: {s['exp_diff']/1000000000000:+.2f} 兆"
                    if s['exp_diff'] < -5000000000000: detail += " 📉(疑似掉趴)"
                    elif s['exp_diff'] > 50000000000000: detail += " 📈(疑似贖回)"
                        
                    lv_text = f"Lv.{s['old_level']}" if s['old_level'] == s['new_level'] else f"Lv.{s['old_level']} ➡️ {s['new_level']}"
                        
                    info = (f"**[舊]** `{s['old_name']}` ({s['old_server']})\n"
                            f"**[新]** `{s['new_name']}` ({s['new_server']})\n"
                            f"{lv_text} | {detail}")
                    embed.add_field(name=f"{s['type']}", value=info, inline=False)
                    
                embeds.append(embed)

            try: await processing_msg.delete()
            except: pass
            for e in embeds: await ctx.send(embed=e)

        except Exception as e:
            await ctx.send(f"❌ 系統錯誤: {e}")

# ⚠️ 這是整份檔案的最後幾行，負責掛載模組
async def setup(bot):
    await bot.add_cog(ExpTracker(bot))