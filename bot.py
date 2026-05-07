import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import random
import datetime
import pytz
import json
import sqlite3
import aiohttp
from bs4 import BeautifulSoup



# 💡 關鍵：從外部模組匯入我們分離出去的靜態資料
from game_data import GAP_BOSS_SCHEDULE, WEEKDAY_NAMES, item_names, item_rates, item_map

# 1. 加載環境變數
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# ⚠️ 自動提醒頻道 ID
REMINDER_CHANNEL_ID = 1477964998818140326

# 2. 機器人初始化
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 3. 背景工作：10 分鐘前自動提醒 ---
@tasks.loop(minutes=1)
async def auto_boss_reminder():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.datetime.now(tz)
    ten_mins_later = now + datetime.timedelta(minutes=10)
    target_hour = ten_mins_later.hour
    target_minute = ten_mins_later.minute
    weekday = ten_mins_later.weekday()

    if target_minute == 0 and target_hour in GAP_BOSS_SCHEDULE.get(weekday, []):
        channel = bot.get_channel(REMINDER_CHANNEL_ID)
        if channel:
            time_str = "點、".join(map(str, GAP_BOSS_SCHEDULE[weekday])) + "點"
            embed = discord.Embed(
                title="🕒 時空縫隙首領召喚提醒",
                description=f"**10 分鐘後** 將開始召喚首領！\n\n今天召喚時段\n✅ **{time_str}**",
                color=discord.Color.red()
            )
            await channel.send(content="@everyone", embed=embed)
# -------- 請把這段加在 on_ready 上面 --------
@bot.event
async def setup_hook():
    # 自動掃描 cogs 資料夾，把所有 .py 結尾的擴充卡全部插上去
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            await bot.load_extension(f'cogs.{filename[:-3]}')
            print(f"✅ 模組 {filename} 已成功掛載！")
# ------------------------------------------
@bot.event
async def on_ready():
    print(f'{bot.user} 已成功登入 Discord！')
    # 確保首領提醒雷達有正常啟動
    if not auto_boss_reminder.is_running():
        auto_boss_reminder.start()

# --- 5. 指令：時空查詢 ---
@bot.command(name="時空", help="顯示今天的時空縫隙召喚時間表。")
async def gap_boss_info(ctx):
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.datetime.now(tz)
    today_index = now.weekday() 
    today_name = WEEKDAY_NAMES[today_index]
    times = GAP_BOSS_SCHEDULE.get(today_index, [])
    
    if not times:
        await ctx.send(f"📅 今天是 {today_name}，目前沒有設定召喚。")
        return

    time_str = "點、".join(map(str, times)) + "點"
    embed = discord.Embed(
        title=f"🕒 時空縫隙首領召喚時間表",
        description=f"今天是 **{today_name}**",
        color=discord.Color.purple()
    )
    embed.add_field(name="今天召喚時段", value=f"✅ **{time_str}**", inline=False)
    embed.set_footer(text=f"伺服器目前時間：{now.strftime('%H:%M')}")
    await ctx.send(embed=embed)

# --- 6. 指令：抽卡 ---
@bot.command(name='抽卡', help='結果透過私訊傳送，最高 1000 抽。')
async def gacha(ctx, num_pulls: int = 10):
    if not 0 < num_pulls <= 1000:
        await ctx.send(f"{ctx.author.mention} 抽卡次數須在 1-1000 之間！", delete_after=5)
        return

    rarity_colors = {
        "傳說": 0xa335ee, "英雄": 0xff0000, "稀有": 0x0070dd, "高級": 0x1eff00, "一般": 0x9d9d9d
    }

    results = [item_map[random.choices(item_names, weights=item_rates, k=1)[0]] for _ in range(num_pulls)]

    summary = {}
    for res in results:
        rarity = res["rarity"]
        if rarity not in summary: summary[rarity] = []
        summary[rarity].append(res["name"])

    response_lines = [f"**--- 您的 {num_pulls} 抽結果 ---**"]
    high_rarity_embeds = []

    for r in ["傳說", "英雄", "稀有", "高級", "一般"]:
        if r in summary:
            response_lines.append(f"**{r}** ({len(summary[r])} 張):")
            if r in ["傳說", "英雄"]:
                for item_name in summary[r]:
                    response_lines.append(f"- {item_name}")
                    e = discord.Embed(
                        title="✨ 恭喜抽到頂級物品！",
                        description=f"**{item_name}** ({r})",
                        color=rarity_colors.get(r, 0xffffff)
                    )
                    high_rarity_embeds.append(e)
            else:
                for item_name in summary[r]:
                    response_lines.append(f"- {item_name}")

    full_response = "\n".join(response_lines)

    try:
        if len(full_response) > 2000:
            chunks = [full_response[i:i+1900] for i in range(0, len(full_response), 1900)]
            for chunk in chunks: await ctx.author.send(chunk)
        else:
            await ctx.author.send(full_response)

        for embed in high_rarity_embeds: await ctx.author.send(embed=embed)
        await ctx.send(f"✅ {ctx.author.mention} {num_pulls} 抽結果已送達私訊！", delete_after=5)
    except discord.Forbidden:
        await ctx.send(f"❌ {ctx.author.mention} 我無法傳私訊給你，請開啟隱私設定。")

