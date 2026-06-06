import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import datetime
import unicodedata
from game_data import SERVER_MAP
import os
import logging

logger = logging.getLogger(__name__)

class ExpTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ALERT_CHANNEL_ID = int(os.getenv("EXP_ALERT_CHANNEL_ID", 0))
        self.TRANSFER_ALERT_CHANNEL_ID = int(os.getenv("TRANSFER_ALERT_CHANNEL_ID", 0))
        self.SPEED_LIMIT = 4000 
        self.alerts_enabled = False 
        # 注意：__init__ 是同步的，不能在這裡執行 await，所以資料庫初始化移到 cog_load

    # ✨ discord.py 提供的非同步初始化入口
    async def cog_load(self):
        await self.setup_database()
        self.auto_fetch_exp.start()

    def cog_unload(self):
        self.auto_fetch_exp.cancel()
        # 不需要再關閉 db_conn，因為 bot.py 關閉時會統一處理

    async def setup_database(self):
        """非同步初始化資料庫"""
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS exp_history (
                record_time TIMESTAMP,
                server_name TEXT,
                player_name TEXT,
                level INTEGER,
                exp REAL
            )
        ''')
        
        # 檢查欄位是否存在 (使用 sqlite 原生 pragma 比較安全)
        async with self.bot.db.execute("PRAGMA table_info(exp_history)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if 'class_name' not in columns:
                await self.bot.db.execute("ALTER TABLE exp_history ADD COLUMN class_name TEXT DEFAULT '未知'")
            if 'subjugation_grade' not in columns:
                await self.bot.db.execute("ALTER TABLE exp_history ADD COLUMN subjugation_grade INTEGER DEFAULT 0")
                
        await self.bot.db.execute('CREATE INDEX IF NOT EXISTS idx_time_server ON exp_history(record_time, server_name)')

        # ✨ 新增：用於記錄已發送過轉移警報的資料表，防止無限洗頻
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS transfer_alerts_log (
                old_name TEXT,
                old_server TEXT,
                new_name TEXT,
                new_server TEXT,
                alert_time TIMESTAMP,
                PRIMARY KEY (old_name, old_server, new_name, new_server)
            )
        ''')

        await self.bot.db.commit()

    async def get_member_info(self, name):
        """改為非同步的讀取標記函數"""
        try:
            await self.bot.db.execute('''
                CREATE TABLE IF NOT EXISTS member_registry (
                    player_name TEXT PRIMARY KEY,
                    original_identity TEXT
                )
            ''')
            await self.bot.db.commit()
            
            async with self.bot.db.execute('SELECT original_identity FROM member_registry WHERE player_name = ?', (name,)) as cursor:
                result = await cursor.fetchone()
                return f"({result[0]})" if result else ""
        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while fetching member info for '{name}': {e}")
            return ""
        except Exception as e:
            logger.error(f"Unexpected error while fetching member info for '{name}': {e}")
            return ""

    async def fetch_server_data(self, session, group_id, world_id):
        # (保持原樣，這本來就是非同步的)
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://warsofprasia.beanfun.com",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }
        try:
            async with session.post(api_url, json=payload, headers=headers, timeout=10) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("gc", [])[:100]
        except asyncio.TimeoutError as e:
            logger.error(f"API timeout while fetching data for group {group_id}, world {world_id}: {e}")
        except aiohttp.ClientError as e:
            logger.error(f"HTTP client error while fetching server data: {e}")
        except ValueError as e:
            logger.error(f"JSON parsing error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while fetching server data: {e}")
        return []

    @tasks.loop(minutes=10.0)
    async def auto_fetch_exp(self):
        try:
            now_time = datetime.datetime.now().replace(second=0, microsecond=0)
            logger.info(f"[{now_time.strftime('%H:%M:%S')}] 哨兵出動：掃描全服前100名...")
            
            # 👇 直接移除 async with aiohttp.ClientSession() 的區塊，改用 self.bot.session
            for server_name, (g_id, w_id) in SERVER_MAP.items():
                try:
                    # 這裡傳入 self.bot.session
                    players = await self.fetch_server_data(self.bot.session, g_id, w_id) 

                    for p in players:
                        # 擷取討伐等級
                        grade_val = (p.get("string_map") or {}).get("grade", "0")
                        try:
                            grade = int(grade_val)
                        except (ValueError, TypeError):
                            grade = 0

                        await self.bot.db.execute('''
                            INSERT INTO exp_history (record_time, server_name, player_name, level, exp, class_name, subjugation_grade)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (now_time, server_name, p.get('gc_name'), p.get('gc_level'), p.get('gc_exp', 0), p.get('class_name', '未知'), grade))
                    await self.bot.db.commit()
                except Exception as e:
                    logger.error(f"Error processing server {server_name}: {e}")
                    continue
                await asyncio.sleep(0.5)
            
            await self.check_for_alerts(now_time)
        except Exception as e:
            logger.error(f"🚨 [經驗值雷達] 發生未預期錯誤，已攔截以防崩潰：{e}")

    async def check_for_alerts(self, current_time):
        # 確保挑選出的時間是「已完成全服抓取」的時間 (避免抓取空隙導致誤判)
        sql_times = '''
            SELECT record_time
            FROM exp_history
            GROUP BY record_time
            HAVING COUNT(DISTINCT server_name) >= 4
            ORDER BY record_time DESC LIMIT 2
        '''
        async with self.bot.db.execute(sql_times) as cursor:
            times = await cursor.fetchall()
            
        if len(times) < 2: return
        time_now, time_prev = times[0][0], times[1][0]
        
        fmt = '%Y-%m-%d %H:%M:%S'
        t1 = datetime.datetime.strptime(time_now, fmt)
        t2 = datetime.datetime.strptime(time_prev, fmt)
        minutes_diff = (t1 - t2).total_seconds() / 60
        if minutes_diff <= 0: return

        if self.alerts_enabled:
            sql = '''
                SELECT DISTINCT t1.player_name, t1.server_name, t1.level, t1.exp, t2.exp
                FROM exp_history t1
                JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
                WHERE t1.record_time = ? AND t2.record_time = ?
            '''
            async with self.bot.db.execute(sql, (time_now, time_prev)) as cursor:
                records = await cursor.fetchall()

            # 超速警報邏輯
            alert_list = []
            for name, server, level, exp_now, exp_prev in records:
                diff = exp_now - exp_prev
                if diff > 0:
                    hourly_speed = (diff / minutes_diff) * 60
                    speed_yi = hourly_speed / 100_000_000
                    if speed_yi >= self.SPEED_LIMIT:
                        alert_list.append({"name": name, "server": server, "level": level, "speed": speed_yi})

            if alert_list:
                alert_list.sort(key=lambda x: x['speed'], reverse=True)
                channel = self.bot.get_channel(self.ALERT_CHANNEL_ID)
                if channel:
                    embed = discord.Embed(title="🚨 偵測到練功超速玩家！", color=0xff0000)
                    desc = f"以下玩家時速超過 {self.SPEED_LIMIT} 億，可能正在強力衝等：\n```yaml\n"
                    for p in alert_list:
                        name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in p['name'])
                        name_padded = p['name'] + " " * max(0, 14 - name_width)
                        desc += f"[{p['server']}] {name_padded} | Lv.{p['level']} | 時速: {p['speed']:,.0f}億\n"
                    desc += "```"
                    embed.description = desc
                    embed.set_footer(text=f"掃描時間: {time_now} (監控週期: {int(minutes_diff)}min)")
                    await channel.send(embed=embed)

        # ✨ 新增：自動轉服/改名偵測
        await self.check_for_transfers(time_now, time_prev)

    async def check_for_transfers(self, time_now, time_prev):
        """自動偵測轉服與改名 (V4 雙引擎升級版)"""
        try:
            # 容許的經驗值偷練誤差 (1 兆以內)
            EXP_MARGIN = 1.0 * 1000000000000

            # 1. 找出嫌疑人 (經驗值 > 1兆)
            # 條件：
            # - 新紀錄的經驗值大於等於舊紀錄的經驗值，且差距在 1 兆以內 (無縫接軌/偷練)
            # - 職業必須相同 (不可能轉服變換職業)
            # - 等級大於等於舊等級 (等級不可能倒退)
            # - 討伐等級大於等於舊討伐等級 (討伐不會因為轉服而退步)
            # - 名字或伺服器其中一項不同
            # - ✨ 確保 t_now 是新面孔（他在 time_prev 的名單中並不存在同伺服器同名字的紀錄）
            sql = '''
                SELECT DISTINCT t_now.exp, t_now.player_name, t_now.server_name, t_now.level, t_now.class_name,
                                t_old.player_name, t_old.server_name, t_old.level, t_old.class_name,
                                t_old.exp, t_now.subjugation_grade
                FROM exp_history t_now
                JOIN exp_history t_old ON t_now.class_name = t_old.class_name
                WHERE t_now.record_time = ? AND t_now.exp > 1000000000000
                  AND t_now.level >= t_old.level
                  AND t_now.subjugation_grade >= t_old.subjugation_grade
                  AND t_now.exp >= t_old.exp AND t_now.exp <= (t_old.exp + ?)
                  AND (t_now.player_name != t_old.player_name OR t_now.server_name != t_old.server_name)
                  AND t_old.record_time <= ? AND t_old.record_time >= datetime(?, '-7 days')
                  AND NOT EXISTS (
                      SELECT 1 FROM exp_history t_check
                      WHERE t_check.record_time = ?
                        AND t_check.player_name = t_now.player_name
                        AND t_check.server_name = t_now.server_name
                  )
            '''
            async with self.bot.db.execute(sql, (time_now, EXP_MARGIN, time_prev, time_prev, time_prev)) as cursor:
                transfer_records = await cursor.fetchall()

            if not transfer_records:
                return

            channel = self.bot.get_channel(self.TRANSFER_ALERT_CHANNEL_ID)
            if not channel:
                return

            # 過濾並整理報告
            reported_pairs = set()
            for row in transfer_records:
                new_exp = row[0]
                new_name, new_server, new_lvl, new_cls = row[1], row[2], row[3], row[4]
                old_name, old_server, old_lvl, old_cls = row[5], row[6], row[7], row[8]
                old_exp = row[9]
                new_sub_grade = row[10]

                # 防止單次掃描中重複推播
                pair_key = (old_name, old_server, new_name, new_server)
                if pair_key in reported_pairs:
                    continue

                # 🛡️ 防無限洗頻：檢查是否在資料庫中已經報過了
                async with self.bot.db.execute('''
                    SELECT 1 FROM transfer_alerts_log
                    WHERE old_name = ? AND old_server = ? AND new_name = ? AND new_server = ?
                ''', pair_key) as check_log_cursor:
                    already_alerted = await check_log_cursor.fetchone()

                if already_alerted:
                    continue

                # 🛡️ 最終防呆：確定舊名字在 time_now 真的「從榜單上消失了」
                # 如果舊名字還在現在的榜單上，代表這只是巧合 (例如兩人剛好練到同一門檻)
                async with self.bot.db.execute('SELECT 1 FROM exp_history WHERE record_time = ? AND player_name = ? AND server_name = ?', (time_now, old_name, old_server)) as check_cursor:
                    is_old_still_active = await check_cursor.fetchone()

                if not is_old_still_active:
                    reported_pairs.add(pair_key)

                    # 將這筆發送過的紀錄存入資料庫
                    await self.bot.db.execute('''
                        INSERT INTO transfer_alerts_log (old_name, old_server, new_name, new_server, alert_time)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (old_name, old_server, new_name, new_server, time_now))
                    await self.bot.db.commit()

                    # 判斷狀態
                    status_str = "跨服轉移並改名"
                    if new_name == old_name:
                        status_str = "跨服轉移"
                    elif new_server == old_server:
                        status_str = "原地改名"

                    # 計算經驗變動
                    exp_diff = new_exp - old_exp
                    if exp_diff == 0:
                        diff_str = "+0.00% (完美吻合)"
                    else:
                        diff_str = f"+{(exp_diff/100000000):,.0f} 億 (轉移期間偷練)"

                    embed = discord.Embed(
                        title="【波拉西亞戰記】轉移/旅團變動警報",
                        description=f"時間：{time_now}\n{'-'*30}\n"
                                    f"✨ [即時轉移辨識] **{old_name}** ({old_server}) ➔\n"
                                    f"**{new_name}** ({new_server})\n"
                                    f"[狀態]: {status_str} | [EXP變動]: {diff_str}\n"
                                    f"[屬性]: Lv.{new_lvl} / {new_cls} / 討伐 {new_sub_grade}",
                        color=0xf1c40f
                    )
                    await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in automatic transfer check: {e}")

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()

        # 為了配合官方 10, 20, 30... 更新，我們將啟動時間對齊到下個 10 分鐘，並加上 30 秒的緩衝時間
        # (例如 10:00:30, 10:10:30...)
        now = datetime.datetime.now()
        # 算出下一個 10 分鐘的時間點
        next_run = now.replace(minute=(now.minute // 10) * 10, second=30, microsecond=0) + datetime.timedelta(minutes=10)
        # 如果當前時間才剛過 10 的倍數（例如 10:00:15），next_run 會算到 10:10:30。如果我們想要在當下的 10:00:30 執行，就可以減少 10 分鐘。
        # 這裡我們統一使用安全的等待方式
        if now.minute % 10 == 0 and now.second < 30:
             next_run = now.replace(second=30, microsecond=0)

        seconds_to_wait = (next_run - now).total_seconds()
        if seconds_to_wait > 0:
            logger.info(f"等待 {seconds_to_wait:.1f} 秒以對齊官方每 10 分鐘更新時間...")
            await asyncio.sleep(seconds_to_wait)

    @commands.command(name="警報", help="開啟或關閉自動超速警報 (用法: !警報 開 或 !警報 關)")
    async def toggle_alerts(self, ctx, state: str = None):
        # ... 保持原樣 ...
        if state in ["開", "on"]:
            self.alerts_enabled = True
            await ctx.send(f"🚨 **【自動超速警報】已開啟！** (門檻: {self.SPEED_LIMIT}億)")
        elif state in ["關", "off"]:
            self.alerts_enabled = False
            await ctx.send("🔕 **【自動超速警報】已關閉！**")
        else:
            current_state = "🟢 開啟中" if self.alerts_enabled else "🔴 關閉中"
            await ctx.send(f"目前警報狀態為：**{current_state}**\n👉 請輸入 `!警報 開` 或 `!警報 關` 切換。")

    @commands.command(name="測速", help="用法: !測速 全服 或 !測速 50 萊涅01")
    async def check_exp_speed(self, ctx, *args):
        # 移除原有的 self.setup_database() (因為已經在 cog_load 執行過了)
        count = 15 
        args_list = list(args)
        if len(args_list) > 0 and args_list[0].isdigit(): count = int(args_list.pop(0))
        if count > 100: count = 100
        if count < 1: count = 10

        target_server = "".join(args_list) if args_list else "全服"
        is_global = target_server in ["全服", "全部", "global"]

        if not is_global and target_server not in SERVER_MAP:
            valid_list = "、".join(SERVER_MAP.keys())
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

        processing_msg = await ctx.send(f"📡 正在調閱測速照相機，計算 {'全台服' if is_global else target_server} 練功時速 TOP {count}...")

        if is_global:
            # 針對全服測速，確保挑選出的時間是「已完成全服抓取」的時間 (避免抓取空隙)
            sql_times = '''
                SELECT record_time
                FROM exp_history
                GROUP BY record_time
                HAVING COUNT(DISTINCT server_name) >= 4
                ORDER BY record_time DESC LIMIT 2
            '''
            params_times = []
        else:
            sql_times = 'SELECT DISTINCT record_time FROM exp_history WHERE server_name = ? ORDER BY record_time DESC LIMIT 2'
            params_times = [target_server]

        async with self.bot.db.execute(sql_times, tuple(params_times)) as cursor:
            times = await cursor.fetchall()
            
        if len(times) < 2: return await processing_msg.edit(content="⚠️ 樣本不足！請等待至少 10 分鐘。")

        time_now, time_prev = times[0][0], times[1][0]
        fmt = '%Y-%m-%d %H:%M:%S'
        t1, t2 = datetime.datetime.strptime(time_now, fmt), datetime.datetime.strptime(time_prev, fmt)
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

        async with self.bot.db.execute(sql, tuple(params)) as cursor:
            speed_records = await cursor.fetchall()
            
        speed_data = []
        for name, server, level, exp_now, exp_prev in speed_records:
            diff = exp_now - exp_prev
            if diff > 0:
                speed_data.append({"name": name, "server": server, "level": level, "speed": (diff / minutes_diff) * 60})
        
        # ... (排版邏輯保持原樣，沒有 SQL 變動) ...
        speed_data.sort(key=lambda x: x['speed'], reverse=True)
        top_list = speed_data[:count] 

        if not top_list: return await processing_msg.edit(content="💤 大家都沒在練功，或資料抓取空隙中。")

        desc = f"**區間：{time_prev[11:16]} ➡️ {time_now[11:16]} (約 {int(minutes_diff)} 分鐘)**\n```yaml\n"
        embeds = [] 
        for idx, p in enumerate(top_list, 1):
            name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(p['name']))
            name_padded = str(p['name']) + " " * max(0, 14 - name_width)
            srv_info = f"({p['server']})" if is_global else ""
            line = f"{idx:02d}. {name_padded} | Lv.{p['level']:<2} | 時速:{p['speed']/100000000:>6.2f}億 {srv_info}\n"
            
            if len(desc) + len(line) > 1900:
                desc += "```"
                embeds.append(discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 (續)", description=desc, color=0x00ff00))
                desc = "```yaml\n" 
            desc += line
            
        if desc != "```yaml\n":
            desc += "```"
            embed = discord.Embed(title=f"🏎️ {'全台服' if is_global else target_server} 練功時速 TOP {count}", description=desc, color=0x00ff00)
            embed.set_footer(text="系統：全自動經驗值測速雷達")
            embeds.append(embed)

        await processing_msg.delete()
        for e in embeds: await ctx.send(embed=e)

    @commands.command(name="星光點名", help="檢驗 23:00~23:30 點名 (預設今日。查歷史用法: !星光點名 2026-05-05)")
    async def starlight_attendance(self, ctx, target_date: str = None):
        tz = datetime.timezone(datetime.timedelta(hours=8))
        if target_date:
            query_date, display_date = target_date, f"歷史調閱 ({target_date})"
        else:
            query_date, display_date = datetime.datetime.now(tz).strftime('%Y-%m-%d'), "今日"

        processing_msg = await ctx.send(f"🛰️ 啟動星光點名系統，正在掃描 **全伺服器** {display_date} 23:00 ~ 23:30...")
        
        async with self.bot.db.execute('''
            SELECT MIN(record_time), MAX(record_time) FROM exp_history 
            WHERE record_time >= ? AND record_time <= ?
        ''', (f"{query_date} 23:00:00", f"{query_date} 23:30:00")) as cursor:
            times = await cursor.fetchone()
        
        if not times or not times[0] or not times[1] or times[0] == times[1]:
            return await processing_msg.edit(content=f"❌ 找不到 {query_date} 23:00 ~ 23:30 的資料。")
            
        start_time, end_time = times[0], times[1]
        fmt = '%Y-%m-%d %H:%M:%S'
        minutes_diff = (datetime.datetime.strptime(end_time, fmt) - datetime.datetime.strptime(start_time, fmt)).total_seconds() / 60
        if minutes_diff <= 0: minutes_diff = 30 

        async with self.bot.db.execute('''
            SELECT t1.server_name, t1.player_name, (t2.exp - t1.exp)
            FROM exp_history t1 JOIN exp_history t2 ON t1.player_name = t2.player_name AND t1.server_name = t2.server_name
            WHERE t1.record_time = ? AND t2.record_time = ?
        ''', (start_time, end_time)) as cursor:
            records = await cursor.fetchall()
        
        results = {}
        for server, player, diff in records:
            if diff > 0:
                hourly_speed = (diff / minutes_diff) * 60
                if 100000000000 <= hourly_speed <= 1500000000000:
                    if server not in results: results[server] = []
                    results[server].append((player, hourly_speed / 100000000))
                
        embed = discord.Embed(
            title=f"✨ 星光解放戰 全服出席 {display_date}", 
            description=f"📊 **採樣**：`{start_time[11:16]}` ~ `{end_time[11:16]}`\n🎯 **門檻**：時速 1000億 ~ 1.5兆", 
            color=0xf1c40f
        )
        
        if not results:
            embed.add_field(name="狀態報告", value="全服無人符合時速條件。", inline=False)
        else:
            for server in sorted(results.keys()):
                players = sorted(results[server], key=lambda x: x[1], reverse=True)
                lines = []
                for p_name, p_speed in players:
                    name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in str(p_name))
                    lines.append(f"• {str(p_name) + ' ' * max(0, 14 - name_width)} (時速 {p_speed:,.0f}億)")
                    
                full_text = "\n".join(lines)
                if len(full_text) > 900: full_text = full_text[:900] + "\n... (截斷)"
                embed.add_field(name=f"🌐 {server} (共 {len(players)} 人)", value=f"```yaml\n{full_text}\n```", inline=False)
            
        await processing_msg.delete()
        await ctx.send(embed=embed)

    @commands.command(name="歷史排名", aliases=["查歷史", "歷史"], help="查詢過去的資料庫排名。用法: !歷史排名 100 2026-05-08 萊涅04 太陽監視者")
    async def historical_ranking(self, ctx, *args):
        count = 100
        tz = datetime.timezone(datetime.timedelta(hours=8))
        date_str = datetime.datetime.now(tz).strftime('%Y-%m-%d')
        target_server = "全服"
        target_class = None
        
        args_list = list(args)
        class_parts = []
        
        for arg in args_list:
            if arg.isdigit():
                count = int(arg)
            elif "-" in arg and len(arg) >= 8:
                date_str = arg
            elif arg in SERVER_MAP.keys() or arg in ["全服", "全部", "global"]:
                target_server = arg
            else:
                class_parts.append(arg)
                
        if class_parts:
            target_class = "".join(class_parts)

        is_global = target_server in ["全服", "全部", "global"]
        if not is_global and target_server not in SERVER_MAP:
            return await ctx.send(f"❌ 找不到伺服器「{target_server}」。")

        if count > 100: count = 100
        if count < 1: count = 10

        filter_msg = f" 【{target_class}】的" if target_class else " "
        processing_msg = await ctx.send(f"📊 正在潛入資料庫，調閱 `{date_str}` 的 {target_server}{filter_msg}歷史排行榜...")

        sql = '''
            SELECT player_name, server_name, level, exp, class_name 
            FROM exp_history 
            WHERE record_time LIKE ? 
        '''
        params = [f"{date_str}%"]
        
        if not is_global:
            sql += " AND server_name = ?"
            params.append(target_server)
            
        if target_class:
            sql += " AND class_name LIKE ?"
            params.append(f"%{target_class}%")
            
        sql += '''
            GROUP BY player_name, server_name
            HAVING record_time = MAX(record_time)
            ORDER BY exp DESC
            LIMIT ?
        '''
        params.append(count)

        try:
            async with self.bot.db.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                err_msg = f"❌ 資料庫中找不到 `{date_str}` 的排名資料。"
                if target_class: err_msg = f"❌ 找不到符合條件的【{target_class}】玩家資料。"
                return await processing_msg.edit(content=err_msg)

            desc = f"**歷史快照日期：{date_str}**\n```yaml\n"
            embeds = []
            
            for idx, r in enumerate(rows, 1):
                name, server, level, exp, class_name = r
                
                # ✨ 這裡加上了重要的 await，防止備註顯示成亂碼物件
                tag = await self.get_member_info(name)
                
                display_name = f"{name}{tag}"
                name_width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in display_name)
                name_padded = display_name + " " * max(0, 16 - name_width)
                
                srv_info = f"({server})" if is_global else ""
                exp_zhao = exp / 1000000000000
                class_info = f"[{class_name}]"
                
                line = f"{idx:02d}. {name_padded} {class_info:<7} | Lv.{level:<2} | {exp_zhao:>7.2f} 兆 {srv_info}\n"
                
                if len(desc) + len(line) > 1900:
                    desc += "```"
                    embeds.append(discord.Embed(title=f"📜 {target_server}{filter_msg}歷史排名 (續)", description=desc, color=0x3498db))
                    desc = "```yaml\n"
                desc += line
                
            if desc != "```yaml\n":
                desc += "```"
                embed = discord.Embed(title=f"📜 {target_server}{filter_msg}歷史排名 TOP {len(rows)}", description=desc, color=0x3498db)
                embed.set_footer(text="系統：天眼資料庫歷史快照 (支援職業篩選)")
                embeds.append(embed)

            await processing_msg.delete()
            for e in embeds:
                await ctx.send(embed=e)

        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while fetching historical ranking: {e}")
            await processing_msg.edit(content="❌ 資料庫查詢逾時，請重試")
        except ValueError as e:
            logger.error(f"Invalid value in historical ranking query: {e}")
            await processing_msg.edit(content="❌ 資料格式錯誤，請檢查查詢參數")
        except Exception as e:
            logger.error(f"Error while fetching historical ranking: {e}")
            try:
                await processing_msg.edit(content=f"❌ 系統錯誤: {type(e).__name__}")
            except discord.NotFound:
                await ctx.send(f"❌ 系統錯誤: {type(e).__name__}")
    # ==========================================
    # 🕵️ 天眼追蹤系統：V4 雙引擎版 (絕對碰撞 + 無縫接軌)
    # ==========================================
    @commands.command(name="尋人", help="利用經驗值特徵，精準追蹤改名或轉服的玩家。用法: !尋人 驕傲o")
    async def track_player(self, ctx, target_name: str):
        processing_msg = await ctx.send(f"🔍 啟動天眼雙引擎，正在進行【絕對碰撞】與【無縫接軌】掃描...")

        try:
            # 1. 取得目標的所有分身/伺服器紀錄 (作為基準點)
            async with self.bot.db.execute('''
                SELECT server_name, MAX(level), MIN(record_time), MAX(record_time), MIN(exp), MAX(exp), class_name, MAX(subjugation_grade)
                FROM exp_history
                WHERE player_name = ?
                GROUP BY server_name
            ''', (target_name,)) as cursor:
                target_profiles = await cursor.fetchall()

            if not target_profiles:
                return await processing_msg.edit(content=f"❌ 天眼系統找不到「{target_name}」的任何歷史紀錄。")

            timeline_entries = []

            # 🚀 引擎 A：絕對碰撞 (上一版的完美邏輯，不管時間跟職業，只要 EXP 一模一樣就抓)
            sql_exact = '''
                SELECT exp, player_name, server_name, MAX(level), class_name, MIN(record_time), MAX(record_time), MAX(subjugation_grade)
                FROM exp_history
                WHERE exp IN (SELECT DISTINCT exp FROM exp_history WHERE player_name = ?)
                GROUP BY exp, player_name, server_name
            '''
            async with self.bot.db.execute(sql_exact, (target_name,)) as cursor:
                exact_matches = await cursor.fetchall()
            
            for exp, p_name, s_name, lvl, cls_name, first_seen, last_seen, sub_grade in exact_matches:
                match_type = "🎯 查詢目標" if p_name == target_name else "🔗 絕對經驗值碰撞"
                diff_text = "EXP 完全一致" if p_name != target_name else ""
                timeline_entries.append({
                    "name": p_name, "server": s_name, "lvl": lvl, "cls": cls_name,
                    "first": first_seen, "last": last_seen,
                    "match_type": match_type,
                    "diff_text": diff_text,
                    "exp_val": exp,
                    "sub_grade": sub_grade
                })

            # 🚀 引擎 B：無縫接軌偷練 (解決轉服空窗期偷打怪的問題)
            EXP_MARGIN = 1.0 * 1000000000000 # 容許 1 兆以內的偷練誤差
            
            for t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls, t_sub_grade in target_profiles:
                
                # 尋找後繼者 (目標消失後出現，經驗值微幅增加)
                sql_forward = '''
                    SELECT player_name, server_name, MAX(level), class_name, MIN(record_time), MAX(record_time), MIN(exp), MAX(subjugation_grade)
                    FROM exp_history
                    WHERE player_name != ?
                    GROUP BY player_name, server_name
                    HAVING MIN(record_time) >= datetime(?, '-2 hours') AND MIN(record_time) <= datetime(?, '+7 days')
                       AND MIN(exp) >= ? AND MIN(exp) <= ?
                '''
                async with self.bot.db.execute(sql_forward, (target_name, t_last, t_last, t_max_exp, t_max_exp + EXP_MARGIN)) as cursor:
                    forward_matches = await cursor.fetchall()
                    
                for c_name, c_server, c_lvl, c_class, c_first, c_last, c_min_exp, c_sub_grade in forward_matches:
                    timeline_entries.append({
                        "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_class,
                        "first": c_first, "last": c_last,
                        "match_type": "✈️ 無縫接軌 (轉服/改名後)",
                        "diff_text": f"轉服空窗偷練 +{(c_min_exp - t_max_exp)/100000000:,.0f} 億",
                        "exp_val": c_min_exp,
                        "sub_grade": c_sub_grade
                    })

                # 尋找前身 (目標出現前消失，經驗值微幅增加到目標的初始值)
                sql_backward = '''
                    SELECT player_name, server_name, MAX(level), class_name, MIN(record_time), MAX(record_time), MAX(exp), MAX(subjugation_grade)
                    FROM exp_history
                    WHERE player_name != ?
                    GROUP BY player_name, server_name
                    HAVING MAX(record_time) >= datetime(?, '-7 days') AND MAX(record_time) <= datetime(?, '+2 hours')
                       AND MAX(exp) <= ? AND MAX(exp) >= ?
                '''
                async with self.bot.db.execute(sql_backward, (target_name, t_first, t_first, t_min_exp, t_min_exp - EXP_MARGIN)) as cursor:
                    backward_matches = await cursor.fetchall()

                for c_name, c_server, c_lvl, c_class, c_first, c_last, c_max_exp, c_sub_grade in backward_matches:
                    timeline_entries.append({
                        "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_class,
                        "first": c_first, "last": c_last,
                        "match_type": "🔍 前身 (轉服/改名前)",
                        "diff_text": f"轉服空窗偷練 +{(t_min_exp - c_max_exp)/100000000:,.0f} 億",
                        "exp_val": c_max_exp,
                        "sub_grade": c_sub_grade
                    })

            # 過濾重複資料 (因為引擎A和引擎B可能會抓到同一筆紀錄)
            unique_entries = []
            seen = set()
            for entry in timeline_entries:
                key = (entry['name'], entry['server'])
                if key not in seen:
                    seen.add(key)
                    unique_entries.append(entry)

            unique_entries.sort(key=lambda x: x['first'])

            # 判斷是否只有目標自己
            if len(unique_entries) <= len(target_profiles) and all(x['name'] == target_name for x in unique_entries):
                target_last_exp = max(p[5] for p in target_profiles)
                return await processing_msg.edit(content=f"⚠️ 目標最後紀錄為 {target_last_exp/1000000000000:.2f} 兆。\n系統啟動了【絕對碰撞】與【無縫接軌】雙引擎掃描，沒有發現轉服或改名軌跡。")

            desc = f"🚨 **啟動雙引擎掃描，成功捕捉「{target_name}」的軌跡！**\n\n```yaml\n"
            
            for idx, p in enumerate(unique_entries, 1):
                exp_zhao = p['exp_val'] / 1_000_000_000_000
                desc += f"{idx}. {p['name']} [{p['server']}]\n"
                desc += f"   ▶ {p['match_type']}\n"
                desc += f"   ▶ 職業: {p['cls']} | Lv.{p['lvl']} | 討伐 {p.get('sub_grade', 0)}\n"
                desc += f"   ▶ 觀測: {p['first'][5:16]} ~ {p['last'][5:16]}\n"
                if p['diff_text']:
                    desc += f"   ▶ 關聯: {p['diff_text']} (特徵: {exp_zhao:,.2f}兆)\n\n"
                else:
                    desc += f"   ▶ EXP : {exp_zhao:,.2f} 兆\n\n"

            desc += "```"
            embed = discord.Embed(title=f"👁️ 天眼追蹤系統 (V4雙引擎版) - {target_name}", description=desc[:4000], color=0xff0000)
            embed.set_footer(text="系統：保留V2絕對碰撞優勢，並加入V3無縫接軌抓包技術")

            await processing_msg.delete()
            await ctx.send(embed=embed)
            
        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while tracking player '{target_name}': {e}")
            try:
                await processing_msg.edit(content="❌ 天眼系統查詢逾時，請重試")
            except discord.NotFound:
                pass
        except KeyError as e:
            logger.error(f"Missing required field in player tracking: {e}")
            try:
                await processing_msg.edit(content=f"❌ 尋人系統資料欄位異常")
            except discord.NotFound:
                pass
        except Exception as e:
            logger.error(f"Error while tracking player '{target_name}': {e}")
            try:
                await processing_msg.edit(content=f"❌ 尋人系統發生錯誤: {type(e).__name__}")
            except discord.NotFound:
                pass
    # ==========================================
    # 👇 從這裡開始複製，把這整段貼到 track_player 的下面 👇
    # ==========================================
    @commands.command(name="轉服掃描", aliases=["移民清單", "抓包"], help="全服掃描近期利用轉服空窗期改名或移動的玩家")
    async def global_transfer_scan(self, ctx):
        processing_msg = await ctx.send("📡 正在進行全資料庫特徵碰撞比對，這可能需要幾秒鐘...")

        try:
            # 1. 找出所有「被兩個以上不同(玩家+伺服器)組合」共用的經驗值 (為了防誤判，僅限大於 1 兆的活躍玩家)
            async with self.bot.db.execute('''
                SELECT exp
                FROM exp_history
                WHERE exp > 1000000000000
                GROUP BY exp
                HAVING COUNT(DISTINCT player_name || server_name) > 1
                ORDER BY MAX(record_time) DESC
                LIMIT 10
            ''') as cursor:
                shared_exps = await cursor.fetchall()
            
            if not shared_exps:
                return await processing_msg.edit(content="💤 目前資料庫中沒有偵測到任何轉服或改名的活動軌跡。")

            exp_list = [row[0] for row in shared_exps]
            placeholders = ','.join('?' for _ in exp_list)
            
            # 2. 把這些有碰撞的經驗值詳細資料抓出來
            async with self.bot.db.execute(f'''
                SELECT exp, player_name, server_name, MIN(record_time), MAX(record_time)
                FROM exp_history
                WHERE exp IN ({placeholders})
                GROUP BY exp, player_name, server_name
                ORDER BY exp DESC, MIN(record_time) ASC
            ''', tuple(exp_list)) as cursor:
                records = await cursor.fetchall()

            # 3. 組合報表
            grouped_data = {}
            for exp, p_name, s_name, first_seen, last_seen in records:
                if exp not in grouped_data:
                    grouped_data[exp] = []
                grouped_data[exp].append({"name": p_name, "server": s_name, "first": first_seen, "last": last_seen})

            embeds = []
            desc = "🔍 **以下玩家被系統偵測到經驗值完全重疊：**\n\n"
            
            for exp, players in grouped_data.items():
                exp_zhao = exp / 1_000_000_000_000
                desc += f"🔗 **特徵碼：{exp_zhao:.3f} 兆**\n```yaml\n"
                
                for idx, p in enumerate(players, 1):
                    desc += f"{idx}. {p['name']} [{p['server']}]\n"
                    desc += f"   (觀測區間: {p['first'][5:16]} ~ {p['last'][5:16]})\n"
                desc += "```\n"

                # 分頁處理避免超過 Discord 字數限制
                if len(desc) > 1500:
                    embeds.append(discord.Embed(title="✈️ 全服轉服與改名掃描報告", description=desc, color=0xe67e22))
                    desc = ""
                    
            if desc:
                embeds.append(discord.Embed(title="✈️ 全服轉服與改名掃描報告", description=desc, color=0xe67e22))

            await processing_msg.delete()
            for e in embeds:
                e.set_footer(text="※ 原理：轉服期間經驗值會凍結，利用相同特徵追蹤移動軌跡。")
                await ctx.send(embed=e)

        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout during transfer scan: {e}")
            try:
                await processing_msg.edit(content="❌ 掃描查詢逾時，請重試")
            except discord.NotFound:
                pass
        except ValueError as e:
            logger.error(f"Invalid value during transfer scan: {e}")
            try:
                await processing_msg.edit(content="❌ 掃描資料格式錯誤")
            except discord.NotFound:
                pass
        except Exception as e:
            logger.error(f"Error during transfer scan: {e}")
            try:
                await processing_msg.edit(content=f"❌ 掃描發生錯誤: {type(e).__name__}")
            except discord.NotFound:
                pass
    @commands.command(name="測試轉移警報", help="發送測試訊息以確認轉移警報頻道設定是否正確。")
    async def test_transfer_alert(self, ctx):
        channel_id = self.TRANSFER_ALERT_CHANNEL_ID
        if not channel_id:
            return await ctx.send("❌ 系統尚未設定 `TRANSFER_ALERT_CHANNEL_ID` 環境變數，請確認 `.env` 檔案設定。")

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return await ctx.send(f"❌ 找不到頻道 ID：`{channel_id}`。請確認 ID 是否正確，且機器人是否在該頻道擁有權限。")

        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        embed = discord.Embed(
            title="【波拉西亞戰記】轉移/旅團變動警報 (測試)",
            description=f"時間：{now}\n{'-'*30}\n"
                        f"✨ [即時轉移辨識] **測試玩家_舊** (測試伺服器_舊) ➔\n"
                        f"**測試玩家_新** (測試伺服器_新)\n"
                        f"[狀態]: 跨服轉移並改名 | [EXP變動]: +999 億 (轉移期間偷練)\n"
                        f"[屬性]: Lv.99 / 測試職業 / 討伐 99\n\n"
                        f"✅ **如果您看到此訊息，表示轉移警報頻道設定與權限皆正常運作中！**",
            color=0xf1c40f
        )

        try:
            await channel.send(embed=embed)
            await ctx.send("✅ 測試轉移警報已成功發送！請檢查警報頻道。")
        except discord.Forbidden:
            await ctx.send("❌ 機器人沒有權限在該頻道發送訊息或嵌入連結 (Embed Links)。")
        except Exception as e:
            await ctx.send(f"❌ 發送警報時發生錯誤：{e}")

    # ==========================================
    # 👆 複製到這裡結束 👆
    # ==========================================        

# setup 獨立在最外層
async def setup(bot):
    await bot.add_cog(ExpTracker(bot))
