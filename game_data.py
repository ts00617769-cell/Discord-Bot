# game_data.py — 官方戰情站資料對照
# 資料來源網站：https://warsofprasia.beanfun.com/ （即時戰況 / Ranking / Territory）
# 排名 API：https://warsofprasia.beanfun.com/api/Records/PostLiveapiGCRanking
# 注意：官網為 SPA，伺服器清單由 API 動態載入；靜態另存 HTML 通常不含 world_id。
# 維護時請用機器人指令 `!伺服器檢查` 對 SERVER_MAP 做 API 探活。

# --- 1. 時空縫隙首領時間表 ---
GAP_BOSS_SCHEDULE = {
    0: [23],       # 週一
    1: [13, 23],   # 週二
    2: [17, 23],   # 週三
    3: [21],       # 週四
    4: [11],       # 週五
    5: [1, 15],    # 週六
    6: [19],       # 週日
}
WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# --- 2. 伺服器映射（顯示名 -> world_group_id, world_id）---
# 開服/合服後請更新此表，並執行 !伺服器檢查
SERVER_MAP = {
    "戴摩爾克04": ("livegm_w02", "livegm_w02_r4"),
    "萊涅01": ("livegm_w04", "livegm_w04_r1"),
    "萊涅03": ("livegm_w04", "livegm_w04_r3"),
    "萊涅04": ("livegm_w04", "livegm_w04_r4"),
    "困特03": ("livegm_w06", "livegm_w06_r3"),
    "伊奈司01": ("livegm_w08", "livegm_w08_r1"),
    "基安05": ("livegm_w09", "livegm_w09_r5"),
    "黛庫爾01": ("livegm_w11", "livegm_w11_r1"),
}

# 大區顯示名（僅供參考 / 除錯）
GROUP_MAP = {
    "livegm_w02": "戴摩爾克",
    "livegm_w04": "萊涅",
    "livegm_w06": "困特",
    "livegm_w08": "伊奈司",
    "livegm_w09": "基安",
    "livegm_w11": "黛庫爾",
}