# --- 7. 指令：純數字抽獎 ---
@bot.command(name="抽", help="隨機抽取一個數字。用法：!抽 100 (代表抽 1~100)")
async def draw_number(ctx, max_val: int = 100):
    if max_val <= 1:
        await ctx.send(f"{ctx.author.mention} 抽獎範圍至少要大於 1 喔！")
        return
    
    lucky_number = random.randint(1, max_val)
    
    embed = discord.Embed(
        title="🎲 隨機抽號碼",
        description=f"從 **1 ~ {max_val}** 之中...",
        color=discord.Color.blue()
    )
    embed.add_field(name="抽出的幸運號碼是：", value=f"✨ **{lucky_number}**", inline=False)
    embed.set_footer(text=f"由 {ctx.author.display_name} 啟動抽獎")
    await ctx.send(embed=embed)

# --- 9. 指令：鍊成系統 ---
@bot.command(name="鍊成", help="模擬四合一鍊成。用法：!鍊成 英雄")
async def alchemy(ctx, rarity: str):
    tiers = {"一般": "高級", "高級": "稀有", "稀有": "英雄", "英雄": "傳說", "傳說": "神話"}
    if rarity not in tiers:
        await ctx.send(f"❌ {ctx.author.mention} 請輸入正確的階級：一般、高級、稀有、英雄、傳說")
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
        rarity_colors = {"神話": 0xffd700, "傳說": 0xa335ee, "英雄": 0xff0000, "稀有": 0x0070dd, "高級": 0x1eff00}
        description = "\n".join(results) + f"\n\n🎊 **恭喜！鍊成成功！**\n獲得：**{target_rarity}** 品質"
        
        if target_rarity in ["英雄", "傳說", "神話"]:
            embed = discord.Embed(title="✨ 鍊成進階成功！", description=description, color=rarity_colors.get(target_rarity, 0xffffff))
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"✅ {ctx.author.mention} 鍊成成功！獲得：**{target_rarity}**")
    else:
        fail_msg = "\n".join(results) + f"\n\n崩了... 鍊成失敗，素材已消失。"
        await ctx.send(f"💀 {ctx.author.mention} {fail_msg}")

# --- 10. 指令：今日塔羅運勢 ---
@bot.command(name="塔羅", help="抽取今日專屬的大阿爾克那塔羅牌。")
async def daily_tarot(ctx):
    today_str = datetime.datetime.now(pytz.timezone('Asia/Taipei')).strftime('%Y%m%d')
    seed = f"tarot_{ctx.author.id}_{today_str}"
    random.seed(seed)
    
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
        "🎡 10. 命運之輪 (Wheel of Fortune)": "正位：迎來轉機！適合直接挑戰 !抽卡 1000，紫光與金光即將降臨。\n逆位：運勢陷入泥沼，今天非酋體質發揮到極致，請安分守己。",
        "⚖️ 11. 正義 (Justice)": "正位：一分耕耘一分收穫，適合去解每日任務，抽卡機率完全照官方走。\n逆位：覺得系統特別坑？沒錯，今天不適合跟機率拼搏。",
        "⏳ 12. 倒吊人 (The Hanged Man)": "正位：以退為進，現在不是抽卡的好時機，把資源留給下一個卡池。\n逆位：無謂的犧牲，為了衝戰力硬點裝備只會換來一場空。",
        "💀 13. 死神 (Death)": "正位：置之死地而後生！雖然可能先爆幾件裝備，但隨後必迎來大突破。\n逆位：泥足深陷，拒絕接受失敗只會越賠越多，該停損了。",
        "🌊 14. 節制 (Temperance)": "正位：資源管理大師！見好就收，只要抽到一張英雄就馬上停手。\n逆位：慾望失控，容易把辛苦農來的鑽石在五分鐘內花光。",
        "😈 15. 惡魔 (The Devil)": "正位：受到致命誘惑！雖然風險極高，但如果敢賭一把大的，或許有奇效。\n逆位：被貪念反噬，小心因為貪圖一時戰力提升而賠上全部素材。",
        "⚡ 16. 高塔 (The Tower)": "正位：大凶！絕對不要點鍊成，點下去四柱必崩，傾家蕩產。\n逆位：雖然會經歷小失敗（例如單抽全綠），但能避開大災難。",
        "🌟 17. 星星 (The Star)": "正位：大吉！希望之光照耀，傳說與英雄機率大幅提升，請直接開抽。\n逆位：好運稍微延遲，建議晚上首領戰打完之後再來抽卡。",
        "🌙 18. 月亮 (The Moon)": "正位：充滿未知與不安，官方機率今天似乎特別詭異，建議觀望。\n逆位：迷霧散去，終於看清官方的套路，今天是當免費仔的好日子。",
        "☀️ 19. 太陽 (The Sun)": "正位：極吉！陽光普照，全身上下充滿歐洲人的氣息，想抽什麼就抽什麼！\n逆位：雖然熱情減退，現在依然有小收穫，適合抽個 10 抽試試手氣。",
        "🎺 20. 審判 (Judgement)": "正位：過去的累積迎來回報，之前的非氣將一次洗刷，準備迎接紫光。\n逆位：還債時刻，之前太歐的話，今天可能會遇到連續保底的懲罰。",
        "🌍 21. 世界 (The World)": "正位：完美圓滿！心想事成，缺什麼裝備今天就能打到或抽到，大圓滿！\n逆位：距離目標只差最後一哩路，可能鍊成卡在最後一柱，請保持平常心。"
    }
    
    drawn_card = random.choice(list(tarot_cards.keys()))
    interpretation_full = tarot_cards[drawn_card]
    
    parts = interpretation_full.split("\n逆位：")
    upright_text = parts[0].replace("正位：", "").strip()
    reversed_text = parts[1].strip() if len(parts) > 1 else "無逆位解釋"

    is_upright = random.choice([True, False])
    
    if is_upright:
        final_title = f"**{drawn_card} (正位)**"
        final_desc = upright_text
    else:
        final_title = f"**{drawn_card} (逆位) 🙃**"
        final_desc = reversed_text

    random.seed()
    
    embed = discord.Embed(
        title="🔮 塔羅神諭 - 今日遊戲運勢",
        description=f"{ctx.author.mention} 抽出的命運之牌是：",
        color=discord.Color.dark_purple()
    )
    embed.add_field(name=final_title, value=final_desc, inline=False)
    embed.set_footer(text="※ 命運掌握在自己手中，塔羅僅指引方向。")
    
    await ctx.send(embed=embed)
