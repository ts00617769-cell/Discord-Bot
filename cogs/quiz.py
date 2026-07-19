import discord
from discord.ext import commands, tasks
import json
import random
import datetime
import pytz
import os
import logging
import asyncio
import sqlite3
from .error_handler import parse_env_channel_id

logger = logging.getLogger(__name__)

# --- 暫存盲投測驗的資料 (記憶體) ---
active_poll = {
    "is_active": False,
    "date": None,
    "channel_id": None,
    "data": None,
    "votes": {}
}


def _parse_quiz_schedule():
    """只解析一次環境變數，供排程與顯示共用。"""
    tz = pytz.timezone("Asia/Taipei")
    post_raw = os.getenv("QUIZ_POST_TIME", "12:00")
    reveal_raw = os.getenv("QUIZ_REVEAL_TIME", "18:00")
    try:
        post_h, post_m = map(int, post_raw.split(":"))
        reveal_h, reveal_m = map(int, reveal_raw.split(":"))
    except (ValueError, IndexError):
        logger.warning(
            f"Invalid quiz time format, using defaults. POST={post_raw!r} REVEAL={reveal_raw!r}"
        )
        post_h, post_m, reveal_h, reveal_m = 12, 0, 18, 0
    return (
        datetime.time(hour=post_h, minute=post_m, tzinfo=tz),
        datetime.time(hour=reveal_h, minute=reveal_m, tzinfo=tz),
        tz,
    )


