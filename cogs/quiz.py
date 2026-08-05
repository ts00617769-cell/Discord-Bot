"""每日測驗／盲投（狀態掛在 cog 實例，非模組全域）。"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import sqlite3
from typing import cast

import discord
from discord.ext import commands, tasks

from services.error_handler import parse_env_channel_id, resolve_bot_channel
from services.timeutil import TAIPEI, now_taipei, today_taipei_str

logger = logging.getLogger(__name__)


def _parse_quiz_schedule():
    """只解析一次環境變數，供排程與顯示共用。"""
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
        datetime.time(hour=post_h, minute=post_m, tzinfo=TAIPEI),
        datetime.time(hour=reveal_h, minute=reveal_m, tzinfo=TAIPEI),
    )


POST_TIME, REVEAL_TIME = _parse_quiz_schedule()


def _empty_poll() -> dict:
    return {
        "is_active": False,
        "date": None,
        "channel_id": None,
        "data": None,
        "votes": {},
    }


def _get_quiz_cog(interaction: discord.Interaction):
    bot = cast(commands.Bot, interaction.client)
    return bot.get_cog("QuizSystem")


class QuizButton(discord.ui.Button):
    def __init__(self, label, custom_id, result_text, style):
        super().__init__(label=label, custom_id=custom_id, style=style)
        self.result_text = result_text

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** 選擇了 {self.label}：\n\n{self.result_text}",
            ephemeral=False,
        )


class QuizView(discord.ui.View):
    def __init__(self, question_data):
        super().__init__(timeout=None)
        styles = [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.danger,
        ]
        for i, (key, text) in enumerate(question_data["options"].items()):
            self.add_item(
                QuizButton(
                    text, f"quiz_{key}", question_data["results"][key], styles[i % len(styles)]
                )
            )


class SecretQuizButton(discord.ui.Button):
    def __init__(self, custom_id, label, style):
        super().__init__(label=label, custom_id=f"secret_quiz_{custom_id}", style=style)
        self.choice_key = custom_id

    async def callback(self, interaction: discord.Interaction):
        cog = _get_quiz_cog(interaction)
        if cog is None or not cog.active_poll["is_active"]:
            await interaction.response.send_message(
                "❌ 這次測驗已經結束或尚未開始！", ephemeral=True
            )
            return

        poll = cog.active_poll
        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name

        if user_id in poll["votes"]:
            await interaction.response.send_message(
                "⚠️ 你已經投過票囉！請耐心等待晚上開獎。", ephemeral=True
            )
            return

        # 以 DB UNIQUE(user_id) 為準：先寫入成功才更新記憶體，避免連點雙回覆
        try:
            bot = cast(commands.Bot, interaction.client)
            db = bot.db  # type: ignore[attr-defined]
            cursor = await db.execute(
                "INSERT INTO quiz_votes (user_id, user_name, choice) VALUES (?, ?, ?)",
                (user_id, user_name, self.choice_key),
            )
            await db.commit()
            if cursor.rowcount == 0:
                await interaction.response.send_message(
                    "⚠️ 你已經投過票囉！請耐心等待晚上開獎。", ephemeral=True
                )
                return
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                "⚠️ 你已經投過票囉！請耐心等待晚上開獎。", ephemeral=True
            )
            return
        except asyncio.TimeoutError as e:
            logger.error(f"Database timeout while saving quiz vote for user {user_id}: {e}")
            await interaction.response.send_message(
                "❌ 投票暫存失敗，請稍後再試。", ephemeral=True
            )
            return
        except sqlite3.DatabaseError as e:
            logger.error(f"Failed to save quiz vote to database for user {user_id}: {e}")
            await interaction.response.send_message(
                "❌ 投票寫入失敗，請稍後再試。", ephemeral=True
            )
            return

        poll["votes"][user_id] = {"name": user_name, "choice": self.choice_key}
        cog = _get_quiz_cog(interaction)
        reveal = cog.reveal_time if cog is not None else REVEAL_TIME
        reveal_label = reveal.strftime("%H:%M")
        await interaction.response.send_message(
            f"✅ 投票成功！你選擇了「{self.label}」。結果將於 {reveal_label} 公布。",
            ephemeral=True,
        )


class SecretQuizView(discord.ui.View):
    def __init__(self, question_data):
        super().__init__(timeout=None)
        styles = [
            discord.ButtonStyle.primary,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.success,
            discord.ButtonStyle.danger,
        ]
        for i, (key, text) in enumerate(question_data["options"].items()):
            self.add_item(SecretQuizButton(key, text, styles[i % len(styles)]))


class QuizSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.quiz_data = []
        self.active_poll = _empty_poll()
        self.load_quiz_data()
        self.post_time, self.reveal_time = _parse_quiz_schedule()
        logger.info(f"Quiz times configured - Post: {self.post_time}, Reveal: {self.reveal_time}")

    def load_quiz_data(self):
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
        self.post_time, self.reveal_time = _parse_quiz_schedule()
        self.auto_post_quiz.change_interval(time=self.post_time)
        self.auto_reveal_quiz.change_interval(time=self.reveal_time)
        await self.check_active_quiz_resume()
        if not self.auto_post_quiz.is_running():
            self.auto_post_quiz.start()
        if not self.auto_reveal_quiz.is_running():
            self.auto_reveal_quiz.start()

    def cog_unload(self):
        if self.auto_post_quiz.is_running():
            self.auto_post_quiz.cancel()
        if self.auto_reveal_quiz.is_running():
            self.auto_reveal_quiz.cancel()

    async def check_active_quiz_resume(self):
        async with self.bot.db.execute(
            "SELECT is_active, quiz_id, channel_id, date_str FROM active_quiz_status WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()

        if row and row[0] == 1:
            quiz_title, channel_id, date_str = row[1], int(row[2]), row[3]
            q_data = next((q for q in self.quiz_data if q["title"] == quiz_title), None)

            if q_data:
                async with self.bot.db.execute(
                    "SELECT user_id, user_name, choice FROM quiz_votes"
                ) as v_cursor:
                    votes = await v_cursor.fetchall()

                self.active_poll = {
                    "is_active": True,
                    "date": date_str,
                    "channel_id": channel_id,
                    "data": q_data,
                    "votes": {v[0]: {"name": v[1], "choice": v[2]} for v in votes},
                }
                self.bot.add_view(SecretQuizView(q_data))
                logger.info(f"[Quiz] 已成功接關尚未開獎的測驗：{quiz_title}")

    async def get_unrepeated_quiz(self):
        """抽出尚未用過的題目；發布 claim 成功後才寫入 history。"""
        async with self.bot.db.execute("SELECT quiz_id FROM quiz_history") as cursor:
            used_ids = [row[0] for row in await cursor.fetchall()]

        available = [q for q in self.quiz_data if q["title"] not in used_ids]

        if not available:
            await self.bot.db.execute("DELETE FROM quiz_history")
            await self.bot.db.commit()
            available = self.quiz_data

        if not available:
            logger.error("Quiz bank is empty; cannot pick a question")
            return None

        return random.choice(available)

    async def claim_quiz_post(self, channel_id, date_str, question) -> bool:
        """發布前以單一交易 claim 今日測驗；同日已有 active 時回 False。"""
        today_str = today_taipei_str()
        await self.bot.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.bot.db.execute(
                "SELECT is_active, date_str FROM active_quiz_status WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0] == 1 and row[1] == date_str:
                await self.bot.db.rollback()
                return False
            await self.bot.db.execute("DELETE FROM quiz_votes")
            await self.bot.db.execute(
                """
                INSERT OR REPLACE INTO active_quiz_status
                (id, is_active, quiz_id, channel_id, date_str)
                VALUES (1, 1, ?, ?, ?)
                """,
                (question["title"], str(channel_id), date_str),
            )
            await self.bot.db.execute(
                "INSERT OR REPLACE INTO quiz_history (quiz_id, used_date) VALUES (?, ?)",
                (question["title"], today_str),
            )
            await self.bot.db.commit()
            return True
        except Exception:
            await self.bot.db.rollback()
            raise

    async def rollback_quiz_post(self, date_str: str, question: dict) -> None:
        """Discord 發送失敗時，只撤銷本次 claim 與 history。"""
        await self.bot.db.execute("BEGIN IMMEDIATE")
        try:
            await self.bot.db.execute(
                """
                UPDATE active_quiz_status SET is_active = 0
                WHERE id = 1 AND date_str = ? AND quiz_id = ?
                """,
                (date_str, question["title"]),
            )
            await self.bot.db.execute("DELETE FROM quiz_votes")
            await self.bot.db.execute(
                "DELETE FROM quiz_history WHERE quiz_id = ? AND used_date = ?",
                (question["title"], today_taipei_str()),
            )
            await self.bot.db.commit()
        except Exception:
            await self.bot.db.rollback()
            raise

    async def clear_active_status(self):
        await self.bot.db.execute("UPDATE active_quiz_status SET is_active = 0 WHERE id = 1")
        await self.bot.db.execute("DELETE FROM quiz_votes")
        await self.bot.db.commit()

    def _start_poll(self, channel_id, current_date, question):
        self.active_poll = {
            "is_active": True,
            "date": current_date,
            "channel_id": channel_id,
            "data": question,
            "votes": {},
        }

    def _reset_poll(self):
        self.active_poll = _empty_poll()

    async def _finalize_reveal(self) -> None:
        """重試持久化結束狀態；最終仍失敗時至少關閉記憶體投票。"""
        last_error: sqlite3.DatabaseError | None = None
        for attempt in range(3):
            try:
                await self.clear_active_status()
                self._reset_poll()
                return
            except sqlite3.DatabaseError as e:
                last_error = e
                try:
                    await self.bot.db.rollback()
                except sqlite3.DatabaseError:
                    pass
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        self._reset_poll()
        logger.critical("Quiz finalize 連續失敗，已停止記憶體投票", exc_info=last_error)
        assert last_error is not None
        raise last_error

    def _build_reveal_embed(self, title: str, question: dict) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=f"回顧今日題目：\n{question['title']}",
            color=0xE74C3C,
        )
        results: dict[str, list[str]] = {key: [] for key in question["options"].keys()}
        for user_info in self.active_poll["votes"].values():
            results[user_info["choice"]].append(user_info["name"])

        for key, option_text in question["options"].items():
            voters = ", ".join(results[key]) if results[key] else "無人選擇"
            embed.add_field(
                name=f"👉 選擇【{option_text}】的玩家：",
                value=f"👥 名單：{voters}\n📝 解析：\n{question['results'][key]}",
                inline=False,
            )
        return embed

    @tasks.loop(time=POST_TIME)
    async def auto_post_quiz(self):
        now = now_taipei()
        current_date = now.strftime("%Y-%m-%d")

        if self.active_poll["is_active"] and self.active_poll["date"] == current_date:
            return

        try:
            channel_id = parse_env_channel_id("QUIZ_CHANNEL_ID", 0)
            if not channel_id:
                logger.warning("QUIZ_CHANNEL_ID unset; skip auto-post quiz")
                return
            channel = await resolve_bot_channel(
                self.bot, channel_id, label="quiz channel"
            )
            if not channel:
                logger.warning(f"Quiz channel {channel_id} not found")
                return

            question = await self.get_unrepeated_quiz()
            if not question:
                logger.error("Skip auto-post quiz: empty quiz bank")
                return

            reveal_label = self.reveal_time.strftime("%H:%M")
            embed = discord.Embed(
                title=f"🕛 {self.post_time.strftime('%H:%M')} 每日深層心理測驗來囉",
                description=question["title"],
                color=0x3498DB,
            )
            embed.set_footer(text=f"請點擊下方按鈕進行盲投，結果將於 {reveal_label} 準時公開！")
            if not await self.claim_quiz_post(channel.id, current_date, question):
                logger.info("今日測驗已被其他發布流程 claim，略過重複發布")
                return
            self._start_poll(channel.id, current_date, question)
            try:
                await channel.send(embed=embed, view=SecretQuizView(question))
            except discord.HTTPException:
                self._reset_poll()
                try:
                    await self.rollback_quiz_post(current_date, question)
                except sqlite3.DatabaseError:
                    logger.critical(
                        "auto_post 發送失敗且無法回滾 quiz claim",
                        exc_info=True,
                    )
                raise
        except AttributeError as e:
            logger.error(f"Quiz data structure error: {e}")
        except (discord.HTTPException, sqlite3.DatabaseError, OSError) as e:
            logger.error(f"Failed to auto-post quiz at {current_date}: {e}")

    @auto_post_quiz.before_loop
    async def before_auto_post_quiz(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=REVEAL_TIME)
    async def auto_reveal_quiz(self):
        if not self.active_poll["is_active"] or not self.active_poll["data"]:
            return

        try:
            channel = await resolve_bot_channel(
                self.bot,
                self.active_poll["channel_id"],
                label="quiz reveal channel",
            )
            if not channel:
                logger.warning(
                    f"Quiz reveal channel {self.active_poll['channel_id']} missing; "
                    "clearing stuck active poll"
                )
                await self._finalize_reveal()
                return

            embed = self._build_reveal_embed("🕕 每日測驗開獎時間！", self.active_poll["data"])
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as e:
                logger.error(f"Failed to send quiz reveal: {e}")
            # 無論是否送達，結束本日盲投，避免整天卡住
            await self._finalize_reveal()
        except KeyError as e:
            logger.error(f"Missing required field in active poll data: {e}")
            await self._finalize_reveal()
        except AttributeError as e:
            logger.error(f"Quiz data structure error during reveal: {e}")
            await self._finalize_reveal()
        except sqlite3.DatabaseError as e:
            logger.error(f"Failed to auto-reveal quiz (db): {e}")

    @auto_reveal_quiz.before_loop
    async def before_auto_reveal_quiz(self):
        await self.bot.wait_until_ready()

    @commands.command(name="測驗")
    async def normal_quiz(self, ctx):
        if not self.quiz_data:
            await ctx.send("❌ 題庫尚未載入或為空，請檢查 `quiz.json`。")
            return
        question = random.choice(self.quiz_data)
        embed = discord.Embed(
            title="✨ 隨機深層心理測驗", description=question["title"], color=0x9B59B6
        )
        await ctx.send(embed=embed, view=QuizView(question))

    @commands.command(name="定時測驗")
    @commands.is_owner()
    async def force_post(self, ctx):
        current_date = today_taipei_str()

        if self.active_poll["is_active"] and self.active_poll["date"] == current_date:
            await ctx.send("⚠️ 今天的測驗已經發布過了！請在發布頻道參與投票。")
            return

        if not self.quiz_data:
            await ctx.send("❌ 題庫尚未載入或為空，請檢查 `quiz.json`。")
            return

        question = await self.get_unrepeated_quiz()
        if not question:
            await ctx.send("❌ 無法抽出題目，請檢查題庫。")
            return

        reveal_label = self.reveal_time.strftime("%H:%M")
        embed = discord.Embed(
            title="🎲 手動觸發今日測驗", description=question["title"], color=0x3498DB
        )
        embed.set_footer(
            text=f"請點擊下方按鈕進行盲投，結果將於 {reveal_label} 公開（可用 !測試開獎 提早結算）。"
        )
        if not await self.claim_quiz_post(ctx.channel.id, current_date, question):
            await ctx.send("⚠️ 今天的測驗已經由其他發布流程建立。")
            return
        self._start_poll(ctx.channel.id, current_date, question)
        try:
            await ctx.send(embed=embed, view=SecretQuizView(question))
        except discord.HTTPException as e:
            self._reset_poll()
            try:
                await self.rollback_quiz_post(current_date, question)
            except sqlite3.DatabaseError:
                logger.critical("force_post 發送失敗且無法回滾 claim", exc_info=True)
            logger.error(f"force_post send failed: {e}")
            await ctx.send("❌ 測驗訊息發送失敗，請稍後再試。")

    @commands.command(name="測試開獎")
    @commands.is_owner()
    async def force_reveal(self, ctx):
        if not self.active_poll["is_active"] or not self.active_poll["data"]:
            await ctx.send("❌ 目前沒有正在進行中的盲投測驗！")
            return

        embed = self._build_reveal_embed("🚨 強制提早開獎！", self.active_poll["data"])
        try:
            await ctx.send(embed=embed)
        except discord.HTTPException as e:
            logger.error(f"force_reveal send failed: {e}")
        await self._finalize_reveal()


async def setup(bot):
    await bot.add_cog(QuizSystem(bot))
