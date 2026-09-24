# -*- coding: utf-8 -*-
"""ABA 数据看板 - 抓取与展示配置。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent          # processes/aba_keyword_trend_dashboard
DB_PATH = ROOT / "db" / "aba.sqlite"
WEB_DIR = ROOT / "web"
WEB_DATA = WEB_DIR / "data"
# 看板服务（保持 8766；中台本身跑在 8000）
HOST = "0.0.0.0"
PORT = 8766
# 直接填整串 Cookie 时优先使用（留空则读 cookie.txt）
COOKIE = ""
# 产出目录
OUTPUT_DIR = str(ROOT / "output")
COOKIE_CANDIDATES = [
    ROOT / "cookie.txt",
    ROOT / "work" / "cookie.txt",
    ROOT.parent.parent / "data" / "sellersprite_cookie.txt",
]

# ---------- 抓取口径 ----------
# 站点：COM=美国（ABA 数据为 GLOBAL 口径）
MARKET = "COM"
MARKET_ID = 1
# W=周表 / M=月表
REVERSE_TYPE = "W"
# 搜索量下限
MIN_SEARCHES = 5000
# 排名增长量的对比周期：W1 = 近一周（上周）
RANK_GROWTH_TYPE = "W1"
# 每页条数（实测 1000 稳定，2000 亦可）
PAGE_SIZE = 1000
# 请求间隔（秒），别调太低，避免触发风控
MIN_INTERVAL = 0.4
# 是否额外跑一次「不限类目」的全量，用于补齐未归类关键词
INCLUDE_ALL_PASS = True
# 要抓取的类目；None = 自动读取接口返回的全部类目
DEPARTMENTS = None

# 触发流程时是否自动登录卖家精灵刷新 Cookie（解决 Sprite-X-Token 24 小时过期）
AUTO_LOGIN = True

# 前端保留多少周的数据目录（数据库始终保留全部历史，这里只影响 web/data 体积）
KEEP_WEEKS = 8

# ---------- 详情页 ASIN 数据 ----------
# 详情抽屉展示的 ASIN 个数（ABA 接口只给 TOP3 点击占比，另外用 gkDatas 的搜索位前 10 补齐）
ASIN_TOP_N = 10
# 每周抓取结束后，把搜索量前 N 个关键词的 TOP10 ASIN 档案预先抓进缓存（0 = 不预取，完全按需实时抓）
# 每个关键词 1 次请求（约 35KB / 10 个 ASIN），300 个约 3~4 分钟
ASIN_PREFETCH_TOP_N = 300
# ASIN 价格/销量趋势缓存有效期（小时）；超过则由看板按需重新抓
ASIN_TREND_MAX_AGE_HOURS = 168
# 抓 ASIN 数据时的请求间隔（秒）
ASIN_MIN_INTERVAL = 0.25
# 看板详情抽屉是否允许「实时抓取」（关闭后只能看已缓存的数据）
ASIN_LIVE_FETCH = True

# ---------- 展示口径 ----------
TOP_N = 20                 # 榜单条数
TREND_SHARDS = 32          # 趋势数据分片数（前端按需加载）

# ABA 接口返回的类目名（英文）里，有一部分不在 departments 接口中，这里补中文名
# 让某个类目合并到另一个（例如 {"wireless": "mobile"}）。
# 默认留空：ABA 的 departments 接口本身返回 22 个大类，其中 mobile / wireless 是
# 同一个 Amazon 类目的两条记录（词表完全相同），这里保持原样展示，只自动加「同样本」标注。
DEPT_ALIAS = {}

# 只把 departments 接口返回的这 22 个类目当作「大类」；ABA 数据里返回的其它细分类目
# （Video Games / Jewelry / Kindle Store / Home / Furniture …）一律归入「其它细分类目」，
# 这样下拉框和图表严格按 22 个大类展示，不会混进小类。
OFFICIAL_DEPTS_ONLY = True
OFFICIAL_DEPTS = [
    "pets", "electronics", "beauty", "lawngarden", "baby-products", "handmade", "mi",
    "sporting", "arts-crafts", "automotive", "tools", "hpc", "kitchen", "computers",
    "office-products", "mobile", "photo", "industrial", "grocery", "toys-and-games", "fashion",
]
OTHER_DEPT_CODE = "__other__"
OTHER_DEPT_NAME = "其它细分类目"
NONE_DEPT_CODE = "__none__"
NONE_DEPT_NAME = "未归类"

DEPT_NAME_EXTRA = {
    "Video Games": "电子游戏",
    "Kindle Store": "Kindle 电子书",
    "Jewelry": "珠宝首饰",
    "Watches": "手表",
    "Home / Furniture": "家具",
    "Fashion > Shoes": "鞋靴",
    "Prime Pantry": "日用百货",
    "Gift Cards": "礼品卡",
    "Movies & TV": "影视",
    "All Departments": "全部分类",
    "Amazon Devices": "亚马逊设备",
    "Apps & Games": "应用与游戏",
    "Wine": "葡萄酒",
    "Software": "软件",
    "Appliances": "家用电器",
    "Books": "图书",
    "Camera & Photo": "摄影摄像",
    "Collectibles & Fine Art": "收藏品与艺术品",
    "Industrial & Scientific": "工业与科学",
    "Magazine Subscriptions": "杂志订阅",
    "Musical Instruments": "乐器",
    "Office Products": "办公用品",
    "Pet Supplies": "宠物用品",
    "Sports & Outdoors": "运动户外",
    "Tools & Home Improvement": "工具与家居装修",
    "Toys & Games": "玩具",
    "Health & Household": "健康与家居",
    "Baby Products": "母婴",
    "Arts, Crafts & Sewing": "艺术工艺",
    "Automotive": "汽车用品",
    "Beauty & Personal Care": "美容个护",
    "Patio, Lawn & Garden": "庭院草坪",
    "CDs & Vinyl": "音乐唱片",
    "Cell Phones & Accessories": "手机配件",
    "Computers": "电脑",
    "Electronics": "电子",
    "Grocery & Gourmet Food": "食品杂货",
    "Handmade Products": "手工艺品",
    "Home & Kitchen": "家居厨房",
    "Sports & Outdoors ": "运动户外",
    "Collectible Coins": "收藏币",
    "Fine Art": "艺术品",
}