POST_TIME, REVEAL_TIME, TZ_TAIPEI = _parse_quiz_schedule()

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
        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while saving quiz vote for user {user_id}: {e}")
        except sqlite3.DatabaseError as e:
            logger.error(f"Failed to save quiz vote to database for user {user_id}: {e}")

        reveal_label = REVEAL_TIME.strftime("%H:%M")
        await interaction.response.send_message(
            f"✅ 投票成功！你選擇了「{self.label}」。結果將於 {reveal_label} 公布。",
            ephemeral=True
        )

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
        self.quiz_data = []
        self.load_quiz_data()
        # 與 @tasks.loop 使用同一組時間，避免 import / __init__ 重複解析不一致
        self.tz_taipei = TZ_TAIPEI
        self.post_time = POST_TIME
        self.reveal_time = REVEAL_TIME
        logger.info(f"Quiz times configured - Post: {self.post_time}, Reveal: {self.reveal_time}")

    def load_quiz_data(self):
        """讀取心理測驗題庫 JSON 檔案"""
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, "quiz.json")

            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    self.quiz_data = json.load(f)
                logger.info(f"[測驗系統] 成功載入 {len(self.quiz_data)} 題測驗！")
            else:
                logger.error(f"[測驗系統] 找不到檔案！預期路徑為：{file_path}")
                self.quiz_data = []
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"[測驗系統] 讀取題庫失敗: {e}")
            self.quiz_data = []

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
        # 表結構由 db.schema migration v3 集中管理；此處僅確保遷移已套用（支援 !reload）
        from db import apply_migrations

        await apply_migrations(self.bot.db)

    async def check_active_quiz_resume(self):
        """重啟機器人時的斷電接關機制"""
        async with self.bot.db.execute("SELECT is_active, quiz_id, channel_id, date_str FROM active_quiz_status WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        
        if row and row[0] == 1:
            quiz_title, channel_id, date_str = row[1], int(row[2]), row[3]
            
            # 👇 這裡加上 self.
            q_data = next((q for q in self.quiz_data if q['title'] == quiz_title), None)
            
            if q_data:
                # 撈出所有已投票的紀錄
                async with self.bot.db.execute("SELECT user_id, user_name, choice FROM quiz_votes") as v_cursor:
                    votes = await v_cursor.fetchall()
                
                # 恢復全域變數記憶體
                active_poll["is_active"] = True
                active_poll["date"] = date_str
                active_poll["channel_id"] = channel_id
                active_poll["data"] = q_data
                active_poll["votes"] = {v[0]: {"name": v[1], "choice": v[2]} for v in votes}
                
                # 重新綁定按鈕監聽 (讓重啟前發送的按鈕繼續有效)
                self.bot.add_view(SecretQuizView(q_data))
                logger.info(f"[Quiz] 已成功接關尚未開獎的測驗：{quiz_title}")

    async def get_unrepeated_quiz(self):
        """防重複抽題機制"""
        async with self.bot.db.execute("SELECT quiz_id FROM quiz_history") as cursor:
            used_ids = [row[0] for row in await cursor.fetchall()]

        # 👇 這裡加上 self.
        available = [q for q in self.quiz_data if q['title'] not in used_ids]

        # 如果全出過了，清空歷史紀錄重來一輪
        if not available:
            await self.bot.db.execute("DELETE FROM quiz_history")
            await self.bot.db.commit()
            available = self.quiz_data # 👇 這裡加上 self.

        if not available:
            logger.error("Quiz bank is empty; cannot pick a question")
            return None

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
    @tasks.loop(time=POST_TIME)
    async def auto_post_quiz(self):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        current_date = now.strftime("%Y-%m-%d")

        if active_poll["is_active"] and active_poll["date"] == current_date:
            return

        try:
            channel_id = parse_env_channel_id("QUIZ_CHANNEL_ID", 0)
            if not channel_id:
                logger.warning("QUIZ_CHANNEL_ID unset; skip auto-post quiz")
                return
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Quiz channel {channel_id} not found")
                return

            question = await self.get_unrepeated_quiz()
            if not question:
                logger.error("Skip auto-post quiz: empty quiz bank")
                return

            active_poll["is_active"] = True
            active_poll["date"] = current_date
            active_poll["channel_id"] = channel.id
            active_poll["data"] = question
            active_poll["votes"].clear()

            await self.save_active_status(channel.id, current_date, question)

            reveal_label = self.reveal_time.strftime('%H:%M')
            embed = discord.Embed(
                title=f"🕛 {self.post_time.strftime('%H:%M')} 每日深層心理測驗來囉",
                description=question['title'],
                color=0x3498db
            )
            embed.set_footer(text=f"請點擊下方按鈕進行盲投，結果將於 {reveal_label} 準時公開！")

            view = SecretQuizView(question)
            await channel.send(embed=embed, view=view)
        except AttributeError as e:
            logger.error(f"Quiz data structure error: {e}")
        except (discord.HTTPException, sqlite3.DatabaseError, OSError) as e:
            logger.error(f"Failed to auto-post quiz at {current_date}: {e}")

    @auto_post_quiz.before_loop
    async def before_auto_post_quiz(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=REVEAL_TIME)
    async def auto_reveal_quiz(self):
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
                await self.clear_active_status()
        except KeyError as e:
            logger.error(f"Missing required field in active poll data: {e}")
        except AttributeError as e:
            logger.error(f"Quiz data structure error during reveal: {e}")
        except (discord.HTTPException, sqlite3.DatabaseError) as e:
            logger.error(f"Failed to auto-reveal quiz: {e}")

    @auto_reveal_quiz.before_loop
    async def before_auto_reveal_quiz(self):
        await self.bot.wait_until_ready()

    # --- 一般指令 ---
    @commands.command(name="測驗")
    async def normal_quiz(self, ctx):
        if not self.quiz_data:
            await ctx.send("❌ 題庫尚未載入或為空，請檢查 `quiz.json`。")
            return
        question = random.choice(self.quiz_data)
        embed = discord.Embed(title="✨ 隨機深層心理測驗", description=question['title'], color=0x9b59b6)
        view = QuizView(question)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="定時測驗")
    @commands.is_owner()
    async def force_post(self, ctx):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        current_date = now.strftime("%Y-%m-%d")

        if active_poll["is_active"] and active_poll["date"] == current_date:
            await ctx.send("⚠️ 今天的測驗已經發布過了！請在發布頻道參與投票。")
            return

        if not self.quiz_data:
            await ctx.send("❌ 題庫尚未載入或為空，請檢查 `quiz.json`。")
            return

        question = await self.get_unrepeated_quiz()
        if not question:
            await ctx.send("❌ 無法抽出題目，請檢查題庫。")
            return

        active_poll["is_active"] = True
        active_poll["date"] = current_date
        active_poll["channel_id"] = ctx.channel.id
        active_poll["data"] = question
        active_poll["votes"].clear()

        await self.save_active_status(ctx.channel.id, current_date, question)

        reveal_label = self.reveal_time.strftime('%H:%M')
        embed = discord.Embed(title="🎲 手動觸發今日測驗", description=question['title'], color=0x3498db)
        embed.set_footer(text=f"請點擊下方按鈕進行盲投，結果將於 {reveal_label} 公開（可用 !測試開獎 提早結算）。")
        view = SecretQuizView(question)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="測試開獎")
    @commands.is_owner()
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