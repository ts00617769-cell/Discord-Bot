import discord
from discord.ext import commands, tasks
import json
import random
import datetime
import pytz

# --- 讀取題庫 ---
with open('quiz.json', 'r', encoding='utf-8') as f:
    quiz_data = json.load(f)

# --- 暫存盲投測驗的資料 ---
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
            self.add_item(QuizButton(text, key, question_data["results"][key], styles[i % len(styles)]))

# 2. 盲投測驗按鈕 (定時測驗用)
class SecretQuizButton(discord.ui.Button):
    def __init__(self, custom_id, label, style):
        super().__init__(label=label, custom_id=custom_id, style=style)

    async def callback(self, interaction: discord.Interaction):
        if not active_poll["is_active"]:
            await interaction.response.send_message("❌ 這次測驗已經結束或尚未開始！", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name

        if user_id in active_poll["votes"]:
            await interaction.response.send_message("⚠️ 你已經投過票囉！請耐心等待晚上開獎。", ephemeral=True)
            return

        active_poll["votes"][user_id] = {
            "name": user_name,
            "choice": self.custom_id
        }
        await interaction.response.send_message(f"✅ 投票成功！你選擇了「{self.label}」。結果將於晚上 18:00 公布。", ephemeral=True)

class SecretQuizView(discord.ui.View):
    def __init__(self, question_data):
        super().__init__(timeout=None)
        styles = [discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger]
        for i, (key, text) in enumerate(question_data["options"].items()):
            self.add_item(SecretQuizButton(key, text, styles[i % len(styles)]))


# ================= 測驗模組 (Cog) 核心 =================

class QuizSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 模組載入時，自動啟動這兩個背景排程
        self.auto_post_quiz.start()
        self.auto_reveal_quiz.start()

    def cog_unload(self):
        # 模組卸載時，自動關閉排程
        self.auto_post_quiz.cancel()
        self.auto_reveal_quiz.cancel()

    # --- 排程：每天中午 12 點自動發布 ---
    @tasks.loop(minutes=1)
    async def auto_post_quiz(self):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)
        current_date = now.strftime("%Y-%m-%d")

        if now.hour == 12 and now.minute == 0:
            if active_poll["is_active"] and active_poll["date"] == current_date:
                return

            try:
                # 替換成你的發題頻道 ID
                channel = self.bot.get_channel(1480493340456783922) 
                if channel:
                    seed = int(now.strftime("%Y%m%d"))
                    random.seed(seed)
                    question = random.choice(quiz_data)
                    random.seed()

                    active_poll["is_active"] = True
                    active_poll["date"] = current_date
                    active_poll["channel_id"] = channel.id
                    active_poll["data"] = question
                    active_poll["votes"].clear()

                    embed = discord.Embed(title="🕛 中午 12 點了！每日深層心理測驗來囉", description=question['title'], color=0x3498db)
                    embed.set_footer(text="請點擊下方按鈕進行盲投，結果將於晚上 18:00 準時公開！")

                    view = SecretQuizView(question)
                    await channel.send(embed=embed, view=view)
            except Exception as e:
                print(f"自動發布測驗失敗: {e}")

    # --- 排程：每天晚上 18 點自動開獎 ---
    @tasks.loop(minutes=1)
    async def auto_reveal_quiz(self):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz)

        if now.hour == 18 and now.minute == 0:
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
            except Exception as e:
                print(f"自動開獎失敗: {e}")

    # --- 一般指令 ---
    @commands.command(name="測驗")
    async def normal_quiz(self, ctx):
        question = random.choice(quiz_data)
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

        seed = int(now.strftime("%Y%m%d"))
        random.seed(seed)
        question = random.choice(quiz_data)
        random.seed()

        active_poll["is_active"] = True
        active_poll["date"] = current_date
        active_poll["channel_id"] = ctx.channel.id
        active_poll["data"] = question
        active_poll["votes"].clear()

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

# ================= 掛載點 =================
async def setup(bot):
    await bot.add_cog(QuizSystem(bot))