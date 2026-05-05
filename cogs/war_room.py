import discord
from discord.ext import commands, tasks
import traceback
import sqlite3
import datetime

class WarRoom(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 🎯 定義你的戰情室頻道 ID (請換成你專屬的隱藏頻道 ID)
        self.log_channel_id = 1501113712176791633  
        
        # 啟動資料庫清理排程
        self.db_cleanup_task.start()

    # ==========================================
    # 1. 系統後勤監控 (Listeners)
    # ==========================================
    @commands.Cog.listener()
    async def on_ready(self):
        print(f'[模組載入] WarRoom (戰情室監控與排程) 運作中')
        channel = self.bot.get_channel(self.log_channel_id)
        if channel:
            await channel.send("🟢 **【系統廣播】** 戰情雷達已重新啟動，後勤監視與自動排程上線。")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        # 忽略一般的「找不到指令」錯誤
        if isinstance(error, commands.CommandNotFound):
            return

        channel = self.bot.get_channel(self.log_channel_id)
        if channel:
            error_msg = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            await channel.send(f"🔴 **【系統報錯】**\n出錯頻道：<#{ctx.channel.id}>\n出錯指令：`{ctx.message.content}`\n```python\n{error_msg[:1900]}\n
```")

    # ==========================================
    # 2. 自動化排程任務 (Tasks)
    # ==========================================
    # 設定台灣時間 (UTC+8) 凌晨 4 點
    tz = datetime.timezone(datetime.timedelta(hours=8))
    clean_time = datetime.time(hour=4, minute=0, tzinfo=tz)

    @tasks.loop(time=clean_time)
    async def db_cleanup_task(self):
        """每天凌晨 4 點自動刪除 30 天前的過期資料"""
        try:
            conn = sqlite3.connect('prasia_data.db')
            cursor = conn.cursor()
            
            # ⚠️ 注意：請確認你的資料表名稱是否為 speed_records，時間欄位是否為 timestamp
            cursor.execute("""
                DELETE FROM speed_records 
                WHERE timestamp < datetime('now', '-30 days')
            """)
            
            deleted_rows = cursor.rowcount
            conn.commit()
            conn.close()

            # 將清理結果回報給戰情室
            log_channel = self.bot.get_channel(self.log_channel_id) 
            if log_channel and deleted_rows > 0:
                await log_channel.send(f"🧹 **【資料庫維護】** 系統已於凌晨自動清理 `{deleted_rows}` 筆 30 天前的過期紀錄，釋放儲存空間。")
                
        except Exception as e:
            # 發生錯誤時回報
            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel:
                await log_channel.send(f"⚠️ **【資料庫清理異常】**\n```python\n{e}\n```")

    @db_cleanup_task.before_loop
    async def before_db_cleanup_task(self):
        # 確保機器人完全連線並準備好後，才開始排程計時
        await self.bot.wait_until_ready()

# 模組註冊入口
async def setup(bot):
    await bot.add_cog(WarRoom(bot))