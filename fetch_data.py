#!/usr/bin/env python3
"""抓取各平台限免、特價、新聞,輸出 data.json。只用標準函式庫,不需安裝套件。
任何一個來源失敗時,保留該來源上一次的資料,並把錯誤記在 data.json 的 errors。"""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

OUT = Path(__file__).parent / "data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (game-radar; personal project)"}


def get(url, timeout=25):
    time.sleep(0.25)  # 放慢一點,避免被限流
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------- Epic 限免 ----------
def parse_epic(d):
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
                        items.append({"platform": "Epic", "title": e["title"], "status": status,
                                      "start": o.get("startDate"), "end": o.get("endDate"),
                                      "desc": f"原價 {price}" if price else "", "url": url})
    return items


def fetch_epic():
    url = ("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
           "?locale=zh-Hant&country=TW&allowCountries=TW")
    return parse_epic(json.loads(get(url)))


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


def fetch_cheapshark():
    url = ("https://www.cheapshark.com/api/1.0/deals?storeID=1,7,25&onSale=1&pageSize=12"
           "&sortBy=Deal+Rating&metacritic=75")
    return parse_cheapshark(json.loads(get(url)))


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


# (來源代號, 放進哪一區, 抓取函式)
SOURCES = [
    ("epic", "free", fetch_epic),
    ("steam", "deals", fetch_steam),
    ("cheapshark", "deals", fetch_cheapshark),
    ("nintendo", "news", rss("https://www.nintendolife.com/feeds/latest", "Nintendo")),
    ("playstation", "news", rss("https://www.pushsquare.com/feeds/latest", "PlayStation")),
    ("xbox", "news", rss("https://www.purexbox.com/feeds/latest", "Xbox")),
    ("ea", "news", gnews("EA Games OR 美商藝電", "EA")),
    ("steam_news", "news", gnews("Steam 遊戲 新聞", "Steam")),
    ("epic_news", "news", gnews("Epic Games Store", "Epic")),
]


# ---------- 遊戲詳情(畫面、影片、評論)與價格歷史 ----------
GAMES = Path(__file__).parent / "games.json"
HISTORY = Path(__file__).parent / "history.json"
MAX_APPS = 30          # 每次最多處理幾款遊戲,避免請求過多
REFRESH_HOURS = 20     # 詳情超過這個時數才重抓


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def norm(t):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", t.lower())


def find_appid(title):
    """用遊戲名稱找 Steam appid,只接受名稱完全相符的結果,找不到回傳 0。"""
    url = "https://store.steampowered.com/api/storesearch/?cc=tw&l=tchinese&term=" + urllib.parse.quote(title)
    for it in json.loads(get(url)).get("items", []):
        if norm(it.get("name", "")) == norm(title):
            return it["id"]
    return 0


def parse_details(d):
    po = d.get("price_overview") or {}
    movies = []
    for m in (d.get("movies") or [])[:2]:
        mp4 = m.get("mp4") or {}
        movies.append({"name": m.get("name", ""), "thumb": m.get("thumbnail", ""),
                       "mp4": mp4.get("480") or mp4.get("max") or ""})
    return {"name": d.get("name", ""), "header": d.get("header_image", ""), "desc": d.get("short_description", ""),
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
        raise ValueError("appdetails 回傳失敗")
    g = parse_details(entry["data"])
    base = f"https://store.steampowered.com/appreviews/{appid}?json=1&filter=all&review_type=all&purchase_type=all"
    summ = json.loads(get(base + "&language=all&num_per_page=0"))
    txt = json.loads(get(base + "&language=tchinese&num_per_page=3"))
    if not txt.get("reviews"):
        txt = json.loads(get(base + "&language=english&num_per_page=3"))
    g["reviews"] = parse_reviews(summ, txt)
    try:
        g["low"] = fetch_lowest(appid)
    except Exception:
        g["low"] = None
    return g


def enrich(new):
    """替限免/特價項目補上 appid,更新 games.json(詳情)與 history.json(每日價格)。回傳失敗數。"""
    games = load(GAMES, {"apps": {}, "map": {}})
    hist = load(HISTORY, {})
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    fails, ids = 0, []
    for sec in ("free", "deals"):
        for it in new[sec]:
            if not it.get("appid") and sec == "free":
                key = norm(it["title"])
                if key not in games["map"]:
                    try:
                        games["map"][key] = find_appid(it["title"])
                    except Exception:
                        fails += 1
                        continue
                it["appid"] = games["map"][key]
            if it.get("appid") and it["appid"] not in ids:
                ids.append(it["appid"])
    for appid in ids[:MAX_APPS]:
        k = str(appid)
        old = games["apps"].get(k)
        if old and (now - datetime.fromisoformat(old["fetched"])).total_seconds() < REFRESH_HOURS * 3600:
            old["seen"] = today
            continue
        try:
            g = fetch_game(appid)
        except Exception:
            fails += 1
            continue
        g["fetched"], g["seen"] = now.isoformat(timespec="seconds"), today
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
    GAMES.write_text(json.dumps(games, ensure_ascii=False), "utf-8")
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False), "utf-8")
    return fails


def main():
    old = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {}
    new = {"free": [], "deals": [], "news": []}
    errors = []
    for src, section, fn in SOURCES:
        try:
            items = fn()
            if not items:
                raise ValueError("沒有解析到任何項目")
        except Exception as ex:
            errors.append(f"{src}: {type(ex).__name__}: {ex}")
            items = [i for i in old.get(section, []) if i.get("src") == src]  # 保留舊資料
        for i in items:
            i["src"] = src
        new[section].extend(items)
    try:
        fails = enrich(new)
        if fails:
            errors.append(f"details: {fails} 筆遊戲詳情抓取失敗")
    except Exception as ex:
        errors.append(f"details: {type(ex).__name__}: {ex}")
    new["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new["errors"] = errors
    OUT.write_text(json.dumps(new, ensure_ascii=False, indent=1), "utf-8")
    print(f"free={len(new['free'])} deals={len(new['deals'])} news={len(new['news'])} errors={len(errors)}")
    for e in errors:
        print("  !", e)


if __name__ == "__main__":
    main()
