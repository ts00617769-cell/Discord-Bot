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
                
        await self.bot.db.execute('CREATE INDEX IF NOT EXISTS idx_time_server ON exp_history(record_time, server_name)')
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
        try:
            async with session.post(api_url, json=payload, timeout=10) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("gc", [])[:50]
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
            logger.info(f"[{now_time.strftime('%H:%M:%S')}] 哨兵出動：掃描全服前50名...")
            
            # 👇 直接移除 async with aiohttp.ClientSession() 的區塊，改用 self.bot.session
            for server_name, (g_id, w_id) in SERVER_MAP.items():
                try:
                    # 這裡傳入 self.bot.session
                    players = await self.fetch_server_data(self.bot.session, g_id, w_id) 

                    for p in players:
                        await self.bot.db.execute('''
                            INSERT INTO exp_history (record_time, server_name, player_name, level, exp, class_name)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (now_time, server_name, p.get('gc_name'), p.get('gc_level'), p.get('gc_exp', 0), p.get('class_name', '未知')))
                    await self.bot.db.commit()
                except Exception as e:
                    logger.error(f"Error processing server {server_name}: {e}")
                    continue
                await asyncio.sleep(0.5)
            
            if self.alerts_enabled:
                await self.check_for_alerts(now_time)
        except Exception as e:
            logger.error(f"🚨 [經驗值雷達] 發生未預期錯誤，已攔截以防崩潰：{e}")

    async def check_for_alerts(self, current_time):
        async with self.bot.db.execute('SELECT DISTINCT record_time FROM exp_history ORDER BY record_time DESC LIMIT 2') as cursor:
            times = await cursor.fetchall()
            
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
        async with self.bot.db.execute(sql, (time_now, time_prev)) as cursor:
            records = await cursor.fetchall()
        
        # ... (後續警報發送邏輯保持原樣，沒有 SQL 變動) ...
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

    @auto_fetch_exp.before_loop
    async def before_auto_fetch(self):
        await self.bot.wait_until_ready()

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

        async with self.bot.db.execute('SELECT DISTINCT record_time FROM exp_history ORDER BY record_time DESC LIMIT 2') as cursor:
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
                SELECT server_name, MAX(level), MIN(record_time), MAX(record_time), MIN(exp), MAX(exp), class_name
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
                SELECT exp, player_name, server_name, MAX(level), class_name, MIN(record_time), MAX(record_time)
                FROM exp_history
                WHERE exp IN (SELECT DISTINCT exp FROM exp_history WHERE player_name = ?)
                GROUP BY exp, player_name, server_name
            '''
            async with self.bot.db.execute(sql_exact, (target_name,)) as cursor:
                exact_matches = await cursor.fetchall()
            
            for exp, p_name, s_name, lvl, cls_name, first_seen, last_seen in exact_matches:
                match_type = "🎯 查詢目標" if p_name == target_name else "🔗 絕對經驗值碰撞"
                diff_text = "EXP 完全一致" if p_name != target_name else ""
                timeline_entries.append({
                    "name": p_name, "server": s_name, "lvl": lvl, "cls": cls_name,
                    "first": first_seen, "last": last_seen,
                    "match_type": match_type,
                    "diff_text": diff_text,
                    "exp_val": exp
                })

            # 🚀 引擎 B：無縫接軌偷練 (解決轉服空窗期偷打怪的問題)
            EXP_MARGIN = 1.0 * 1000000000000 # 容許 1 兆以內的偷練誤差
            
            for t_server, t_lvl, t_first, t_last, t_min_exp, t_max_exp, t_cls in target_profiles:
                
                # 尋找後繼者 (目標消失後出現，經驗值微幅增加)
                sql_forward = '''
                    SELECT player_name, server_name, MAX(level), class_name, MIN(record_time), MAX(record_time), MIN(exp)
                    FROM exp_history
                    WHERE player_name != ?
                    GROUP BY player_name, server_name
                    HAVING MIN(record_time) >= datetime(?, '-2 hours') AND MIN(record_time) <= datetime(?, '+7 days')
                       AND MIN(exp) >= ? AND MIN(exp) <= ?
                '''
                async with self.bot.db.execute(sql_forward, (target_name, t_last, t_last, t_max_exp, t_max_exp + EXP_MARGIN)) as cursor:
                    forward_matches = await cursor.fetchall()
                    
                for c_name, c_server, c_lvl, c_class, c_first, c_last, c_min_exp in forward_matches:
                    timeline_entries.append({
                        "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_class,
                        "first": c_first, "last": c_last,
                        "match_type": "✈️ 無縫接軌 (轉服/改名後)",
                        "diff_text": f"轉服空窗偷練 +{(c_min_exp - t_max_exp)/100000000:,.0f} 億",
                        "exp_val": c_min_exp
                    })

                # 尋找前身 (目標出現前消失，經驗值微幅增加到目標的初始值)
                sql_backward = '''
                    SELECT player_name, server_name, MAX(level), class_name, MIN(record_time), MAX(record_time), MAX(exp)
                    FROM exp_history
                    WHERE player_name != ?
                    GROUP BY player_name, server_name
                    HAVING MAX(record_time) >= datetime(?, '-7 days') AND MAX(record_time) <= datetime(?, '+2 hours')
                       AND MAX(exp) <= ? AND MAX(exp) >= ?
                '''
                async with self.bot.db.execute(sql_backward, (target_name, t_first, t_first, t_min_exp, t_min_exp - EXP_MARGIN)) as cursor:
                    backward_matches = await cursor.fetchall()

                for c_name, c_server, c_lvl, c_class, c_first, c_last, c_max_exp in backward_matches:
                    timeline_entries.append({
                        "name": c_name, "server": c_server, "lvl": c_lvl, "cls": c_class,
                        "first": c_first, "last": c_last,
                        "match_type": "🔍 前身 (轉服/改名前)",
                        "diff_text": f"轉服空窗偷練 +{(t_min_exp - c_max_exp)/100000000:,.0f} 億",
                        "exp_val": c_max_exp
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
                desc += f"   ▶ 職業: {p['cls']} | Lv.{p['lvl']}\n"
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
    # ==========================================
    # 👆 複製到這裡結束 👆
    # ==========================================        

# setup 獨立在最外層
async def setup(bot):
    await bot.add_cog(ExpTracker(bot))
