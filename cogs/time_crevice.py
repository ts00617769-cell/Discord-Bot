import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

from services.beanfun_http import get_beanfun_client

logger = logging.getLogger(__name__)

INIT_URL = "https://warsofprasia.beanfun.com/api/Records/PostERGetCrossWorldRaidInit"
INFO_URL = "https://warsofprasia.beanfun.com/api/Records/PostERGetCrossWorldRaidInfo"
CREVICE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://warsofprasia.beanfun.com/TimeCrevice",
}


class TimeCrevice(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="時空王", aliases=["交叉王", "時間隙縫", "時空隙縫", "隙縫戰報", "crevice戰報"])
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def time_crevice(self, ctx, server_name: str = None):
        """
        查詢時間隙縫首領擊殺戰報
        用法: !時空王 [伺服器名稱]
        範例: !時空王 萊涅01
        """
        if not server_name:
            await ctx.send("請指定伺服器名稱，例如：`!時空王 萊涅01`")
            return

        status_msg = await ctx.send(f"正在查詢 **{server_name}** 的時間隙縫資訊...")
        client = get_beanfun_client(self.bot)

        try:
            init_result = await client.post_json(
                INIT_URL, {}, headers=CREVICE_HEADERS, use_cache=False
            )
            if not init_result.ok:
                await status_msg.edit(content="⚠️ 取得資料失敗 (Init API 回應異常)")
                return
            init_data = init_result.data or {}
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as e:
            logger.error(f"TimeCrevice Init Error: {e}")
            await status_msg.edit(content="⚠️ 網路連線異常，請稍後再試")
            return

        data = init_data.get("data", {})
        if not data:
            await status_msg.edit(content="⚠️ API 查無資料")
            return

        seasons = data.get("season", [])
        if not seasons:
            await status_msg.edit(content="⚠️ 目前沒有賽季資訊")
            return

        latest_season = seasons[-1]
        season_seq = latest_season.get("seq")
        season_title = latest_season.get("title", "")
        season_eName = latest_season.get("eName")

        group_members = data.get("groupMember", [])

        target_group_seq = None
        for member in group_members:
            if member.get("seasonSeq") == season_seq and member.get("serverName") == server_name:
                target_group_seq = member.get("groupSeq")
                break

        if target_group_seq is None:
            available_servers = [
                m.get("serverName")
                for m in group_members
                if m.get("seasonSeq") == season_seq
            ]
            servers_str = ", ".join(available_servers) if available_servers else "無"
            if len(servers_str) > 1000:
                servers_str = servers_str[:1000] + "..."
            await status_msg.edit(
                content=f"⚠️ 找不到伺服器 `{server_name}`。\n當前賽季有: {servers_str}"
            )
            return

        matching_group_id = None
        for group in latest_season.get("group", []):
            if group.get("groupSeq") == target_group_seq:
                matching_group_id = group.get("eName")
                break

        if not matching_group_id:
            await status_msg.edit(content="⚠️ 找不到群組 ID")
            return

        payload = {"season": season_eName, "matching_group_id": matching_group_id}

        try:
            info_result = await client.post_json(
                INFO_URL, payload, headers=CREVICE_HEADERS, use_cache=False
            )
            if not info_result.ok:
                await status_msg.edit(content="⚠️ 取得資料失敗 (Info API 回應異常)")
                return
            info_data = info_result.data or {}
        except (asyncio.TimeoutError, aiohttp.ClientError, ValueError) as e:
            logger.error(f"TimeCrevice Info Error: {e}")
            await status_msg.edit(content="⚠️ 網路連線異常，請稍後再試")
            return

        raid_info = info_data.get("data", {}).get("raid_info", [])
        if not raid_info:
            await status_msg.edit(content=f"⚠️ {server_name} 所在的群組尚無首領擊殺紀錄。")
            return

        embed = discord.Embed(
            title=f"⏳ 時間隙縫戰報 - {server_name} ({season_title} 群組 {target_group_seq})",
            color=discord.Color.purple(),
        )
        embed.set_footer(text="資料來源: 波拉西亞戰記官網")

        raid_info.sort(key=lambda x: x.get("date_clear", ""), reverse=True)

        for raid in raid_info[:4]:
            boss_name = raid.get("monster_name", "未知首領")
            date_clear = raid.get("date_clear", "")
            if date_clear:
                date_clear = date_clear.replace("T", " ")
            guild_name = raid.get("guild_name", "")
            kill_server = raid.get("world_name", "")

            mvp_list = raid.get("mvp_list", [])

            damage_mvp = []
            heal_mvp = []
            recv_damage_mvp = []

            for mvp in mvp_list:
                m_type = mvp.get("mvp_type", "")
                name = mvp.get("gc_name", "")

                if m_type.startswith("damage_ranking"):
                    val = mvp.get("damage", "0")
                    damage_mvp.append(f"{name} ({val}%)")
                elif m_type.startswith("heal_ranking"):
                    val = mvp.get("heal", "0")
                    heal_mvp.append(f"{name} ({val}%)")
                elif m_type.startswith("receive_damage_ranking"):
                    val = mvp.get("receive_damage", "0")
                    recv_damage_mvp.append(f"{name} ({val}%)")

            desc = (
                f"**擊殺時間**: {date_clear}\n"
                f"**擊殺伺服器**: {kill_server} ({guild_name})\n"
            )
            if damage_mvp:
                desc += f"⚔️ **輸出 MVP**: {', '.join(damage_mvp)}\n"
            if heal_mvp:
                desc += f"💚 **治療 MVP**: {', '.join(heal_mvp)}\n"
            if recv_damage_mvp:
                desc += f"🛡️ **承傷 MVP**: {', '.join(recv_damage_mvp)}\n"

            embed.add_field(name=f"👹 {boss_name}", value=desc, inline=False)

        await status_msg.edit(content=None, embed=embed)


async def setup(bot):
    await bot.add_cog(TimeCrevice(bot))
