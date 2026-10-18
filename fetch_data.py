#!/usr/bin/env python3
"""抓取各平台限免、特價、新聞,輸出 data.json。只用標準函式庫,不需安裝套件。
任何一個來源失敗時,保留該來源上一次的資料,並把錯誤記在 data.json 的 errors。"""
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

OUT = Path(__file__).parent / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (game-radar; personal project)"}
# CheapShark、RAWG 要求用「說明用途的 User-Agent」,太通用的(例如 Mozilla/5.0)會被擋成 400
API_UA = f"GameRadar/1.0 (+https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'local-run')})"


def api_headers(url):
    return {"User-Agent": API_UA} if ("cheapshark.com" in url or "rawg.io" in url) else HEADERS


def get(url, timeout=25, tries=3):
    """下載網址內容。遇到 429 / 5xx 或連線逾時會自動重試;失敗訊息會附上伺服器回應(不含網址參數,避免洩漏金鑰)。"""
    for attempt in range(tries):
        time.sleep(1.6 if "appdetails" in url else 0.25)  # 放慢一點;Steam 商店詳情約每 5 分鐘 200 次的上限
        req = urllib.request.Request(url, headers=api_headers(url))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            body = e.read().decode("utf-8", "replace")[:150].replace("\n", " ")
            server = e.headers.get("Server", "?") if e.headers else "?"
            raise RuntimeError(f"HTTP {e.code} [{server}] {body} @ {url.split('?')[0]}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise


# ---------- Epic 限免 ----------
def parse_epic(d, en=None):
    """en:{商品 id: 英文名稱},用來和 GamerPower 的英文名稱比對、合併同一款遊戲"""
    items = []
    for e in d["data"]["Catalog"]["searchStore"]["elements"]:
        promos = e.get("promotions") or {}
        slug = (e.get("productSlug") or (e.get("offerMappings") or [{}])[0].get("pageSlug")
                or e.get("urlSlug"))
        url = (f"https://store.epicgames.com/zh-Hant/p/{slug}" if slug
               else "https://store.epicgames.com/zh-Hant/free-games")
        price = (((e.get("price") or {}).get("totalPrice") or {}).get("fmtPrice") or {}).get("originalPrice", "")
        for key, status in (("promotionalOffers", "now"), ("upcomingPromotionalOffers", "next")):
            for grp in promos.get(key) or []:
                for o in grp.get("promotionalOffers", []):
                    if (o.get("discountSetting") or {}).get("discountPercentage") == 0:
                        items.append({"platform": "Epic", "title": e["title"], "title_en": (en or {}).get(e.get("id")), "blurb": re.sub(r"\s+", " ", e.get("description") or "")[:300], "status": status,
                                      "start": o.get("startDate"), "end": o.get("endDate"),
                                      "desc": f"原價 {price}" if price else "", "url": url})
    return items


def fetch_epic():
    url = ("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
           "?locale=zh-Hant&country=TW&allowCountries=TW")
    en = None
    try:   # 另抓英文版取得英文名稱(失敗不影響主要資料)
        d_en = json.loads(get(url.replace("zh-Hant", "en-US")))
        en = {e.get("id"): e["title"] for e in d_en["data"]["Catalog"]["searchStore"]["elements"]}
    except Exception:
        pass
    return parse_epic(json.loads(get(url)), en)


# ---------- Steam 特價 ----------
def parse_steam(d, limit=15):
    items = []
    for g in d["specials"]["items"]:
        pct = g.get("discount_percent", 0)
        if not pct:
            continue
        cur = g.get("currency", "")
        fin, orig = g.get("final_price", 0) / 100, g.get("original_price", 0) / 100
        desc = "限時免費" if fin == 0 else f"-{pct}%  {cur} {fin:,.0f}(原價 {orig:,.0f})"
        items.append({"platform": "Steam", "title": g["name"], "desc": desc, "pct": pct,
                      "url": f"https://store.steampowered.com/app/{g['id']}", "appid": g["id"]})
    items.sort(key=lambda i: -i["pct"])
    return items[:limit]


def fetch_steam():
    url = "https://store.steampowered.com/api/featuredcategories?cc=tw&l=tchinese"
    return parse_steam(json.loads(get(url)))


# ---------- CheapShark 跨商店特價(美元) ----------
STORES = {"1": "Steam", "7": "GOG", "25": "Epic"}


def parse_cheapshark(rows):
    items = []
    for r in rows:
        pct = round(float(r.get("savings", 0)))
        items.append({"platform": STORES.get(str(r.get("storeID")), "其他"), "title": r["title"], "pct": pct,
                      "desc": f"-{pct}%  US${r['salePrice']}(原價 {r['normalPrice']})",
                      "url": "https://www.cheapshark.com/redirect?dealID=" + r["dealID"],
                      "appid": int(r["steamAppID"]) if r.get("steamAppID") else 0})
    return items


def fetch_cheapshark_deals():
    """CheapShark 熱門特價。曾經回傳 400,所以依序嘗試幾種參數 / 編碼寫法,第一個成功的就用。"""
    base = "https://www.cheapshark.com/api/1.0/deals?"
    variants = [
        ({"storeID": "1,7,25", "onSale": 1, "pageSize": 12, "sortBy": "Deal Rating", "metacritic": 75}, urllib.parse.quote_plus),
        ({"storeID": "1,7,25", "onSale": 1, "pageSize": 12, "sortBy": "Deal Rating", "metacritic": 75}, urllib.parse.quote),
        ({"storeID": "1,7,25", "onSale": 1, "pageSize": 12, "sortBy": "Savings"}, urllib.parse.quote),
        ({"onSale": 1, "pageSize": 12}, urllib.parse.quote),
    ]
    errs = []
    for params, via in variants:
        try:
            rows = json.loads(get(base + urllib.parse.urlencode(params, safe=",", quote_via=via)))
            if rows:
                return parse_cheapshark(rows)
            errs.append("空結果")
        except Exception as ex:
            errs.append(str(ex)[:220])
    raise RuntimeError("4 種寫法都失敗,第一個錯誤:" + errs[0])


# ---------- 新聞 RSS ----------
def parse_rss(text, platform, limit=8):
    root = ET.fromstring(text)
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in list(root.iter("item")) + list(root.iter(atom + "entry")):
        title = (it.findtext("title") or it.findtext(atom + "title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            el = it.find(atom + "link")
            link = el.get("href", "") if el is not None else ""
        raw = it.findtext("pubDate") or it.findtext(atom + "updated") or ""
        try:
            date = parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
        except Exception:
            try:
                date = datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
            except Exception:
                date = ""
        if title and link:
            out.append({"platform": platform, "title": title, "url": link, "date": date})
    return sorted(out, key=lambda i: i["date"], reverse=True)[:limit]


def rss(url, platform):
    return lambda: parse_rss(get(url), platform)


def gnews(query, platform):
    q = urllib.parse.quote(query)
    return rss(f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", platform)


# ---------- 新上架 / 即將上架 ----------
_FC = {}


def steam_section(key, limit=20):
    """Steam 首頁分類:new_releases(新上架)、coming_soon(即將上架)"""
    if "d" not in _FC:
        _FC["d"] = json.loads(get("https://store.steampowered.com/api/featuredcategories?cc=tw&l=tchinese"))
    return parse_steam_section(_FC["d"], key, limit)


def parse_steam_section(d, key, limit=20):
    items = []
    for g in d[key]["items"][:limit]:
        fin = g.get("final_price")
        items.append({"platform": "Steam", "title": g["name"],
                      "desc": f"{g.get('currency', '')} {fin / 100:,.0f}" if fin else "",
                      "url": f"https://store.steampowered.com/app/{g['id']}", "appid": g["id"]})
    return items


def parse_release(s):
    """把 Steam 的繁中日期『2026 年 11 月 3 日』轉成 2026-11-03,看不懂就回傳空字串"""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s or "")
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.search(r"\d{4}-\d{2}-\d{2}", s or "")
    return m[0] if m else ""


def plat_label(name):
    n = name.lower()
    for k, v in (("playstation", "PlayStation"), ("xbox", "Xbox"), ("nintendo", "Nintendo")):
        if k in n:
            return v
    return "PC" if n == "pc" else None


RAWG_AGE = {"everyone": 0, "everyone-10-plus": 6, "teen": 12, "mature": 18, "adults-only": 18}


def parse_rawg(d):
    items = []
    for g in d.get("results", []):
        labels = []
        for p in g.get("platforms") or []:
            lab = plat_label(p["platform"]["name"])
            if lab and lab not in labels:
                labels.append(lab)
        items.append({"platform": labels[0] if labels else "其他", "platforms": labels or ["其他"],
                      "title": g["name"], "release": g.get("released") or "", "image": g.get("background_image") or "",
                      "desc": "", "genres": uniq([RAWG_GENRE[x["name"]] for x in g.get("genres") or [] if x.get("name") in RAWG_GENRE]),
                      "age": RAWG_AGE.get((g.get("esrb_rating") or {}).get("slug") or ""),
                      "url": f"https://rawg.io/games/{g['slug']}"})
    return items


def fetch_rawg(days_from, days_to):
    from datetime import timedelta
    t = datetime.now(timezone.utc).date()
    rng = f"{t + timedelta(days=days_from)},{t + timedelta(days=days_to)}"
    url = ("https://api.rawg.io/api/games?key=" + os.environ["RAWG_API_KEY"] +
           f"&dates={rng}&ordering=-added&page_size=20")
    return parse_rawg(json.loads(get(url)))


def merge_dupes(items):
    """同名遊戲只留一筆,把另一筆的平台、封面、日期補進來"""
    out = {}
    for it in items:
        k = norm(it.get("title_en") or it["title"])
        if k not in out:
            out[k] = it
            continue
        a = out[k]
        for f in ("platforms", "image", "release", "appid", "blurb"):
            if it.get(f) and not a.get(f):
                a[f] = it[f]
        if a.get("age") is None and it.get("age") is not None:
            a["age"] = it["age"]
    return list(out.values())


# (來源代號, 放進哪一區, 抓取函式)
SOURCES = [
    ("epic", "free", fetch_epic),
    ("steam", "deals", fetch_steam),
    ("cheapshark", "deals", fetch_cheapshark_deals),
    ("nintendo", "news", rss("https://www.nintendolife.com/feeds/latest", "Nintendo")),
    ("playstation", "news", rss("https://www.pushsquare.com/feeds/latest", "PlayStation")),
    ("xbox", "news", rss("https://www.purexbox.com/feeds/latest", "Xbox")),
    ("ea", "news", gnews("EA Games OR 美商藝電", "EA")),
    ("steam_news", "news", gnews("Steam 遊戲 新聞", "Steam")),
    ("epic_news", "news", gnews("Epic Games Store", "Epic")),
    ("steam_new", "new", lambda: steam_section("new_releases")),
    ("steam_soon", "soon", lambda: steam_section("coming_soon")),
    ("ios_free", "free", lambda: ios_compute()[0]),
    ("ios_deals", "deals", lambda: ios_compute()[1]),
    ("ios_news", "news", rss("https://toucharcade.com/feed/", "iOS")),
    ("ios_news_tw", "news", gnews("App Store 遊戲 限免 OR 特價 OR 新作", "iOS")),
    ("android_news", "news", gnews("Google Play 遊戲 限免 OR 特價 OR 新作", "Android")),
    ("popular", "popular", lambda: fetch_popular()),
    ("cheapshark_pool", "pool", lambda: fetch_cheapshark_pool()),
    ("gamerpower", "free", lambda: fetch_gamerpower()),
    ("steam_free", "free", lambda: steam_free()),
    ("ios_top", "mobile", lambda: ios_top()),
    ("android_top", "mobile", lambda: android_top()),
]
if os.environ.get("RAWG_API_KEY"):  # 有設定金鑰才啟用:涵蓋 PS / Xbox / Nintendo 的新作
    SOURCES += [("rawg_new", "new", lambda: fetch_rawg(-30, 0)),
                ("rawg_soon", "soon", lambda: fetch_rawg(1, 120))]


# ---------- 跨平台比價(PC 商店 + iOS + Android)與手機平台 ----------
_FX = {}
_STORES = {}
_IOS = {}
MOBILE = Path(__file__).parent / "mobile_prices.json"
ALLOW_EMPTY = {"ios_free", "ios_deals", "steam_free"}   # 這些來源沒有項目是正常的(要累積價格紀錄才看得出降價)


def get_fx(prev):
    """美元換台幣匯率;抓不到就沿用上一次的數字"""
    try:
        return float(json.loads(get("https://open.er-api.com/v6/latest/USD"))["rates"]["TWD"])
    except Exception:
        return prev


def store_names():
    if not _STORES:
        try:
            for s in json.loads(get("https://www.cheapshark.com/api/1.0/stores")):
                _STORES[str(s["storeID"])] = s["storeName"]
        except Exception:
            pass
    return _STORES


def fetch_cheapshark(appid):
    """CheapShark:各 PC 商店現價、歷史最低價、英文名稱"""
    rows = json.loads(get(f"https://www.cheapshark.com/api/1.0/games?steamAppID={appid}"))
    if not rows:
        return None
    info = json.loads(get("https://www.cheapshark.com/api/1.0/games?id=" + rows[0]["gameID"]))
    c = info.get("cheapestPriceEver") or {}
    low = ({"usd": c.get("price"), "date": datetime.fromtimestamp(c["date"], timezone.utc).date().isoformat()}
           if c.get("date") else None)
    names = store_names()
    deals = [{"store": names.get(str(d["storeID"]), "商店 " + str(d["storeID"])), "sid": str(d["storeID"]),
              "usd": float(d["price"]), "url": "https://www.cheapshark.com/redirect?dealID=" + d["dealID"]}
             for d in info.get("deals", [])]
    return {"low": low, "deals": deals, "en": (info.get("info") or {}).get("title") or rows[0].get("external", "")}


def similar(a, b):
    """名稱『很像』(包含關係或相似度 ≥ 0.75):用來分辨『名稱不同但可能是同一款』和『根本沒有這款遊戲』。"""
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def find_ios(title):
    """回傳:{"price","url"}=找到;{"similar":True}=有名稱很像的結果但不是同一個;None=完全沒有相關結果。"""
    url = "https://itunes.apple.com/search?entity=software&country=tw&limit=5&term=" + urllib.parse.quote(title)
    seen = False
    for r in json.loads(get(url)).get("results", []):
        if str(r.get("primaryGenreId", "")) != "6014":
            continue
        if norm(r.get("trackName", "")) == norm(title):
            return {"price": float(r.get("price") or 0), "url": r.get("trackViewUrl", "")}
        seen = seen or similar(r.get("trackName", ""), title)
    return {"similar": True} if seen else None


def find_android(title):
    """用非官方的 google-play-scraper 查 Google Play;沒安裝或失敗就略過"""
    try:
        from google_play_scraper import search
    except ImportError:
        raise                       # 沒安裝:讓呼叫端知道是「無法查詢」,而不是「查無此遊戲」
    seen = False
    for r in search(title, lang="zh_TW", country="tw", n_hits=5):
        if (norm(r.get("title", "")) == norm(title) and r.get("price") is not None
                and r.get("currency") in (None, "", "TWD")):
            return {"price": float(r["price"]), "url": "https://play.google.com/store/apps/details?id=" + r["appId"]}
        seen = seen or similar(r.get("title", ""), title)
    return {"similar": True} if seen else None


def build_compare(g, cs, appid):
    fx = _FX.get("rate")
    rows = []
    if g.get("price"):
        rows.append({"store": "Steam(台灣區)", "twd": g["price"]["final"],
                     "url": f"https://store.steampowered.com/app/{appid}"})
    for d in (cs or {}).get("deals", []):
        if d["sid"] == "1":      # CheapShark 的 Steam 是美國區價格,改用上面的台灣區價格
            continue
        rows.append({"store": d["store"], "usd": d["usd"], "twd": round(d["usd"] * fx) if fx else None, "url": d["url"]})
    en = (cs or {}).get("en") or ""
    if en:
        for fn, label in ((find_ios, "App Store(iOS)"), (find_android, "Google Play(Android)")):
            try:
                r = fn(en)
            except Exception:
                r = None
            if r and "price" in r:
                rows.append({"store": label, "twd": r["price"], "url": r["url"]})
    return {"fx": fx, "en": en, "rows": rows} if rows else None


def ios_chart_ids():
    """App Store 台灣區「遊戲」付費榜的 app id。
    新版 RSS(applemarketingtools)的付費榜不含遊戲分類,所以先用舊版 RSS 指定遊戲分類 6014,失敗才退回新版。"""
    try:
        d = json.loads(get("https://itunes.apple.com/tw/rss/toppaidapplications/limit=100/genre=6014/json"))
        entries = d["feed"]["entry"]
        if isinstance(entries, dict):
            entries = [entries]
        ids = [e["id"]["attributes"]["im:id"] for e in entries]
        if ids:
            return ids
    except Exception:
        pass
    return [r["id"] for r in json.loads(get("https://rss.applemarketingtools.com/api/v2/tw/apps/top-paid/100/apps.json"))["feed"]["results"]]


def ios_age(s):
    m = re.search(r"\d+", str(s or ""))
    if not m:
        return None
    n = int(m[0])
    return 0 if n <= 4 else 6 if n <= 9 else 12 if n <= 12 else 18


def parse_ios_lookup(d):
    apps = []
    for r in d.get("results", []):
        if str(r.get("primaryGenreId", "")) != "6014":   # 6014 = 遊戲;只收主分類是遊戲的,避免把學習、工具類 App 混進來
            continue
        apps.append({"id": str(r["trackId"]), "title": r["trackName"], "price": float(r.get("price") or 0),
                     "cur": r.get("currency", "TWD"), "url": r.get("trackViewUrl", ""),
                     "img": r.get("artworkUrl512") or r.get("artworkUrl100", ""),
                     "genres": uniq([IOS_GENRE[str(i)] for i in r.get("genreIds", []) if str(i) in IOS_GENRE]),
                     "age": ios_age(r.get("contentAdvisoryRating") or r.get("trackContentRating")),
                     "blurb": re.sub(r"\s+", " ", r.get("description") or "")[:300],
                     "rating": float(r["averageUserRating"]) if r.get("averageUserRating") else None,
                     "count": int(r.get("userRatingCount") or 0)})
    return apps


def ios_compute():
    """App Store 台灣區付費榜:每次記錄價格,現價低於近 60 天高點就算特價 / 限免"""
    if "d" in _IOS:
        return _IOS["d"]
    ids = ",".join(ios_chart_ids())
    apps = parse_ios_lookup(json.loads(get("https://itunes.apple.com/lookup?country=tw&id=" + ids)))
    hist = load(MOBILE, {})
    today = datetime.now(timezone.utc).date()
    free, deals = [], []
    for a in apps:
        h = hist.get(a["id"], {})
        peak, pd = h.get("max", a["price"]), h.get("pd", today.isoformat())
        if (today - datetime.fromisoformat(pd).date()).days > 60 or a["price"] >= peak:
            peak, pd = a["price"], today.isoformat()
        hist[a["id"]] = {"title": a["title"], "price": a["price"], "max": peak, "pd": pd, "seen": today.isoformat()}
        base = {"platform": "iOS", "title": a["title"], "url": a["url"], "image": a["img"], "square": True, "age": a["age"], "genres": a["genres"], "blurb": a["blurb"]}
        if a["price"] == 0 and peak > 0:
            free.append({**base, "status": "now", "desc": f"原價 {a['cur']} {peak:,.0f}"})
        elif 0 < a["price"] < peak:
            pct = round((1 - a["price"] / peak) * 100)
            deals.append({**base, "pct": pct, "desc": f"-{pct}%  {a['cur']} {a['price']:,.0f}(原價 {peak:,.0f})"})
    hist = {k: v for k, v in hist.items() if (today - datetime.fromisoformat(v["seen"]).date()).days <= 60}
    MOBILE.write_text(json.dumps(hist, ensure_ascii=False), "utf-8")
    deals.sort(key=lambda i: -i["pct"])
    _IOS["apps"] = apps
    _IOS["d"] = (free, deals)
    return _IOS["d"]


# ---------- 手機付費遊戲榜(iOS / Android 各取評價最好且評論最多的前 10 名) ----------
ANDROID_QUERIES = ["買斷 遊戲", "付費 遊戲 無廣告", "premium game no ads", "paid rpg game",
                   "paid strategy game", "paid puzzle game", "paid platformer game"]   # Android 沒有榜單,靠這些關鍵字找候選
ANDROID_CACHE = Path(__file__).parent / "android_top.json"


def bayes(r, n, m=4.2, c=200):
    """貝氏平均:評分高、評論多的排前面,評論太少的高分不會直接上榜"""
    return (n * r + c * m) / (n + c)


def android_age(s):
    s = str(s or "")
    if re.search(r"Adults|成人|18", s):
        return 18
    if re.search(r"Mature|17|成熟", s):
        return 18
    if re.search(r"Teen|青少年|12", s):
        return 12
    if "10" in s:
        return 6
    if re.search(r"Everyone|所有|3\+", s):
        return 0
    return None


def ios_top(n=10):
    ios_compute()
    pool = [a for a in _IOS["apps"] if a["price"] > 0 and a.get("rating") and a.get("count")]
    pool.sort(key=lambda a: -bayes(a["rating"], a["count"]))
    return [{"platform": "iOS", "rank": i, "title": a["title"], "desc": f"{a['cur']} {a['price']:,.0f}",
             "url": a["url"], "image": a["img"], "square": True, "age": a["age"],
             "rating": round(a["rating"], 1), "count": a["count"], "genres": a["genres"], "blurb": a["blurb"]} for i, a in enumerate(pool[:n], 1)]


def android_top(n=10):
    c = load(ANDROID_CACHE, {})
    if c.get("t") and c.get("items") and \
            (datetime.now(timezone.utc) - datetime.fromisoformat(c["t"])).total_seconds() < 20 * 3600:
        return c["items"]                      # 一天只重抓一次,避免請求太多
    from google_play_scraper import app, search
    cands = {}
    for q in ANDROID_QUERIES:
        for r in search(q, lang="zh_TW", country="tw", n_hits=30):
            if ((r.get("price") or 0) > 0 and str(r.get("genreId") or "GAME").startswith("GAME")
                    and (r.get("score") or 0) >= 4.0):
                cands.setdefault(r["appId"], r)
        time.sleep(0.5)
    detail = []
    for appid in sorted(cands, key=lambda k: -(cands[k].get("score") or 0))[:30]:
        try:
            d = app(appid, lang="zh_TW", country="tw")
        except Exception:
            continue
        time.sleep(0.5)
        if d.get("score") and d.get("ratings") and (d.get("price") or 0) > 0:
            detail.append(d)
    detail.sort(key=lambda d: -bayes(d["score"], d["ratings"]))
    items = [{"platform": "Android", "rank": i, "title": d["title"],
              "desc": f"{d.get('currency') or 'TWD'} {d['price']:,.0f}",
              "url": d.get("url") or "https://play.google.com/store/apps/details?id=" + d["appId"],
              "image": d.get("icon", ""), "square": True, "age": android_age(d.get("contentRating")),
              "blurb": re.sub(r"\s+", " ", d.get("summary") or d.get("description") or "")[:300],
              "genres": [ANDROID_GENRE[d["genreId"]]] if d.get("genreId") in ANDROID_GENRE else [],
              "rating": round(d["score"], 1), "count": d["ratings"]} for i, d in enumerate(detail[:n], 1)]
    if items:
        ANDROID_CACHE.write_text(json.dumps({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                             "items": items}, ensure_ascii=False), "utf-8")
    return items


# ---------- 限時免費:GamerPower(各平台限免/贈送)與 Steam 特價中 100% 折扣 ----------
NO_STEAM = {"iOS", "Android", "PlayStation", "Xbox", "Nintendo", "Meta"}   # 這些平台的限免不用去 Steam 比對 appid


BANNED_PLATFORM = re.compile(r"itch|drm|stove|meta|rift|oculus|quest|^vr$", re.I)   # itch.io、DRM-Free 領取流程不直覺;Stove、Meta / Rift(VR)不是台灣常用的平台 → 不收錄(想排除別的平台就加在這個正規表示式裡)


def gp_labels(s):
    out = []
    for p in [x.strip() for x in (s or "").split(",")]:
        if BANNED_PLATFORM.search(p):
            continue
        l = p.lower()
        lab = ("Meta" if ("meta" in l or "oculus" in l or "quest" in l or l == "vr") else
               "Steam" if "steam" in l else "Epic" if "epic" in l else "GOG" if "gog" in l else
               "EA" if ("origin" in l or l.startswith("ea")) else "Ubisoft" if "ubisoft" in l else
               "PlayStation" if ("playstation" in l or l.startswith("ps")) else "Xbox" if "xbox" in l else
               "Nintendo" if ("nintendo" in l or "switch" in l) else "iOS" if l == "ios" else
               "Android" if "android" in l else "PC" if l in ("pc", "windows") else None)
        lab = lab or p            # 對照不到的平台保留原本的名稱,不再統一寫「其他」
        if lab and lab not in out:
            out.append(lab)
    return out


def parse_gamerpower(rows):
    items = []
    for g in rows:
        if str(g.get("status", "Active")).lower() != "active":
            continue
        raw = [x.strip() for x in str(g.get("platforms") or "").split(",") if x.strip()]
        sm = re.search(r"\(([^)]*)\)\s*(?:Key\s*)?Giveaway\s*$", g.get("title", ""))
        title_store = sm.group(1) if sm else ""            # 標題括號裡的商店,例如「(Stove)」
        rest = [x for x in raw if not BANNED_PLATFORM.search(x)]
        hit = len(rest) < len(raw) or BANNED_PLATFORM.search(title_store)
        if hit and not any(x.lower() not in ("pc", "windows") for x in rest):
            continue                      # 被排除的平台(itch.io、DRM-Free、Stove)之外沒有其他具體商店(只剩泛用的 PC),整筆略過
        labs = gp_labels(g.get("platforms"))
        m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", str(g.get("end_date", "")))
        for x in (gp_labels(sm.group(1)) if sm else []):          # 標題括號裡的商店若不在平台清單,補上去(只補認得的商店,避免把「(Remastered)」之類當成平台)
            if x not in labs and x in ("Steam", "Epic", "GOG", "EA", "Ubisoft", "PlayStation", "Xbox", "Nintendo", "iOS", "Android"):
                labs.append(x)
        title = re.sub(r"\s*\([^)]*\)\s*Giveaway\s*$", "", g.get("title", ""))
        title = re.sub(r"\s+Giveaway\s*$", "", title).strip()
        title = re.sub(r"\s*\((?:[^)]*(?:steam|epic|gog|ubisoft|origin|\bea\b|xbox|playstation|switch|battle\.net)[^)]*)\)\s*(?:key|code)?\s*$", "", title, flags=re.I).strip()   # 「Dwarven Realms (Steam) Key」→「Dwarven Realms」
        worth = str(g.get("worth", ""))
        url = g.get("open_giveaway_url") or g.get("claim_url") or g.get("gamerpower_url") or g.get("open_giveaway") or ""
        items.append({"platform": next((l for l in labs if l != "PC"), labs[0] if labs else "其他"),
                      "platforms": labs or ["其他"], "title": title, "status": "now",
                      "end": f"{m[1]}T{m[2]}Z" if m else None,      # GamerPower 的結束時間當作 UTC
                      "desc": f"原價 US{worth}" if worth.startswith("$") else "",
                      "url": url, "image": g.get("image") or g.get("thumbnail") or "",
                      "blurb": re.sub(r"\s+", " ", g.get("description") or "")[:300]})
    return items


def fetch_gamerpower():
    return parse_gamerpower(json.loads(get("https://www.gamerpower.com/api/giveaways?type=game&sort-by=date")))


def steam_free():
    """Steam 首頁特價清單中折扣 100% 的遊戲(限時免費領取)"""
    if "d" not in _FC:
        _FC["d"] = json.loads(get("https://store.steampowered.com/api/featuredcategories?cc=tw&l=tchinese"))
    items = []
    for g in _FC["d"]["specials"]["items"]:
        if g.get("discount_percent") == 100 or (g.get("final_price") == 0 and g.get("discount_percent")):
            orig = (g.get("original_price") or 0) / 100
            items.append({"platform": "Steam", "title": g["name"], "status": "now",
                          "desc": f"原價 {g.get('currency', '')} {orig:,.0f}" if orig else "",
                          "url": f"https://store.steampowered.com/app/{g['id']}", "appid": g["id"]})
    return items


def dedupe_by_appid(items):
    seen, out = {}, []
    for it in items:
        k = it.get("appid")
        if k and k in seen:
            a = seen[k]
            for f in ("platforms", "image", "end"):
                if it.get(f) and not a.get(f):
                    a[f] = it[f]
            continue
        if k:
            seen[k] = it
        out.append(it)
    return out


# ---------- 歷史新低:現價不高於歷史最低價的遊戲 ----------
def fetch_cheapshark_pool(pages=2, size=60):
    """多抓一批 Steam 特價當候選(不直接顯示),用來找出『目前就是史低』的遊戲。只收 Steam 好評率 70% 以上的。"""
    base = "https://www.cheapshark.com/api/1.0/deals?"
    variants = [
        ({"storeID": "1", "onSale": 1, "pageSize": size, "sortBy": "Deal Rating", "steamRating": 70}, urllib.parse.quote_plus),
        ({"storeID": "1", "onSale": 1, "pageSize": size, "sortBy": "Deal Rating", "steamRating": 70}, urllib.parse.quote),
        ({"storeID": "1", "onSale": 1, "pageSize": size, "sortBy": "Deal Rating"}, urllib.parse.quote),
        ({"storeID": "1", "onSale": 1, "pageSize": size}, urllib.parse.quote),
    ]
    out, working, last = [], None, None
    for page in range(pages):
        got = None
        for params, via in ([working] if working else variants):
            try:
                rows = json.loads(get(base + urllib.parse.urlencode({**params, "pageNumber": page}, safe=",", quote_via=via)))
                if rows:
                    got, working = rows, (params, via)
                    break
            except Exception as ex:
                last = ex
        if not got:
            break
        out += [{"platform": "Steam", "title": r["title"], "appid": int(r["steamAppID"])} for r in got if r.get("steamAppID")]
    if not out:
        raise last or ValueError("沒有資料")
    return out


def build_lows(new, games):
    """現價(台幣)≤ 歷史最低價(美元換算台幣)的 102% 就算史低;必須是目前有折扣的遊戲。"""
    fx = games.get("fx")
    out, seen = [], set()
    for sec in ("free", "deals", "popular", "new", "soon", "pool"):
        for it in new.get(sec, []):
            k = it.get("appid")
            if not k or k in seen:
                continue
            seen.add(k)
            g = games["apps"].get(str(k)) or {}
            pr, low = g.get("price"), g.get("low")
            if not (fx and pr and low and low.get("usd") and pr.get("final", 0) > 0 and pr.get("pct", 0) > 0):
                continue
            ratio = pr["final"] / (float(low["usd"]) * fx) if float(low["usd"]) > 0 else None
            if ratio is None or ratio > 1.02:
                continue
            rv = g.get("reviews") or {}
            tot, posr = rv.get("total") or 0, rv.get("pct") or 0
            score = (posr * tot / 100 + 35) / (tot + 50)      # 貝氏平均:好評率高且評論多的分數高
            out.append({"platform": "Steam", "title": g.get("name") or it.get("title", ""), "appid": k,
                        "pct": pr["pct"], "ratio": round(ratio, 2), "score": round(score, 4), "reviews": tot,
                        "desc": f"-{pr['pct']}%  {pr['cur']} {pr['final']:,.0f}(原價 {pr['orig']:,.0f})",
                        "url": f"https://store.steampowered.com/app/{k}"})
    out.sort(key=lambda i: (-i["score"], -i["reviews"]))      # 依評價與評論數由高到低
    return out


# ---------- 大家都在玩:Steam 目前同時在線人數 ----------
def fetch_popular(n=50, pool=100):
    """Steam 官方公開資料(不需金鑰):先取『最多人玩』榜單前 100 名,再逐款查目前在線人數,依人數排序取前 n 名。"""
    d = json.loads(get("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"))
    items = []
    for r in d["response"]["ranks"][:pool]:
        appid = r["appid"]
        try:
            cur = json.loads(get(f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}"))["response"]["player_count"]
        except Exception:
            continue                       # 少數 app 查不到人數
        items.append({"platform": "Steam", "appid": appid, "title": "", "players": cur,
                      "peak": r.get("peak_in_game", 0), "url": f"https://store.steampowered.com/app/{appid}"})
    if not items:
        raise ValueError("沒有取得任何在線人數")
    items.sort(key=lambda i: -i["players"])
    return items[:n]


# ---------- 遊戲詳情(畫面、影片、評論)與價格歷史 ----------
GAMES = Path(__file__).parent / "games.json"
HISTORY = Path(__file__).parent / "history.json"
MAX_APPS = 240          # 每次最多處理幾款遊戲,避免請求過多
SCHEMA = 3            # 詳情資料格式版本;舊版資料(缺比價 / 史低)會自動重抓一次
_CS_ERR = []


class Unavailable(Exception):
    """Steam 回報這個 app 查不到(下架、台灣區不提供、或不是遊戲),不算錯誤"""


_DETAIL_ERR = []
REFRESH_HOURS = 20     # 詳情超過這個時數才重抓


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def norm(t):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", t.lower())


def find_appid(title):
    """用遊戲名稱找 Steam appid。回傳 (appid, 狀態):
    exact=名稱完全相符;similar=有名稱很像的結果但不是同一個;none=完全沒有相關結果(這款遊戲大概不在 Steam 上)。先用英文名稱搜尋,再用繁中。"""
    seen = False
    for lang in ("english", "tchinese"):
        url = f"https://store.steampowered.com/api/storesearch/?cc=tw&l={lang}&term=" + urllib.parse.quote(title)
        for it in json.loads(get(url)).get("items", []):
            if norm(it.get("name", "")) == norm(title):
                return it["id"], "exact"
            seen = seen or similar(it.get("name", ""), title)
    return 0, ("similar" if seen else "none")


def tw_age(ratings, required_age=0):
    """把 Steam 提供的 PEGI / ESRB / required_age 換算成台灣分級的 0/6/12/15/18。
    這是參考換算,不是台灣官方分級;完全沒資料時回傳 None。"""
    r = ratings or {}
    m = re.search(r"\d+", str((r.get("pegi") or {}).get("rating", "")))
    if m:
        n = int(m[0])
        return 0 if n <= 3 else 6 if n <= 7 else 12 if n <= 12 else 15 if n <= 16 else 18
    esrb = str((r.get("esrb") or {}).get("rating", "")).lower()
    if esrb in {"e": 0, "e10": 6, "t": 12, "m": 18, "ao": 18}:
        return {"e": 0, "e10": 6, "t": 12, "m": 18, "ao": 18}[esrb]
    try:
        ra = int(required_age or 0)
    except ValueError:
        ra = 0
    if ra:
        return 0 if ra < 6 else 6 if ra < 12 else 12 if ra < 15 else 15 if ra < 17 else 18
    return None


# ---------- 遊戲類型(統一成同一組中文分類) ----------
STEAM_GENRE = {"1": "動作", "2": "策略", "3": "角色扮演", "4": "休閒", "9": "競速", "18": "運動",
               "23": "獨立", "25": "冒險", "28": "模擬", "29": "多人線上"}      # 用 Steam 的分類編號,不受語言影響
IOS_GENRE = {"7001": "動作", "7002": "冒險", "7003": "休閒", "7004": "卡牌桌遊", "7005": "卡牌桌遊", "7006": "休閒",
             "7007": "卡牌桌遊", "7008": "教育親子", "7009": "教育親子", "7011": "音樂", "7012": "益智",
             "7013": "競速", "7014": "角色扮演", "7015": "模擬", "7016": "運動", "7017": "策略", "7018": "益智", "7019": "益智"}
ANDROID_GENRE = {"GAME_ACTION": "動作", "GAME_ADVENTURE": "冒險", "GAME_ARCADE": "休閒", "GAME_BOARD": "卡牌桌遊",
                 "GAME_CARD": "卡牌桌遊", "GAME_CASINO": "休閒", "GAME_CASUAL": "休閒", "GAME_EDUCATIONAL": "教育親子",
                 "GAME_MUSIC": "音樂", "GAME_PUZZLE": "益智", "GAME_RACING": "競速", "GAME_ROLE_PLAYING": "角色扮演",
                 "GAME_SIMULATION": "模擬", "GAME_SPORTS": "運動", "GAME_STRATEGY": "策略", "GAME_TRIVIA": "益智",
                 "GAME_WORD": "益智"}
RAWG_GENRE = {"Action": "動作", "Indie": "獨立", "Adventure": "冒險", "RPG": "角色扮演", "Strategy": "策略",
              "Shooter": "射擊", "Casual": "休閒", "Simulation": "模擬", "Puzzle": "益智", "Arcade": "休閒",
              "Platformer": "平台跳躍", "Racing": "競速", "Massively Multiplayer": "多人線上", "Sports": "運動",
              "Fighting": "格鬥", "Family": "教育親子", "Board Games": "卡牌桌遊", "Educational": "教育親子", "Card": "卡牌桌遊"}


def uniq(xs):
    return list(dict.fromkeys(xs))


def steam_genres(gs):
    return uniq([STEAM_GENRE[str(g.get("id"))] for g in gs or [] if str(g.get("id")) in STEAM_GENRE])


def parse_details(d):
    po = d.get("price_overview") or {}
    movies = []
    for m in (d.get("movies") or [])[:2]:
        mp4 = m.get("mp4") or {}
        movies.append({"name": m.get("name", ""), "thumb": m.get("thumbnail", ""),
                       "mp4": mp4.get("480") or mp4.get("max") or ""})
    rel = (d.get("release_date") or {}).get("date", "")
    return {"age": tw_age(d.get("ratings"), d.get("required_age")), "genres": steam_genres(d.get("genres")), "release": rel, "release_iso": parse_release(rel), "name": d.get("name", ""), "header": d.get("header_image", ""), "desc": d.get("short_description", ""),
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "shots": [{"t": s.get("path_thumbnail", ""), "f": s.get("path_full", "")}
                      for s in (d.get("screenshots") or [])[:6]],
            "movies": movies,
            "price": ({"final": po["final"] / 100, "orig": po["initial"] / 100,
                       "cur": po.get("currency", ""), "pct": po.get("discount_percent", 0)} if po else None)}


def parse_reviews(summ, txt):
    q = summ.get("query_summary", {})
    pos, neg = q.get("total_positive", 0), q.get("total_negative", 0)
    tot = pos + neg
    top = []
    for r in txt.get("reviews", [])[:3]:
        body = re.sub(r"\[/?[a-zA-Z0-9*]+[^\]]*\]", "", r.get("review", "")).strip()  # 去掉 [h1] 之類標記
        top.append({"text": body[:220], "up": bool(r.get("voted_up")),
                    "hours": round((r.get("author") or {}).get("playtime_forever", 0) / 60)})
    return {"label": q.get("review_score_desc", ""), "pct": round(pos * 100 / tot) if tot else None,
            "total": tot, "top": top}


def fetch_lowest(appid):
    """CheapShark 記錄的歷史最低價(美元)。"""
    rows = json.loads(get(f"https://www.cheapshark.com/api/1.0/games?steamAppID={appid}"))
    if not rows:
        return None
    c = json.loads(get("https://www.cheapshark.com/api/1.0/games?id=" + rows[0]["gameID"])).get("cheapestPriceEver") or {}
    if not c.get("date"):
        return None
    return {"usd": c.get("price"), "date": datetime.fromtimestamp(c["date"], timezone.utc).date().isoformat()}


def fetch_game(appid):
    raw = json.loads(get(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=tw&l=tchinese"))
    entry = raw[str(appid)]
    if not entry.get("success"):
        raise Unavailable("appdetails 回傳失敗")
    g = parse_details(entry["data"])
    base = f"https://store.steampowered.com/appreviews/{appid}?json=1&filter=all&review_type=all&purchase_type=all"
    summ = json.loads(get(base + "&language=all&num_per_page=0"))
    txt = json.loads(get(base + "&language=tchinese&num_per_page=3"))
    if not txt.get("reviews"):
        txt = json.loads(get(base + "&language=english&num_per_page=3"))
    g["reviews"] = parse_reviews(summ, txt)
    try:
        cs = fetch_cheapshark(appid)
    except Exception as ex:
        cs = None
        _CS_ERR.append(str(ex)[:160])
    g["low"] = cs["low"] if cs else None
    try:
        g["compare"] = build_compare(g, cs, appid)
    except Exception:
        g["compare"] = None
    return g


def free_prices(new, games):
    """替限免項目查 iOS / Android 的價格。找不到標記為 none(前端顯示『查無』);查詢本身失敗則不顯示,避免誤導。"""
    for it in new["free"]:
        if it["platform"] in NO_STEAM or it.get("prices") is not None:   # 主機 / VR / 手機專屬的限免不需要查
            continue
        g = games["apps"].get(str(it.get("appid") or "")) or {}
        en = it.get("title_en") or (g.get("compare") or {}).get("en") or it["title"]
        have = it.get("platforms") or [it["platform"]]
        rows = []
        for fn, label in ((find_ios, "iOS"), (find_android, "Android")):
            if label in have:
                continue
            try:
                r = fn(en)
            except Exception:
                continue
            if r and "price" in r:
                rows.append({"store": label, "twd": r["price"], "url": r["url"]})
        it["prices"] = rows


def enrich(new):
    """替限免/特價項目補上 appid,更新 games.json(詳情)與 history.json(每日價格)。回傳失敗數。"""
    games = load(GAMES, {"apps": {}, "map": {}})
    hist = load(HISTORY, {})
    games["fx"] = get_fx(games.get("fx"))
    _FX["rate"] = games["fx"]
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    fails, ids = 0, []
    if games.get("map_v") != 3:      # 配對方式改良了(多記錄「名稱很像 / 完全沒有」):過去記成「查無」的遊戲重試一次
        games["map"] = {k: v for k, v in games["map"].items() if v}
        games["hint"] = {}
        games["map_v"] = 3
    for sec in ("free", "deals", "popular", "new", "soon", "pool"):
        for it in new[sec]:
            if not it.get("appid") and sec == "free" and it["platform"] not in NO_STEAM:
                name = it.get("title_en") or it["title"]
                key = norm(name)
                if key not in games["map"]:
                    try:
                        games["map"][key], hint = find_appid(name)
                        games.setdefault("hint", {})[key] = hint
                    except Exception:
                        fails += 1
                        continue
                it["appid"] = games["map"][key]
                if not it["appid"]:
                    it["steam"] = games.get("hint", {}).get(key, "none")      # similar=名稱很像但沒對上;none=Steam 上沒有
            if it.get("appid") and it["appid"] not in ids:
                ids.append(it["appid"])
    bad = games.setdefault("bad", {})
    for appid in ids[:MAX_APPS]:
        k = str(appid)
        if k in bad and (now.date() - datetime.fromisoformat(bad[k]).date()).days < 14:
            continue                                  # 這個 app 最近確認查不到,14 天內不重試
        old = games["apps"].get(k)
        if old and old.get("v") == SCHEMA and (now - datetime.fromisoformat(old["fetched"])).total_seconds() < REFRESH_HOURS * 3600:
            old["seen"] = today
            continue
        try:
            g = fetch_game(appid)
        except Unavailable:
            bad[k] = today
            continue
        except Exception as ex:
            fails += 1
            _DETAIL_ERR.append(f"app {appid}: {type(ex).__name__} {str(ex)[:80]}")
            continue
        g["fetched"], g["seen"], g["v"] = now.isoformat(timespec="seconds"), today, SCHEMA
        games["apps"][k] = g
        if g.get("price"):
            pts = hist.setdefault(k, [])
            if pts and pts[-1][0] == today:
                pts[-1][1] = g["price"]["final"]      # 同一天只留最後一筆
            else:
                pts.append([today, g["price"]["final"]])
            del pts[:-365]
    for k in [k for k, v in games["apps"].items()
              if (now.date() - datetime.fromisoformat(v["seen"]).date()).days > 90]:
        games["apps"].pop(k)
        hist.pop(k, None)
    for it in new.get("popular", []):               # 名稱來自 Steam 商店詳情
        g = games["apps"].get(str(it["appid"]))
        if g and g.get("name"):
            it["title"] = g["name"]
    new["popular"] = [it for it in new.get("popular", []) if it.get("title")]
    for r, it in enumerate(new["popular"], 1):
        it["rank"] = r
    free_prices(new, games)
    new["lows"] = build_lows(new, games)
    GAMES.write_text(json.dumps(games, ensure_ascii=False), "utf-8")
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False), "utf-8")
    return fails


def main():
    old = load(OUT, {})   # 檔案不存在、是空的或內容壞掉時,都當作沒有舊資料
    new = {"free": [], "deals": [], "news": [], "new": [], "soon": [], "mobile": [], "pool": [], "popular": []}
    errors = []
    for src, section, fn in SOURCES:
        try:
            items = fn()
            if not items and src not in ALLOW_EMPTY:
                raise ValueError("沒有解析到任何項目")
        except Exception as ex:
            errors.append(f"{src}: {type(ex).__name__}: {ex}")
            items = [i for i in old.get(section, []) if i.get("src") == src]  # 保留舊資料
        for i in items:
            i["src"] = src
        new[section].extend(items)
    for sec in ("new", "soon", "free"):
        new[sec] = merge_dupes(new[sec])
    try:
        fails = enrich(new)
        if fails:
            errors.append(f"details: {fails} 筆遊戲詳情抓取失敗" + (f"(例:{_DETAIL_ERR[0]})" if _DETAIL_ERR else ""))
        if _CS_ERR:
            errors.append(f"cheapshark_details: {_CS_ERR[0]}(共 {len(_CS_ERR)} 筆)")
    except Exception as ex:
        errors.append(f"details: {type(ex).__name__}: {ex}")
    new.pop("pool", None)          # 候選池只用來找史低,不存進 data.json
    new["free"] = dedupe_by_appid(new["free"])
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new["errors"] = errors
    OUT.write_text(json.dumps(new, ensure_ascii=False, indent=1), "utf-8")
    print(f"new={len(new['new'])} soon={len(new['soon'])}", end=" ")
    print(f"free={len(new['free'])} deals={len(new['deals'])} news={len(new['news'])} errors={len(errors)}")
    for e in errors:
        print("  !", e)


if __name__ == "__main__":
    main()
