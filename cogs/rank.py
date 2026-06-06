import discord
from discord.ext import commands
import aiohttp
import asyncio
import unicodedata
from game_data import SERVER_MAP
import os
import logging

logger = logging.getLogger(__name__)

class RankTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # ==========================================
        # 👇 新增這段：開機時就把頻道清單讀好，存進 self 裡面
        # ==========================================
        allowed_channels_str = os.getenv("ALLOWED_COMMAND_CHANNELS", "")
        self.allowed_channel_ids = [int(cid.strip()) for cid in allowed_channels_str.split(",") if cid.strip()]
        # ==========================================

        # 連線到資料庫以抓取成員名牌備註
    async def get_member_info(self, name):
        """抓取團內成員備註的輔助函數"""
        try:
            async with self.bot.db.execute('SELECT original_identity FROM member_registry WHERE player_name = ?', (name,)) as cursor:
                result = await cursor.fetchone()
                return f"({result[0]})" if result else ""
        except aiohttp.ClientError as e:
            logger.error(f"Database connection error while fetching member info for '{name}': {e}")
            return ""
        except Exception as e:
            logger.error(f"Unexpected error while fetching member info for '{name}': {e}")
            return ""

    async def fetch_server_data(self, session, group_id, world_id):
        api_url = "https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking"
        payload = {"world_group_id": group_id, "world_id": world_id, "class": None}
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://warsofprasia.beanfun.com",
            "Referer": "https://warsofprasia.beanfun.com/Main/Ranking"
        }
        try:
            async with session.post(api_url, json=payload, headers=headers, timeout=10) as response:
                if response.status == 200:
                    json_data = await response.json()
                    return (json_data.get("data") or {}).get("gc") or []
        except asyncio.TimeoutError as e:
            logger.error(f"API timeout while fetching ranking for group {group_id}, world {world_id}: {e}")
        except aiohttp.ClientError as e:
            logger.error(f"HTTP client error while fetching ranking: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while fetching ranking data: {e}")
        return []

    @commands.command(name="排名", help="例如: !排名 幻影劍士, !排名 25 萊涅01 咒文刻印使")
    async def get_ranking(self, ctx, *args):
  
        if ctx.channel.id not in self.allowed_channel_ids:
            return

        count = 10
        args_list = list(args)
        
        if len(args_list) > 0 and args_list[0].isdigit():
            count = int(args_list.pop(0))
            
        if count > 100: count = 100
        if count < 1: count = 10

        target_server = "全服"
        target_class = None
        class_parts = []

        for arg in args_list:
            if arg in SERVER_MAP or arg in ["全服", "全部", "global"]:
                target_server = arg
            else:
                class_parts.append(arg)

        if class_parts:
            target_class = "".join(class_parts)

        is_global = target_server in ["全服", "全部", "global"]
        
        filter_msg = f"【{target_class}】" if target_class else " "
        if is_global:
            display_title = f"全伺服器{filter_msg}TOP {count}"
        else:
            if target_server not in SERVER_MAP:
                valid_list = "、".join(SERVER_MAP.keys())
                return await ctx.send(f"❌ 找不到伺服器「{target_server}」。支援：{valid_list} 或 全服")
            
            target_group_id, target_world_id = SERVER_MAP[target_server]
            display_title = f"【{target_server}】{filter_msg}TOP {count}"
        
        # 這裡以下就接上你原本寫好的 try 區塊了
        processing_msg = await ctx.send(f"🔍 正在潛入橘子主機，彙整 {display_title} 即時戰情...")

        try:
            all_players = []
            
            if is_global:
                tasks = [self.fetch_server_data(self.bot.session, g_id, w_id) for _, (g_id, w_id) in SERVER_MAP.items()]
                results = await asyncio.gather(*tasks)
                for r in results:
                    all_players.extend(r)
            else:
                # ✨ 這裡也改用 self.bot.session
                players = await self.fetch_server_data(self.bot.session, target_group_id, target_world_id)
                all_players.extend(players)

            if not all_players:
                return await processing_msg.edit(content=f"❌ 撈取失敗，找不到資料。")

            if target_class:
                all_players = [p for p in all_players if target_class in str(p.get("class_name", ""))]

            if not all_players:
                return await processing_msg.edit(content=f"❌ 找不到符合【{target_class}】職業條件的即時排名資料。")

            all_players.sort(key=lambda x: x.get("gc_exp", 0), reverse=True)
            top_list = all_players[:count]
            
            description = "```yaml\n" 
            for idx, p in enumerate(top_list, 1):
                exp_zhao = p.get("gc_exp", 0) / 1_000_000_000_000
                server_info = f"({p.get('world_name', '未知')})" if is_global else ""
                
                name = str(p.get('gc_name') or "未知")
                class_name = str(p.get('class_name', '未知')) 
                
                # ✨ 這裡要補上 await 來呼叫非同步的函式
                tag = await self.get_member_info(name) 
                
                display_name = f"{name}{tag}"
                level_str = p.get('gc_level', '?')
                
                line = f"{idx:02d}. [{display_name}] [{class_name}] Lv.{level_str} {server_info}\n"
                line += f"    ▶ 經驗值: {exp_zhao:,.2f} 兆\n"
                
                if len(description) + len(line) > 1900:
                    description += "```"
                    embed = discord.Embed(title=f"🏆 {display_title} (續)", description=description, color=0xffd700)
                    await ctx.send(embed=embed)
                    description = "```yaml\n"
                description += line
            
            description += "```"
            embed = discord.Embed(title=f"🏆 {display_title}", description=description, color=0xffd700)
            embed.set_footer(text="單位：兆經驗值 | 系統：即時雷達過濾引擎")
            
            await processing_msg.delete()
            await ctx.send(embed=embed)

        except asyncio.TimeoutError as e:
            logger.error(f"Timeout while fetching ranking data: {e}")
            await processing_msg.edit(content=f"❌ 連線逾時：抓取排名資料花時過久，請重試")
        except aiohttp.ClientError as e:
            logger.error(f"HTTP client error while fetching ranking: {e}")
            await processing_msg.edit(content=f"❌ 網路連線失敗：無法連接到遊戲伺服器")
        except ValueError as e:
            logger.error(f"Data parsing error while formatting ranking: {e}")
            await processing_msg.edit(content=f"❌ 資料解析錯誤：伺服器回傳的資料格式異常")
        except discord.NotFound:
            logger.error("Processing message was deleted before we could edit it")
        except Exception as e:
            logger.error(f"Unexpected error while fetching ranking: {e}")
            try:
                await processing_msg.edit(content=f"❌ 發生嚴重錯誤：{type(e).__name__}")
            except discord.NotFound:
                pass

async def setup(bot):
    await bot.add_cog(RankTracker(bot))