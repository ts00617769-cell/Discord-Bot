import discord
from discord.ext import commands, tasks
import json
import random
import datetime
import pytz
import os

# --- 讀取題庫 ---
with open('quiz.json', 'r', encoding='utf-8') as f:
    quiz_data = json.load(f)

# --- 暫存盲投測驗的資料 (記憶體) ---
active_poll = {
    "is_active": False,
    "date": None,
    "channel_id": None,
    "data": None,
    "votes": {}
}

# ================= UI 互動按鈕 =================

# 1. 即時測驗按鈕 (無盲投)
class QuizButton(discord.ui.Button):
    def __init__(self, label, custom_id, result_text, style):
        super().__init__(label=label, custom_id=custom_id, style=style)
        self.result_text = result_text

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"**{interaction.user.display_name}** 選擇了 {self.label}：\n\n{self.result_text}", ephemeral=False)

class QuizView(discord.ui.View):
    def __init__(self, question_data):
        super().__init__(timeout=None)
        styles = [discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger]
        for i, (key, text) in enumerate(question_data["options"].items()):
            self.add_item(QuizButton(text, f"quiz_{key}", question_data["results"][key], styles[i % len(styles)]))

# 2. 盲投測驗按鈕 (持久化，支援重啟)
class SecretQuizButton(discord.ui.Button):
    def __init__(self, custom_id, label, style):
        # 加上前綴 secret_quiz_ 確保重新啟動時按鈕 ID 唯一且能被監聽
        super().__init__(label=label, custom_id=f"secret_quiz_{custom_id}", style=style)
        self.choice_key = custom_id # 真正的選項 A, B, C

    async def callback(self, interaction: discord.Interaction):
        if not active_poll["is_active"]:
            await interaction.response.send_message("❌ 這次測驗已經結束或尚未開始！", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name

        if user_id in active_poll["votes"]:
            await interaction.response.send_message("⚠️ 你已經投過票囉！請耐心等待晚上開獎。", ephemeral=True)
            return

        # 1. 更新記憶體
        active_poll["votes"][user_id] = {
            "name": user_name,
            "choice": self.choice_key
        }
        
        # 2. 同步寫入資料庫 (防斷電)
        try:
            db = interaction.client.db
            await db.execute(
                "INSERT OR REPLACE INTO quiz_votes (user_id, user_name, choice) VALUES (?, ?, ?)",
                (user_id, user_name, self.choice_key)
            )
            await db.commit()
        except Exception as e:
            print(f"投票寫入資料庫失敗: {e}")

        await interaction.response.send_message(f"✅ 投票成功！你選擇了「{self.label}」。結果將於晚上 18:00 公布。", ephemeral=True)

class SecretQuizView(discord.ui.View):
    def __init__(self, question_data):
        super().__init__(timeout=None) # timeout=None 是重啟監聽的關鍵
        styles = [discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger]
        for i, (key, text) in enumerate(question_data["options"].items()):
            self.add_item(SecretQuizButton(key, text, styles[i % len(styles)]))


# ================= 測驗模組 (Cog) 核心 =================

class QuizSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # ⚠️ 不要在這裡啟動排程，移到 cog_load 等資料庫準備好再啟動

    # =======================================================
    # 1. ✨ 新增這段：定義台北時間的中午 12 點與晚上 18 點 (縮排 4 個空格)
    # =======================================================
    tz_taipei = pytz.timezone('Asia/Taipei')
    post_time = datetime.time(hour=12, minute=0, tzinfo=tz_taipei)
    reveal_time = datetime.time(hour=18, minute=0, tzinfo=tz_taipei)

    async def cog_load(self):
        """模組載入時，初始化資料庫與接關機制"""
        await self.setup_database()
        await self.check_active_quiz_resume()
        self.auto_post_quiz.start()
        self.auto_reveal_quiz.start()

    def cog_unload(self):
        self.auto_post_quiz.cancel()
        self.auto_reveal_quiz.cancel()

    async def setup_database(self):
        # 記錄出過的題目
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS quiz_history (
                quiz_id TEXT PRIMARY KEY,
                used_date TEXT
            )
        ''')
        # 記錄正在進行的測驗狀態
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS active_quiz_status (
                id INTEGER PRIMARY KEY,
                is_active INTEGER,
                quiz_id TEXT,
                channel_id TEXT,
                date_str TEXT
            )
        ''')
        # 記錄成員的投票
        await self.bot.db.execute('''
            CREATE TABLE IF NOT EXISTS quiz_votes (
                user_id TEXT PRIMARY KEY,
                user_name TEXT,
                choice TEXT
            )
        ''')
        await self.bot.db.commit()

    async def check_active_quiz_resume(self):
        """重啟機器人時的斷電接關機制"""
        async with self.bot.db.execute("SELECT is_active, quiz_id, channel_id, date_str FROM active_quiz_status WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        
        if row and row[0] == 1:
            quiz_title, channel_id, date_str = row[1], int(row[2]), row[3]
            
            # 從題庫找出當前題目
            q_data = next((q for q in quiz_data if q['title'] == quiz_title), None)
            
            if q_data:
                # 撈出所有已投票的紀錄
                async with self.bot.db.execute("SELECT user_id, user_name, choice FROM quiz_votes") as v_cursor:
                    votes = await v_cursor.fetchall()
                
                # 恢復全域變數記憶體
                global active_poll
                active_poll["is_active"] = True
                active_poll["date"] = date_str
                active_poll["channel_id"] = channel_id
                active_poll["data"] = q_data
                active_poll["votes"] = {v[0]: {"name": v[1], "choice": v[2]} for v in votes}
                
                # 重新綁定按鈕監聽 (讓重啟前發送的按鈕繼續有效)
                self.bot.add_view(SecretQuizView(q_data))
                print(f"[Quiz] 已成功接關尚未開獎的測驗：{quiz_title}")

    async def get_unrepeated_quiz(self):
        """防重複抽題機制"""
        async with self.bot.db.execute("SELECT quiz_id FROM quiz_history") as cursor:
            used_ids = [row[0] for row in await cursor.fetchall()]

        # 過濾掉出過的題目 (以 title 作為唯一識別碼)
        available = [q for q in quiz_data if q['title'] not in used_ids]

        # 如果全出過了，清空歷史紀錄重來一輪
        if not available:
            await self.bot.db.execute("DELETE FROM quiz_history")
            await self.bot.db.commit()
            available = quiz_data

        question = random.choice(available)
        
        # 標記為已出過
        tz = pytz.timezone('Asia/Taipei')
        today_str = datetime.datetime.now(tz).strftime('%Y-%m-%d')
        await self.bot.db.execute("INSERT OR REPLACE INTO quiz_history (quiz_id, used_date) VALUES (?, ?)", (question['title'], today_str))
        await self.bot.db.commit()
        
        return question

    async def save_active_status(self, channel_id, date_str, question):
        """保存當前測驗狀態到資料庫"""
        await self.bot.db.execute("DELETE FROM quiz_votes") # 清空昨日選票
        await self.bot.db.execute('''
            INSERT OR REPLACE INTO active_quiz_status (id, is_active, quiz_id, channel_id, date_str)
            VALUES (1, 1, ?, ?, ?)
        ''', (question['title'], str(channel_id), date_str))
        await self.bot.db.commit()

    async def clear_active_status(self):
        """開獎後清空狀態"""
        await self.bot.db.execute("UPDATE active_quiz_status SET is_active = 0 WHERE id = 1")
        await self.bot.db.execute("DELETE FROM quiz_votes")
        await self.bot.db.commit()

    # =======================================================
    # 2. ✨ 修改排程：中午 12 點自動發布
    # =======================================================
    @tasks.loop(time=post_time) # 👈 這裡原本是 (minutes=1)，改成指定我們設定好的 post_time
    async def auto_post_quiz(self):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        current_date = now.strftime("%Y-%m-%d")

        # ✂️ 刪除了原本的 [if now.hour == 12 and now.minute == 0:] 這一行！
        # 👇 下方的程式碼因為少了一層 if 包裹，全部都要「往左推 4 個空格」對齊喔！
        if active_poll["is_active"] and active_poll["date"] == current_date:
            return

        try:
            channel_id = int(os.getenv("QUIZ_CHANNEL_ID", 0))
            channel = self.bot.get_channel(channel_id) 
            if channel:
                # ✨ 使用新的不重複抽題功能
                question = await self.get_unrepeated_quiz()

                active_poll["is_active"] = True
                active_poll["date"] = current_date
                active_poll["channel_id"] = channel.id
                active_poll["data"] = question
                active_poll["votes"].clear()

                # 保存狀態至資料庫
                await self.save_active_status(channel.id, current_date, question)

                embed = discord.Embed(title="🕛 中午 12 點了！每日深層心理測驗來囉", description=question['title'], color=0x3498db)
                embed.set_footer(text="請點擊下方按鈕進行盲投，結果將於晚上 18:00 準時公開！")

                view = SecretQuizView(question)
                await channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"自動發布測驗失敗: {e}")

    # =======================================================
    # 3. ✨ 修改排程：晚上 18 點自動開獎
    # =======================================================
    @tasks.loop(time=reveal_time) # 👈 這裡也改成指定 reveal_time
    async def auto_reveal_quiz(self):
        # ✂️ 刪除了原本的 [if now.hour == 18 and now.minute == 0:] 這一行！
        # 👇 下方的程式碼同樣全部「往左推 4 個空格」對齊！
        if not active_poll["is_active"] or not active_poll["data"]:
            return

        try:
            channel = self.bot.get_channel(active_poll["channel_id"])
            if channel:
                question = active_poll["data"]
                embed = discord.Embed(title="🕕 每日測驗開獎時間！", description=f"回顧今日題目：\n{question['title']}", color=0xe74c3c)

                results = {key: [] for key in question["options"].keys()}
                for user_info in active_poll["votes"].values():
                    results[user_info["choice"]].append(user_info["name"])

                for key, option_text in question["options"].items():
                    voters = ", ".join(results[key]) if results[key] else "無人選擇"
                    embed.add_field(
                        name=f"👉 選擇【{option_text}】的玩家：",
                        value=f"👥 名單：{voters}\n📝 解析：\n{question['results'][key]}",
                        inline=False
                    )

                await channel.send(embed=embed)
                
                active_poll["is_active"] = False
                await self.clear_active_status() # 清空資料庫狀態
        except Exception as e:
            print(f"自動開獎失敗: {e}")

    # --- 一般指令 ---
    @commands.command(name="測驗")
    async def normal_quiz(self, ctx):
        question = random.choice(quiz_data) # 單次娛樂不受重複限制
        embed = discord.Embed(title="✨ 隨機深層心理測驗", description=question['title'], color=0x9b59b6)
        view = QuizView(question)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="定時測驗")
    async def force_post(self, ctx):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        current_date = now.strftime("%Y-%m-%d")

        if active_poll["is_active"] and active_poll["date"] == current_date:
            await ctx.send("⚠️ 今天的測驗已經發布過了！請在發布頻道參與投票。")
            return

        question = await self.get_unrepeated_quiz()

        active_poll["is_active"] = True
        active_poll["date"] = current_date
        active_poll["channel_id"] = ctx.channel.id
        active_poll["data"] = question
        active_poll["votes"].clear()

        await self.save_active_status(ctx.channel.id, current_date, question)

        embed = discord.Embed(title="🎲 手動觸發今日測驗", description=question['title'], color=0x3498db)
        embed.set_footer(text="請點擊下方按鈕進行盲投，你可以使用 !測試開獎 來提早結算。")
        view = SecretQuizView(question)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="測試開獎")
    async def force_reveal(self, ctx):
        if not active_poll["is_active"] or not active_poll["data"]:
            await ctx.send("❌ 目前沒有正在進行中的盲投測驗！")
            return

        question = active_poll["data"]
        embed = discord.Embed(title="🚨 強制提早開獎！", description=f"回顧今日題目：\n{question['title']}", color=0xe74c3c)

        results = {key: [] for key in question["options"].keys()}
        for user_info in active_poll["votes"].values():
            results[user_info["choice"]].append(user_info["name"])

        for key, option_text in question["options"].items():
            voters = ", ".join(results[key]) if results[key] else "無人選擇"
            embed.add_field(
                name=f"👉 選擇【{option_text}】的玩家：",
                value=f"👥 名單：{voters}\n📝 解析：\n{question['results'][key]}",
                inline=False
            )

        await ctx.send(embed=embed)
        
        active_poll["is_active"] = False
        await self.clear_active_status()

# ================= 掛載點 =================
async def setup(bot):
    await bot.add_cog(QuizSystem(bot))