# --- 11. 指令：真實星座運勢 (SQLite 快取版 + Big5 強制破譯) ---
@bot.command(name="星座")
async def horoscope(ctx, sign: str): # 🔧 修正 1：刪除了 self
    # 1. 建立星座對照表
    zodiac_map = {
        "牡羊座": 0, "金牛座": 1, "雙子座": 2, "巨蟹座": 3,
        "獅子座": 4, "處女座": 5, "天秤座": 6, "天蠍座": 7,
        "射手座": 8, "摩羯座": 9, "水瓶座": 10, "雙魚座": 11
    }

    # 2. 將文字轉換為 ID
    sign_id = zodiac_map.get(sign)

    if sign_id is None:
        return await ctx.send("❌ 請輸入正確的星座名稱（例如：!運勢 牡羊座）")

    loading_msg = await ctx.send(f"🔮 正在觀測 {sign} 的星象...")

    try:
        # 3. 建立不驗證 SSL 的連線器
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            url = f"https://astro.click108.com.tw/daily_{sign_id}.php?iAstro={sign_id}"
            
            async with session.get(url) as response:
                response.raise_for_status() 
                # 用 utf-8 解碼，並「強制忽略」網頁裡寫壞的字元
                html_bytes = await response.read()
                html = html_bytes.decode('utf-8', errors='ignore')

        # 4. 開始解析 HTML
        soup = BeautifulSoup(html, 'html.parser')
        today_content = soup.find('div', class_='TODAY_CONTENT')
        
        if today_content:
            raw_text = today_content.text.strip()
            # 這裡把標題加粗
            fortune_text = raw_text.replace("整體運勢", "**整體運勢**").replace("愛情運勢", "\n\n**愛情運勢**").replace("事業運勢", "\n\n**事業運勢**").replace("財運運勢", "\n\n**財運運勢**")
            footer_text = "※ 資料來源：科技紫微網即時連線"
        else:
            fortune_text = "⚠️ 星象儀受干擾，無法解析今日運勢。"
            footer_text = "※ 抓取失敗，請稍後重試。"

        # 5. 發送最終報表 (🔧 修正 3：統整發送邏輯並修正縮排)
        embed = discord.Embed(
            title=f"🌌 今日真實運勢 - {sign}",
            description=fortune_text[:4000], 
            color=discord.Color.dark_blue()
        )
        embed.set_footer(text=footer_text)
        
        await loading_msg.delete()
        await ctx.send(content=f"✅ {ctx.author.mention}", embed=embed)

    except Exception as e:
        print(f"爬蟲報錯: {e}")
        await loading_msg.edit(content=f"❌ 連線外部星象資料庫失敗，請確認網路狀態。({e})")
        # 🔧 修正 2：移除了會報錯的 conn.close()
        return

    # 🔧 修正 4：移除了 await bot.load_extension("cogs.exp_tracker")，這交給頂部的 setup_hook 處理就好

# ⚠️ run 永遠在最後一行
bot.run(TOKEN)