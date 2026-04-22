import discord
from discord.ext import commands
import json
import os
import datetime
import pytz

class GuildBank(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.file_path = 'bank_ledger.json'
        self.ensure_file()

    # 確保帳本檔案存在
    def ensure_file(self):
        if not os.path.exists(self.file_path):
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump({"balance": 0, "history": []}, f, ensure_ascii=False, indent=4)

    def load_data(self):
        with open(self.file_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def save_data(self, data):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    # --- 指令：記帳 (已解除權限鎖定，所有人皆可使用) ---
    @commands.command(name="記帳", help="格式: !記帳 [金額] [說明] (例如: !記帳 20000 宇宙鑽石 或 !記帳 -10000 出席分配)")
    async def add_record(self, ctx, amount: int, *, description: str):
        data = self.load_data()
        
        # 取得台灣時間
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.datetime.now(tz).strftime("%Y/%m/%d %H:%M")

        # 計算新餘額
        old_balance = data["balance"]
        new_balance = old_balance + amount
        data["balance"] = new_balance

        # 建立交易紀錄
        record = {
            "date": now,
            "amount": amount,
            "description": description,
            "balance_after": new_balance,
            "operator": ctx.author.display_name
        }
        data["history"].append(record)
        self.save_data(data)

        # 回報成功
        action = "🟢 收入" if amount > 0 else "🔴 支出"
        embed = discord.Embed(title="💰 旅團金庫帳目已更新", color=0xf1c40f)
        embed.add_field(name="項目", value=description, inline=True)
        embed.add_field(name=action, value=f"{amount:,} 鑽", inline=True)
        embed.add_field(name="目前總結餘", value=f"**{new_balance:,} 鑽**", inline=False)
        embed.set_footer(text=f"經手人: {ctx.author.display_name} | 時間: {now}")
        
        await ctx.send(embed=embed)

    # 錯誤處理：如果輸入格式不對
    @add_record.error
    async def add_record_error(self, ctx, error):
        if isinstance(error, commands.BadArgument) or isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("⚠️ 格式錯誤！請輸入：`!記帳 [金額] [說明]`\n例如：`!記帳 20000 宇宙鑽石` 或 `!記帳 -10000 出席分配`")

async def setup(bot):
    await bot.add_cog(GuildBank(bot))