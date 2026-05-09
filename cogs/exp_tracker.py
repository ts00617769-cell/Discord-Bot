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

    # 🕵️ 【終極指令】抓改名與跳服雷達 (自帶防爆分頁系統 + 無視死亡掉趴)
    @commands.command(name="抓改名", help="比對兩個時間點的榜單，抓出改名或跳服的玩家。用法: !抓改名 2026-05-06 2026-05-07")
    async def catch_name_changers(self, ctx, date1: str, date2: str):
        # allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        # if ctx.channel.id not in allowed_channel_ids: return 

        processing_msg = await ctx.send(f"🕵️ 啟動天眼系統，正在交叉比對 `{date1}` 與 `{date2}` 的全服數據 (已啟用死亡掉趴容錯機制)...")
        self.setup_database()

        def get_snapshot(date_str):
            self.cursor.execute('''
                SELECT player_name, server_name, level, exp 
                FROM exp_history 
                WHERE record_time LIKE ? 
                GROUP BY player_name
                HAVING record_time = MAX(record_time)
            ''', (f'{date_str}%',))
            return {row[0]: {'server': row[1], 'level': row[2], 'exp': row[3]} for row in self.cursor.fetchall()}

        try:
            old_data = get_snapshot(date1)
            new_data = get_snapshot(date2)

            if not old_data or not new_data:
                return await processing_msg.edit(content=f"❌ 找不到 `{date1}` 或 `{date2}` 的完整資料。")

            old_names = set(old_data.keys())
            new_names = set(new_data.keys())

            missing_players = old_names - new_names 
            appeared_players = new_names - old_names 

            suspects = []
            
            # 用來記錄已經被配對走的新名字，避免一個人被重複配對
            matched_new_names = set()

            for old_name in missing_players:
                old_info = old_data[old_name]
                
                best_match = None
                min_abs_diff = float('inf') # 記錄最小的絕對誤差
                
                for new_name in appeared_players:
                    if new_name in matched_new_names:
                        continue # 已經被配對走的就跳過
                        
                    new_info = new_data[new_name]
                    exp_diff = new_info['exp'] - old_info['exp'] # 可正可負
                    abs_diff = abs(exp_diff) # 絕對距離
                    
                    # ✨ 核心升級：等級相同(或剛好升一級)，且經驗值浮動在「正負 200 兆」以內 (包容死亡掉 5% 或鑽石贖回)
                    if new_info['level'] in (old_info['level'], old_info['level'] + 1) and abs_diff <= 200000000000000:
                        # 誰的絕對誤差最小，誰就是真兇！
                        if abs_diff < min_abs_diff:
                            min_abs_diff = abs_diff
                            best_match = {
                                'old_name': old_name,
                                'new_name': new_name,
                                'old_server': old_info['server'],
                                'new_server': new_info['server'],
                                'level': new_info['level'],
                                'exp_diff': exp_diff # 保留正負號，才能知道他是死了還是贖回
                            }
                            
                if best_match:
                    suspects.append(best_match)
                    matched_new_names.add(best_match['new_name']) # 標記這個新名字已經死會了

            if not suspects:
                return await processing_msg.edit(content=f"✅ 比對完畢：在 `{date1}` 與 `{date2}` 之間沒有發現明顯的改名或跳服跡象。")

            # 🛡️ 解決 6000 字元與 25 欄位限制：將名單分組 (每 15 人一頁)
            CHUNK_SIZE = 15
            chunks = [suspects[i:i + CHUNK_SIZE] for i in range(0, len(suspects), CHUNK_SIZE)]
            
            embeds = []
            for idx, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"🚨 戰情雷達：改名與跳服追蹤報告 ({idx+1}/{len(chunks)})", 
                    description=f"比對區間：`{date1}` ➡️ `{date2}`\n追蹤原理：最小經驗值誤差配對 (支援死亡掉趴偵測)", 
                    color=0xe74c3c
                )
                
                for s in chunk:
                    action_text = "🔄 單純改名" if s['old_server'] == s['new_server'] else "✈️ 跳服＋改名"
                    
                    # 判斷狀態給予不同表情符號
                    if s['exp_diff'] < 0:
                        status_emoji = "📉 (疑似死亡掉趴)"
                    elif s['exp_diff'] > 30000000000000: # 如果突然暴增超過 30 兆
                        status_emoji = "📈 (疑似花鑽贖回)"
                    else:
                        status_emoji = "📊"
                        
                    info = (f"**[舊]** `{s['old_name']}` ({s['old_server']})\n"
                            f"**[新]** `{s['new_name']}` ({s['new_server']})\n"
                            f"Lv.{s['level']} | 經驗浮動: {s['exp_diff']/1000000000000:+.2f} 兆 {status_emoji}")
                    embed.add_field(name=f"{action_text}", value=info, inline=False)
                    
                embeds.append(embed)

            try:
                await processing_msg.delete()
            except:
                pass
                
            for e in embeds:
                await ctx.send(embed=e)

        except Exception as e:
            await ctx.send(f"❌ 系統錯誤: {e}")

# ⚠️ 這是整份檔案的最後幾行，負責掛載模組
async def setup(bot):
    await bot.add_cog(ExpTracker(bot))