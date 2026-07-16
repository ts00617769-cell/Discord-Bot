import discord
from discord.ext import commands
import random
import json
import asyncio
import os
import logging

logger = logging.getLogger(__name__)

class Temple(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.fortunes = []
        self.load_fortunes()

    def load_fortunes(self):
        """讀取籤詩 JSON 檔案"""
        try:
            # 👇 改用絕對路徑定位：抓取這支腳本的上一層 (專案根目錄)
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, 'omikuji.json')
            
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    self.fortunes = json.load(f)
                logger.info(f"[線上廟宇] 成功載入 {len(self.fortunes)} 首籤詩！")
            else:
                logger.error(f"[線上廟宇] 找不到檔案！預期路徑為：{file_path}")
        except Exception as e:
            logger.error(f"[線上廟宇] 讀取籤詩失敗: {e}")

    @commands.command(name="求籤", help="向菩薩請示。用法: !求籤 [你的問題]")
    async def draw_fortune(self, ctx, *, question: str = None):
        # --- 強制容錯：如果籤筒是空的，立刻執行一次載入 ---
        if not self.fortunes:
            logger.warning("[線上廟宇] 偵測到籤筒為空，嘗試強制重新載入...")
            self.load_fortunes()
            
        if not question:
            await ctx.send(f"❌ {ctx.author.mention} 求籤必須心誠，請把問題說清楚！\n👉 **正確用法**：`!求籤 晚上該不該抽卡？`")
            return

        # 再次檢查是否載入成功
        if not self.fortunes:
            await ctx.send("⚠️ 廟公今天還是不在，籤筒讀取失敗。請通知主人檢查 `omikuji.json` 格式是否正確。")
            return

        # 1. 營造儀式感：發送擲筊中的訊息
        processing_msg = await ctx.send(f"🙏 **{ctx.author.display_name}** 跪在神明桌前，在心中默念：\n> 「*{question}*」\n\n🎲 正在擲筊請示菩薩...")

        # 模擬停頓 2 秒鐘，讓玩家有期待感
        await asyncio.sleep(2)

        # 2. 擲筊邏輯 (機率設定：聖筊50%, 笑筊25%, 陰筊25%)
        # 聖筊 = 一正一反 / 笑筊 = 兩正 / 陰筊 = 兩反
        bwa_bwei = random.choices(["聖筊", "笑筊", "陰筊"], weights=[50, 25, 25])[0]

        if bwa_bwei == "笑筊":
            await processing_msg.edit(content=f"🙏 **{ctx.author.display_name}** 問：「*{question}*」\n\n😅 **【笑筊】 (兩平)**\n菩薩笑了笑不說話，可能是你的問題不夠明確，或是時機未到。\n👉 **請沉澱心思，稍後重新 `!求籤`。**")
            return
        
        elif bwa_bwei == "陰筊":
            await processing_msg.edit(content=f"🙏 **{ctx.author.display_name}** 問：「*{question}*」\n\n❌ **【陰筊】 (兩凸)**\n菩薩表示不妥！神明不同意你的想法，或前方有風險。\n👉 **請放棄這個念頭，或換個方式重新提問。**")
            return

        # 3. 如果是聖筊，則開始抽籤！
        drawn = random.choice(self.fortunes)
        
        # 根據吉凶設定卡片顏色
        color_map = {
            "大吉": 0xff0000, "吉": 0xff5555, "半吉": 0xff8888, "小吉": 0xffaaaa, "末吉": 0xffcccc,
            "末小吉": 0xffdddd, "凶": 0x000000
        }
        embed_color = color_map.get(drawn["type"], 0xf1c40f)

        # 建立精美的籤詩 Embed 卡片
        embed = discord.Embed(
            title=f"⛩️ 觀音靈籤 - 第 {drawn['id']} 籤 【{drawn['type']}】",
            description=f"**{ctx.author.display_name}** 的問題：\n> *{question}*",
            color=embed_color
        )
        
        # ✅ 這裡已經修復了 f-string 的閉合問題
        embed.add_field(name="📜 【籤詩】", value=f"```\n{drawn['poem']}\n```", inline=False)
        embed.add_field(name="💡 【白話解析】", value=drawn['explain'], inline=False)
        
        embed.set_footer(text="✨ 神明指示僅供參考，命運依然掌握在自己手中。")

        # 編輯原本的訊息，秀出聖筊與籤詩
        await processing_msg.edit(content=f"🎉 **【聖筊】！** 菩薩同意賜籤：", embed=embed)

    @commands.command(name="檢查廟宇")
    @commands.is_owner()
    async def check_temple_path(self, ctx):
        """專門用來除錯路徑的指令"""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, 'omikuji.json')

        if os.path.exists(file_path):
            await ctx.send(f"✅ 報告！檔案找到了，位置在：\n`{file_path}`\n已載入籤詩：{len(self.fortunes)} 首")
        else:
            await ctx.send(f"❌ 找不到檔案！預期路徑：\n`{file_path}`")

async def setup(bot):
    await bot.add_cog(Temple(bot))