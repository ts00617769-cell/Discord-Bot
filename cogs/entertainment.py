"""鍊成、塔羅、星座運勢。"""
from __future__ import annotations

import asyncio
import logging
import random

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands

from services.timeutil import today_taipei_str

logger = logging.getLogger(__name__)


class Entertainment(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="鍊成", help="模擬四合一鍊成。")
    async def alchemy(self, ctx, rarity: str):
        tiers = {"一般": "高級", "高級": "稀有", "稀有": "英雄", "英雄": "傳說", "傳說": "神話"}
        if rarity not in tiers:
            await ctx.send(
                f"❌ {ctx.author.mention} 請輸入正確的階級：一般、高級、稀有、英雄、傳說"
            )
            return

        target_rarity = tiers[rarity]
        success_rate = 0.6
        results = []
        total_success = True

        for i in range(1, 5):
            if random.random() < success_rate:
                results.append(f"第 {i} 柱：✅ 成功")
            else:
                results.append(f"第 {i} 柱：❌ 失敗")
                total_success = False
                break

        if total_success:
            rarity_colors = {
                "神話": 0xFFD700,
                "傳說": 0xA335EE,
                "英雄": 0xFF0000,
                "稀有": 0x0070DD,
                "高級": 0x1EFF00,
            }
            description = "\n".join(results) + f"\n\n🎊 **恭喜！鍊成成功！**\n獲得：**{target_rarity}** 品質"

            if target_rarity in ["英雄", "傳說", "神話"]:
                embed = discord.Embed(
                    title="✨ 鍊成進階成功！",
                    description=description,
                    color=rarity_colors.get(target_rarity, 0xFFFFFF),
                )
                await ctx.send(embed=embed)
            else:
                await ctx.send(f"✅ {ctx.author.mention} 鍊成成功！獲得：**{target_rarity}**")
        else:
            fail_msg = "\n".join(results) + "\n\n崩了... 鍊成失敗，素材已消失。"
            await ctx.send(f"💀 {ctx.author.mention} {fail_msg}")

    @commands.command(name="塔羅", help="抽取今日專屬的大阿爾克那塔羅牌。")
    async def daily_tarot(self, ctx):
        today_str = today_taipei_str().replace("-", "")
        seed = f"tarot_{ctx.author.id}_{today_str}"
        rng = random.Random(seed)

        tarot_cards = {
            "🃏 0. 愚者 (The Fool)": "正位：放下得失心，適合隨手單抽，常有意外驚喜。\n逆位：切忌上頭！絕對不要把留著保底的鑽石拿去亂抽。",
            "✨ 1. 魔術師 (The Magician)": "正位：創造力爆發！鍊成系統成功率體感大增，四柱連過不是夢。\n逆位：素材準備不足，建議先囤貨，不要輕易點鍊成。",
            "📜 2. 女祭司 (The High Priestess)": "正位：直覺敏銳，適合冷靜分析王團掉落機率，精準出手。\n逆位：判斷失誤機率高，分配戰利品或抽卡建議聽從盟友建議，別盲衝。",
            "👑 3. 皇后 (The Empress)": "正位：豐收之日！打怪掉寶率體感上升，適合長時間掛機農資源。\n逆位：資源消耗過快，點裝備或鍊成容易傾家蕩產，請守住錢包。",
            "🛡️ 4. 皇帝 (The Emperor)": "正位：掌控全局！今晚首領戰你將是 MVP，指揮若定，戰利品滿滿。\n逆位：過度固執會吃虧，如果鍊成連爆兩柱就該收手，別硬剛機率。",
            "🗝️ 5. 教皇 (The Hierophant)": "正位：貴人相助，非常適合找公會裡的「歐洲人」幫你代抽。\n逆位：不宜盲從玄學，什麼綠色乖乖今天可能都無效，回歸基本面吧。",
            "💞 6. 戀人 (The Lovers)": "正位：完美契合！跟固定團友組隊打寶會有意想不到的好運。\n逆位：組隊溝通易有摩擦，或是裝備分配可能出現分歧，請保持和氣。",
            "⚔️ 7. 戰車 (The Chariot)": "正位：勇往直前！據點戰大殺四方，氣勢如虹，抽卡也適合大保底硬抽。\n逆位：衝動是魔鬼，方向錯誤的堅持只會讓素材全部化為烏有。",
            "🦁 8. 力量 (Strength)": "正位：以柔克剛，面對 12.96% 的鍊成機率也能穩住心態，最終迎來金光。\n逆位：耐心見底，容易因為連續出綠光而崩潰，建議遠離抽卡介面。",
            "🏮 9. 隱者 (The Hermit)": "正位：低調發大財，深夜獨自一人在冷門頻道單抽，出紫機率高。\n逆位：太過孤立無援，有問題多在頻道問問大家，別自己瞎摸索。",
            "🎡 10. 命運之輪 (Wheel of Fortune)": "正位：迎來轉機！適合挑戰保底池，紫光與金光即將降臨。\n逆位：運勢陷入泥沼，今天非酋體質發揮到極致，請安分守己。",
            "⚖️ 11. 正義 (Justice)": "正位：一分耕耘一分收穫，適合去解每日任務，抽卡機率完全照官方走。\n逆位：覺得系統特別坑？沒錯，今天不適合跟機率拼搏。",
            "⏳ 12. 倒吊人 (The Hanged Man)": "正位：以退為進，現在不是抽卡的好時機，把資源留給下一個卡池。\n逆位：無謂的犧牲，為了衝戰力硬點裝備只會換來一場空。",
            "💀 13. 死神 (Death)": "正位：置之死地而後生！雖然可能先爆幾件裝備，隨後必迎來大突破。\n逆位：泥足深陷，拒絕接受失敗只會越賠越多，該停損了。",
            "🌊 14. 節制 (Temperance)": "正位：資源管理大師！見好就收，只要抽到一張英雄就馬上停手。\n逆位：慾望失控，容易把辛苦農來的鑽石在五分鐘內花光。",
            "😈 15. 惡魔 (The Devil)": "正位：受到致命誘惑！風險極高，但如果敢賭一把大的，或許有奇效。\n逆位：被貪念反噬，小心因為貪圖戰力提升而賠上全部素材。",
            "⚡ 16. 高塔 (The Tower)": "正位：大凶！絕對不要點鍊成，點下去四柱必崩，傾家蕩產。\n逆位：雖然會經歷小失敗（例如單抽全綠），但能避開大災難。",
            "🌟 17. 星星 (The Star)": "正位：大吉！希望之光照耀，傳說與英雄機率大幅提升，請直接開抽。\n逆位：好運稍微延遲，建議晚上首領戰打完之後再來抽卡。",
            "🌙 18. 月亮 (The Moon)": "正位：充滿未知與不安，官方機率今天似乎特別詭異，建議觀望。\n逆位：迷霧散去，終於看清官方的套路，今天是當免費仔的好日子。",
            "☀️ 19. 太陽 (The Sun)": "正位：極吉！陽光普照，充滿歐洲人的氣息，想抽什麼就抽什麼！\n逆位：雖然熱情減退，依然有小收穫，適合抽個 10 抽試試手氣。",
            "🎺 20. 審判 (Judgement)": "正位：過去的累積迎來回報，之前的非氣將一次洗刷，準備迎接紫光。\n逆位：還債時刻，之前太歐的話，今天可能會遇到連續保底的懲罰。",
            "🌍 21. 世界 (The World)": "正位：完美圓滿！心想事成，缺什麼裝備今天就能打到或抽到！\n逆位：距離目標只差最後一哩路，鍊成卡在最後一柱，請保持平常心。",
        }

        drawn_card = rng.choice(list(tarot_cards.keys()))
        interpretation_full = tarot_cards[drawn_card]

        parts = interpretation_full.split("\n逆位：")
        upright_text = parts[0].replace("正位：", "").strip()
        reversed_text = parts[1].strip() if len(parts) > 1 else "無逆位解釋"

        is_upright = rng.choice([True, False])

        if is_upright:
            final_title = f"**{drawn_card} (正位)**"
            final_desc = upright_text
        else:
            final_title = f"**{drawn_card} (逆位) 🙃**"
            final_desc = reversed_text

        embed = discord.Embed(
            title="🔮 塔羅神諭 - 今日遊戲運勢",
            description=f"{ctx.author.mention} 抽出的命運之牌是：",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(name=final_title, value=final_desc, inline=False)
        embed.set_footer(text="※ 命運掌握在自己手中，塔羅僅指引方向。")

        await ctx.send(embed=embed)

    @commands.command(name="星座", aliases=["運勢"])
    async def horoscope(self, ctx, sign: str):
        zodiac_map = {
            "牡羊座": 0,
            "金牛座": 1,
            "雙子座": 2,
            "巨蟹座": 3,
            "獅子座": 4,
            "處女座": 5,
            "天秤座": 6,
            "天蠍座": 7,
            "射手座": 8,
            "摩羯座": 9,
            "水瓶座": 10,
            "雙魚座": 11,
        }

        sign_id = zodiac_map.get(sign)
        if sign_id is None:
            return await ctx.send("❌ 請輸入正確的星座名稱（例如：!星座 牡羊座）")

        today_str = today_taipei_str()

        async with self.bot.db.execute(
            "SELECT content FROM horoscope_cache WHERE date = ? AND sign = ?",
            (today_str, sign),
        ) as cursor:
            cached_result = await cursor.fetchone()

        await self.bot.db.execute(
            "DELETE FROM horoscope_cache WHERE date != ?", (today_str,)
        )
        await self.bot.db.commit()

        if cached_result:
            fortune_text = cached_result[0]
            footer_text = "※ 資料來源：科技紫微網 (⚡ 讀取自資料庫快取)"
        else:
            loading_msg = None
            try:
                loading_msg = await ctx.send(f"🔮 星象儀啟動，正在為 {sign} 觀測今日星象...")
                url = f"https://astro.click108.com.tw/daily_{sign_id}.php?iAstro={sign_id}"
                async with self.bot.session.get(url, timeout=10) as response:
                    response.raise_for_status()
                    html_bytes = await response.read()
                    html = html_bytes.decode("utf-8", errors="ignore")

                soup = BeautifulSoup(html, "html.parser")
                today_content = (
                    soup.find("div", class_="TODAY_CONTENT")
                    or soup.find("div", id="TODAY_CONTENT")
                    or soup.select_one(".TODAY_CONTENT, #dailyStar, .daily_content, .fortune")
                )

                if not today_content:
                    for tag in soup.find_all(["div", "section", "article"]):
                        txt = tag.get_text(" ", strip=True)
                        if "整體運勢" in txt and len(txt) > 40:
                            today_content = tag
                            break

                if today_content:
                    raw_text = today_content.get_text("\n", strip=True)
                    fortune_text = (
                        raw_text.replace("整體運勢", "**整體運勢**")
                        .replace("愛情運勢", "\n\n**愛情運勢**")
                        .replace("事業運勢", "\n\n**事業運勢**")
                        .replace("財運運勢", "\n\n**財運運勢**")
                    )
                    footer_text = "※ 資料來源：科技紫微網即時連線"

                    await self.bot.db.execute(
                        "INSERT OR REPLACE INTO horoscope_cache (date, sign, content) VALUES (?, ?, ?)",
                        (today_str, sign, fortune_text),
                    )
                    await self.bot.db.commit()
                else:
                    fortune_text = (
                        "⚠️ 目前無法取得今日運勢（外部網站版面可能已改版）。\n"
                        "請稍後再試。"
                    )
                    footer_text = "※ 抓取失敗（已降級提示）"
                    logger.warning(f"Horoscope parse miss for {sign}; url={url}")

                if loading_msg:
                    await loading_msg.delete()

            except asyncio.TimeoutError as e:
                logger.error(f"爬蟲逾時: {e}")
                msg = "❌ 目前無法取得今日運勢（連線逾時），請稍後再試。"
                if loading_msg:
                    try:
                        await loading_msg.edit(content=msg)
                    except discord.HTTPException:
                        await ctx.send(msg)
                else:
                    await ctx.send(msg)
                return
            except (aiohttp.ClientError, OSError, ValueError) as e:
                logger.error(f"爬蟲報錯: {e}")
                msg = "❌ 目前無法取得今日運勢（網路異常），請稍後再試。"
                if loading_msg:
                    try:
                        await loading_msg.edit(content=msg)
                    except discord.HTTPException:
                        await ctx.send(msg)
                else:
                    await ctx.send(msg)
                return

        embed = discord.Embed(
            title=f"🌌 今日真實運勢 - {sign}",
            description=fortune_text[:4000],
            color=discord.Color.dark_blue(),
        )
        embed.set_footer(text=footer_text)
        await ctx.send(content=f"✅ {ctx.author.mention}", embed=embed)


async def setup(bot):
    await bot.add_cog(Entertainment(bot))
