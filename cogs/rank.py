import discord
from discord.ext import commands
import aiohttp
import asyncio
import unicodedata
import sqlite3
from game_data import SERVER_MAP

# ==========================================
# 🎨 現代化 UI 組件：伺服器下拉式選單
# ==========================================
class RankUI(discord.ui.View):
    def __init__(self, cog, count):
        super().__init__(timeout=120) # 兩分鐘後選單自動失效
        self.cog = cog
        self.count = count

        # 建立選單選項 (結合 game_data 的伺服器清單)
        options = [discord.SelectOption(label="全服", description="掃描全伺服器綜合排名", emoji="🌐")]
        for srv in SERVER_MAP.keys():
            options.append(discord.SelectOption(label=srv, emoji="🖥️"))
        
        # 實例化下拉式選單
        self.select = discord.ui.Select(
            placeholder="👇 請點擊此處選擇目標伺服器...", 
            min_values=1, 
            max_values=1, 
            options=options
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        selected_server = self.select.values[0]
        
        # 1. 立即回覆互動 (使用 ephemeral=True 讓訊息只有點擊的人看得到)
        await interaction.response.send_message(
            f"✅ 收到指示！正在為您生成 **{selected_server}** (TOP {self.count}) 的戰情報表...", 
            ephemeral=True
        )
        
        # 2. 將報表發送到原本的頻道中
        await self.cog.generate_ranking(interaction.channel, self.count, selected_server)


# ==========================================
# ⚙️ 排名追蹤核心模組
# ==========================================
class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_conn = sqlite3.connect('prasia_data.db', check_same_thread=False)
        self.cursor = self.db_conn.cursor()

    def get_member_info(self, name):
        try:
            self.cursor.execute('SELECT original_identity FROM member_registry WHERE player_name = ?', (name,))
            result = self.cursor.fetchone()
            return f"({result[0]})" if result else ""
        except:
            return ""

    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        try:
            async with session.post(api_url, json=payload) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return json_data.get("data", {}).get("gc") or []
        except Exception:
            pass
        return []

    # 📡 指令入口
    @commands.command(name="排名", help="傳統用法: !排名 25 萊涅01 | 現代用法: !排名 25 (跳出選單)")
    async def get_ranking(self, ctx, *args):
        # 🛡️ 【資安防護網】
        allowed_channel_ids = [1477966312411107493, 1476506457032884328] 
        if ctx.channel.id not in allowed_channel_ids:
            return 

        count = 10
        args_list = list(args)
        
        # 判斷第一個參數是否為數字 (數量)
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 100: count = 100
        if count < 1: count = 10

        # 剩下的參數當作伺服器名稱
        target_server = "".join(args_list) if args_list else ""

        # 💡 【核心邏輯分流】
        # 如果使用者有直接打伺服器名字，就走傳統直接生成的路線
        if target_server:
            await self.generate_ranking(ctx.channel, count, target_server)
            
        # 如果沒打伺服器名字，就彈出精美的下拉式選單 UI
        else:
            view = RankUI(self, count)
            embed = discord.Embed(
                title="📊 伺服器戰力觀測站", 
                description=f"請從下方選單選擇您要掃描的伺服器。\n*(目前設定抓取前 **{count}** 名，若要更改請輸入如 `!排名 50`)*", 
                color=0x2ECC71
            )
            await ctx.send(embed=embed, view=view)


    # 🧠 實際處理排名的後台引擎 (獨立出來給指令與 UI 共同使用)
    async def generate_ranking(self, channel, count, target_server):
        is_global = False
        if target_server in ["全服", "全部", ""]:
            is_global = True
            display_title = f"全伺服器 TOP {count}"
        else:
            if target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await channel.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")
            
            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】 TOP {count}"

        processing_msg = await channel.send(f"🔍 正在潛入橘子主機，彙整 {display_title} 戰情數據...")

        try:
            all_players = []
            async with aiohttp.ClientSession() as session:
                if is_global:
                    tasks = [self.fetch_server_data(session, g_id, w_id) for _, (g_id, w_id) in SERVER_MAP.items()]
                    results = await asyncio.gather(*tasks)
                    for r in results:
                        all_players.extend(r)
                else:
                    players = await self.fetch_server_data(session, target_group_id, target_world_id)
                    all_players.extend(players)

            if not all_players:
                return await processing_msg.edit(content=f"❌ 撈取失敗，找不到資料。")

            all_players.sort(key=lambda x: x.get("gc_exp", 0), reverse=True)
            top_list = all_players[:count]
            
            # ✨ 緊湊雙行排版
            description = "```yaml\n" 
            for idx, p in enumerate(top_list, 1):
                exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                server_info = f"({p.get('world_name', '未知')})" if is_global else ""
                
                name = str(p.get('gc_name') or "未知")
                class_name = str(p.get('class_name', '未知'))
                tag = self.get_member_info(name) 
                display_name = f"{name}{tag}"
                
                level_str = f"Lv.{p.get('gc_level', '?')}"
                
                line1 = f"{idx:02d}. [{display_name}] [{class_name}] {level_str} {server_info}\n"
                line2 = f"    ▶ 經驗值: {exp_zhao:,.2f} 兆\n"
                
                full_line = line1 + line2
                
                if len(description) + len(full_line) > 1900:
                    description += "```"
                    embed = discord.Embed(title=f"🏆 {display_title} (續)", description=description, color=0xffd700)
                    await channel.send(embed=embed)
                    description = "```yaml\n"
                    
                description += full_line
            
            description += "```"
            embed = discord.Embed(title=f"🏆 {display_title}", description=description, color=0xffd700)
            embed.set_footer(text="單位：兆經驗值 | 系統：O(1) 極速伺服器雷達 (支援現代化 UI)")
            
            await processing_msg.delete()
            await channel.send(embed=embed)

        except Exception as e:
            await processing_msg.edit(content=f"❌ 發生嚴重錯誤：{str(e)}")

async def setup(bot):
    await bot.add_cog(RankTracker(bot))