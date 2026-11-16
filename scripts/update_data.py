#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Собирает базу мест и событий Москвы для «Генератора вечера».

Источники:
  * OpenStreetMap (через Overpass API) — места, адреса, часы работы, метро.
  * KudaGo (public-api) — отобранные редакцией места и афиша событий.

Результат: data/places.json. Скрипту не нужны ни ключи, ни сторонние библиотеки.
Если какой-то источник не ответил, остаются прежние данные — сайт не ломается.
"""
import datetime
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "places.json")
UA = "evening-generator/1.0 (personal non-commercial project; https://github.com/Den3d-droid/vecher)"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
KUDAGO = "https://kudago.com/public-api/v1.4"

EVENT_DAYS = 10          # на сколько дней вперёд брать афишу
MAX_EVENTS = 500
# сколько лучших мест оставлять в каждой категории
LIMITS = {"restaurant": 1500, "cafe": 1200, "bar": 700}
DEFAULT_LIMIT = 600


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # ::warning:: подсвечивается в журнале GitHub Actions
    print("::warning::" + msg, flush=True)


# ---------------------------------------------------------------- сеть
def http_json(url, post=None, timeout=240, tries=3):
    last = None
    for attempt in range(tries):
        try:
            data = urllib.parse.urlencode(post).encode() if post else None
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                raise
        except Exception as e:  # таймауты, обрывы, кривой JSON
            last = e
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("не удалось получить %s: %s" % (url.split("?")[0], last))


def overpass(query):
    last = None
    for url in OVERPASS_URLS:
        try:
            res = http_json(url, post={"data": query}, tries=2)
            if isinstance(res, dict) and "elements" in res:
                return res["elements"]
            last = "нет поля elements"
        except Exception as e:
            last = e
            log("  сервер %s не ответил (%s), пробую следующий" % (url.split("/")[2], e))
    raise RuntimeError("Overpass недоступен: %s" % last)


# ---------------------------------------------------------------- геометрия и имена
def dist_km(la1, lo1, la2, lo2):
    p = math.pi / 180
    x = (lo2 - lo1) * p * math.cos((la1 + la2) / 2 * p)
    y = (la2 - la1) * p
    return 6371 * math.sqrt(x * x + y * y)


TYPE_WORDS = ("ресторан", "кафе", "бар", "паб", "кофейня", "антикафе", "клуб", "кинотеатр", "гастробар",
              "кафе-бар", "ресторан-бар", "пиццерия", "центр", "пространство", "арт-пространство", "парк",
              "государственный", "московский")


def norm_name(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`“”„.,!?:;()\[\]/\\|–—-]+", " ", s)
    words = [w for w in s.split() if w not in TYPE_WORDS]
    return " ".join(words)


def same_name(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 5 and short in long_


def cap(s):
    s = (s or "").strip()
    return s[:1].upper() + s[1:] if s else s


def clean_text(s, limit=170):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def good_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        if re.match(r"^[\w.-]+\.[a-zа-я]{2,}(/|$)", u, re.I):
            u = "https://" + u
        else:
            return ""
    return u[:200]


# ---------------------------------------------------------------- OpenStreetMap
Q_AREA = 'area["ISO3166-2"="RU-MOW"]["admin_level"="4"]->.a;'

Q_FOOD = """[out:json][timeout:240];
%s
(
  nwr(area.a)["amenity"~"^(restaurant|cafe|bar|pub|food_court)$"]["name"];
);
out center tags;""" % Q_AREA

Q_FUN = """[out:json][timeout:240];
%s
(
  nwr(area.a)["amenity"~"^(cinema|theatre|arts_centre|planetarium|karaoke_box|nightclub|public_bath|music_venue)$"]["name"];
  nwr(area.a)["tourism"~"^(museum|gallery|zoo|aquarium|viewpoint|attraction|theme_park)$"]["name"];
  nwr(area.a)["leisure"~"^(ice_rink|bowling_alley|escape_game|amusement_arcade|trampoline_park|water_park|sauna|park|miniature_golf)$"]["name"];
  nwr(area.a)["leisure"="sports_centre"]["sport"~"climbing"]["name"];
  nwr(area.a)["shop"="books"]["name"];
);
out center tags;""" % Q_AREA

Q_METRO = """[out:json][timeout:120];
%s
(
  node(area.a)["railway"="station"]["station"~"^(subway|light_rail|monorail)$"]["name"];
);
out tags;""" % Q_AREA

AMENITY = {"restaurant": "restaurant", "cafe": "cafe", "bar": "bar", "pub": "bar", "food_court": "market",
           "cinema": "cinema", "theatre": "theatre", "arts_centre": "arts", "planetarium": "planetarium",
           "karaoke_box": "karaoke", "nightclub": "club", "public_bath": "banya", "music_venue": "concert"}
TOURISM = {"museum": "museum", "gallery": "gallery", "zoo": "zoo", "aquarium": "zoo", "viewpoint": "viewpoint",
           "attraction": "attraction", "theme_park": "fun"}
LEISURE = {"ice_rink": "rink", "bowling_alley": "bowling", "escape_game": "quest", "amusement_arcade": "arcade",
           "trampoline_park": "trampoline", "water_park": "waterpark", "sauna": "banya", "park": "park",
           "miniature_golf": "fun"}

CUISINE = {
    "italian": "итальянская", "pizza": "пицца", "georgian": "грузинская", "japanese": "японская", "sushi": "суши",
    "russian": "русская", "chinese": "китайская", "asian": "азиатская", "burger": "бургеры", "coffee_shop": "кофейня",
    "uzbek": "узбекская", "vietnamese": "вьетнамская", "indian": "индийская", "french": "французская",
    "korean": "корейская", "thai": "тайская", "mexican": "мексиканская", "armenian": "армянская",
    "american": "американская", "seafood": "морепродукты", "fish": "рыба", "steak_house": "стейки",
    "steak": "стейки", "european": "европейская", "international": "европейская", "kebab": "шашлык и кебаб",
    "noodle": "лапша", "ramen": "рамен", "bbq": "барбекю", "barbecue": "барбекю", "vegetarian": "вегетарианская",
    "vegan": "веганская", "mediterranean": "средиземноморская", "caucasian": "кавказская", "turkish": "турецкая",
    "greek": "греческая", "spanish": "испанская", "azerbaijani": "азербайджанская", "lebanese": "ливанская",
    "israeli": "израильская", "breakfast": "завтраки", "dessert": "десерты", "cake": "десерты",
    "ice_cream": "мороженое", "bubble_tea": "бабл-ти", "tea": "чайная", "pancake": "блины", "crepe": "блины",
    "dumplings": "пельмени и хинкали", "ukrainian": "украинская", "serbian": "сербская", "tatar": "татарская",
    "german": "немецкая", "czech": "чешская", "belgian": "бельгийская", "irish": "ирландская", "wine": "вино",
    "craft_beer": "крафтовое пиво", "beer": "пиво", "cocktails": "коктейли", "hookah": "кальян",
    "pasta": "паста", "chicken": "курица", "sandwich": "сэндвичи", "bakery": "выпечка", "pastry": "выпечка",
    "donut": "пончики", "bagel": "бейглы", "poke": "поке", "wok": "вок", "shawarma": "шаурма",
    "middle_eastern": "ближневосточная", "arab": "арабская", "persian": "персидская", "peruvian": "перуанская",
    "argentinian": "аргентинская", "brazilian": "бразильская", "balkan": "балканская", "jewish": "еврейская",
    "kosher": "кошерная", "buryat": "бурятская", "kazakh": "казахская", "tex-mex": "текс-мекс",
}

# сетевые заведения — не плохие, но для «особенного вечера» понижаем
CHAINS = ("шоколадница", "кофе хауз", "starbucks", "stars coffee", "cofix", "one price coffee", "вкусно и точка",
          "kfc", "rostic", "burger king", "бургер кинг", "теремок", "крошка картошка", "subway", "додо",
          "му-му", "муму", "грабли", "якитория", "тануки", "il патио", "il patio", "coffee like", "кофе лайк",
          "макдоналдс", "mcdonald", "папа джонс", "papa john", "domino", "сабвэй", "prime", "прайм",
          "братья караваевы", "хлеб насущный", "cinnabon", "синнабон", "dunkin", "чайхона №1", "шикари",
          "планета суши", "ёбидоёби", "суши wok", "суши вок", "правда кофе", "surf coffee", "даблби",
          "double b", "кофемания", "буханка", "волконский", "кулинарная лавка", "жан-жак", "елки-палки",
          "ёлки-палки", "столовая", "вареничная №1", "chaihona")


def osm_kind(t):
    k = AMENITY.get(t.get("amenity")) or TOURISM.get(t.get("tourism")) or LEISURE.get(t.get("leisure"))
    if not k and t.get("leisure") == "sports_centre" and "climbing" in t.get("sport", ""):
        k = "climbing"
    if not k and t.get("shop") == "books":
        k = "books"
    return k


def osm_score(t, kind):
    s = 0.0
    if t.get("website") or t.get("contact:website"):
        s += 3
    if t.get("opening_hours"):
        s += 2
    if t.get("phone") or t.get("contact:phone"):
        s += 1
    if t.get("cuisine"):
        s += 1
    if t.get("wikidata"):
        s += 4
    if t.get("wikipedia"):
        s += 3
    if t.get("addr:street") and t.get("addr:housenumber"):
        s += 1
    for key in ("outdoor_seating", "wheelchair", "internet_access", "email", "contact:email", "contact:instagram",
                "contact:vk", "contact:telegram", "description", "image", "wikimedia_commons", "payment:cards",
                "start_date", "heritage", "name:en", "level"):
        if t.get(key):
            s += 0.5
    name = (t.get("name") or "").lower().replace("ё", "е")
    if kind in ("restaurant", "cafe", "bar", "market"):
        if any(c.replace("ё", "е") in name for c in CHAINS):
            s -= 6
        elif t.get("brand") or t.get("brand:wikidata"):
            s -= 3
    return s


def osm_place(el):
    t = el.get("tags") or {}
    kind = osm_kind(t)
    if not kind:
        return None
    name = (t.get("name:ru") or t.get("name") or "").strip()
    if len(name) < 2:
        return None
    if t.get("access") in ("private", "no") or t.get("opening_hours") in ("closed", "off"):
        return None
    la = el.get("lat") or (el.get("center") or {}).get("lat")
    lo = el.get("lon") or (el.get("center") or {}).get("lon")
    if la is None or lo is None:
        return None
    notable = bool(t.get("wikidata") or t.get("wikipedia"))
    has_site = bool(t.get("website") or t.get("contact:website"))
    # парков и «достопримечательностей» в OSM тысячи — берём только заметные
    if kind == "attraction" and not notable:
        return None
    if kind == "park" and not (notable or has_site):
        return None
    if kind == "books" and not (has_site or t.get("opening_hours")):
        return None
    p = {"n": name[:90], "k": kind, "la": round(la, 5), "lo": round(lo, 5), "s": osm_score(t, kind)}
    street, house = t.get("addr:street"), t.get("addr:housenumber")
    if street:
        p["a"] = (street + (", " + house if house else ""))[:90]
    if t.get("opening_hours"):
        p["h"] = t["opening_hours"][:80]
    site = good_url(t.get("website") or t.get("contact:website"))
    if site:
        p["w"] = site
    if t.get("cuisine"):
        cs = []
        for c in re.split(r"[;,]", t["cuisine"]):
            ru = CUISINE.get(c.strip().lower())
            if ru and ru not in cs:
                cs.append(ru)
        if cs:
            p["c"] = ", ".join(cs[:3])
    if t.get("fee") == "no":
        p["f"] = 1
    return p


def fetch_osm():
    log("OpenStreetMap: еда и бары…")
    els = overpass(Q_FOOD)
    log("  получено объектов: %d" % len(els))
    time.sleep(10)
    log("OpenStreetMap: культура и развлечения…")
    els2 = overpass(Q_FUN)
    log("  получено объектов: %d" % len(els2))
    places, seen = [], {}
    for el in els + els2:
        p = osm_place(el)
        if not p:
            continue
        key = (norm_name(p["n"]), round(p["la"], 3), round(p["lo"], 3))
        if key in seen:
            if seen[key]["s"] < p["s"]:
                seen[key].update(p)
            continue
        seen[key] = p
        places.append(p)
    time.sleep(10)
    log("OpenStreetMap: станции метро…")
    metro, names = [], set()
    try:
        for el in overpass(Q_METRO):
            name = ((el.get("tags") or {}).get("name") or "").strip()
            if name and name not in names and el.get("lat"):
                names.add(name)
                metro.append([name, round(el["lat"], 5), round(el["lon"], 5)])
    except Exception as e:
        warn("не удалось получить станции метро: %s" % e)
    metro.sort(key=lambda m: m[0])
    log("  мест после отбора: %d, станций: %d" % (len(places), len(metro)))
    return places, metro


# ---------------------------------------------------------------- KudaGo
KG_PLACE = {
    "restaurants": "restaurant", "restaurant": "restaurant", "bar": "bar", "bars": "bar", "pub": "bar",
    "brewery": "bar", "cafe": "cafe", "coffee": "cafe", "coffee-shops": "cafe", "anticafe": "anticafe",
    "museums": "museum", "museum": "museum", "theatre": "theatre", "theater": "theatre", "cinema": "cinema",
    "park": "park", "parks": "park", "clubs": "club", "club": "club", "art-space": "gallery",
    "art-centers": "arts", "culture": "arts", "questroom": "quest", "quests": "quest", "amusement": "fun",
    "attractions": "attraction", "sights": "attraction", "palace": "attraction", "homesteads": "attraction",
    "bridge": "attraction", "fountain": "attraction", "concert-hall": "concert", "comedy-club": "comedy",
    "photo-places": "viewpoint", "observatory": "planetarium", "planetarium": "planetarium",
    "water-park": "waterpark", "rink": "rink", "bowling": "bowling", "karaoke": "karaoke", "zoo": "zoo",
    "bath": "banya", "banya": "banya", "sauna": "banya", "climbing-walls": "climbing", "trampoline": "trampoline",
    "bookstore": "books", "books": "books", "market": "market", "food-court": "market", "gastro-market": "market",
    "kids": "fun", "recreation": "fun", "entertainment": "fun",
}
# порядок важен: более конкретная категория выигрывает у общей
KG_PRIORITY = ["planetarium", "rink", "bowling", "karaoke", "quest", "climbing", "trampoline", "waterpark", "banya",
               "anticafe", "comedy", "zoo", "museum", "gallery", "theatre", "cinema", "concert", "books", "market",
               "restaurant", "bar", "cafe", "club", "arts", "viewpoint", "park", "attraction", "fun"]
EVENT_SKIP = {"business-events", "shopping", "sale", "stock", "discount", "social-activity", "other", "presentation",
              "speed-dating", "yarmarki-razvlecheniya-yarmarki", "recreation-other", "global"}


def kg_pages(path, params, max_pages):
    url = "%s/%s/?%s" % (KUDAGO, path, urllib.parse.urlencode(params))
    out, pages = [], 0
    while url and pages < max_pages:
        res = http_json(url.replace("http://", "https://"), timeout=60)
        out.extend(res.get("results") or [])
        url = res.get("next")
        pages += 1
        time.sleep(0.4)
    return out


def kg_coords(obj):
    c = (obj or {}).get("coords") or {}
    la, lo = c.get("lat"), c.get("lon")
    try:
        la, lo = float(la), float(lo)
    except (TypeError, ValueError):
        return None, None
    # отсекаем нули и точки далеко от Москвы
    if not (54.5 < la < 57.0 and 35.5 < lo < 40.0):
        return None, None
    return round(la, 5), round(lo, 5)


def fetch_kudago_places():
    log("KudaGo: места…")
    base = {"location": "msk", "page_size": 100, "lang": "ru"}
    full = "id,title,short_title,address,coords,subway,favorites_count,comments_count,categories,site_url,description,is_closed,is_stub,timetable"
    try:
        raw = kg_pages("places", dict(base, fields=full), 80)
    except urllib.error.HTTPError:
        raw = kg_pages("places", dict(base, fields="id,title,address,coords,categories,site_url,is_closed"), 80)
    unknown, out = {}, []
    for r in raw:
        if r.get("is_closed") or r.get("is_stub"):
            continue
        kinds = set()
        for c in r.get("categories") or []:
            slug = c.get("slug") if isinstance(c, dict) else c
            if slug in KG_PLACE:
                kinds.add(KG_PLACE[slug])
            elif slug:
                unknown[slug] = unknown.get(slug, 0) + 1
        kind = next((k for k in KG_PRIORITY if k in kinds), None)
        la, lo = kg_coords(r)
        title = cap(r.get("short_title") or r.get("title") or "")
        if not kind or la is None or not title:
            continue
        fav = r.get("favorites_count") or 0
        p = {"n": title[:90], "k": kind, "la": la, "lo": lo, "kg": good_url(r.get("site_url")),
             "s": 6 + min(6.0, math.log2(1 + max(0, fav))), "_full": cap(r.get("title") or "")}
        if r.get("address"):
            p["a"] = clean_text(r["address"], 90)
        if r.get("timetable"):
            p["h"] = clean_text(r["timetable"], 80)
        d = clean_text(r.get("description"))
        if d:
            p["d"] = d
        out.append(p)
    if unknown:
        top = sorted(unknown.items(), key=lambda x: -x[1])[:25]
        log("  нераспознанные категории KudaGo (можно добавить в KG_PLACE): %s" % ", ".join("%s×%d" % x for x in top))
    log("  мест: %d (из %d)" % (len(out), len(raw)))
    return out


def fetch_kudago_events(now):
    log("KudaGo: события на %d дней…" % EVENT_DAYS)
    until = now + EVENT_DAYS * 86400
    params = {"location": "msk", "page_size": 100, "lang": "ru", "actual_since": now, "actual_until": until,
              "expand": "place",
              "fields": "id,title,short_title,dates,place,price,is_free,site_url,categories,age_restriction,favorites_count"}
    raw = kg_pages("events", params, 60)
    out, seen = [], set()
    for r in raw:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        cats = [c.get("slug") if isinstance(c, dict) else c for c in (r.get("categories") or [])]
        cats = [c for c in cats if c]
        if not cats or any(c in EVENT_SKIP for c in cats):
            continue
        dates = []
        for d in r.get("dates") or []:
            try:
                s, e = int(d.get("start")), int(d.get("end") or d.get("start"))
            except (TypeError, ValueError):
                continue
            if e < now or s > until:
                continue
            dates.append([max(s, now - 86400 * 400), min(e, until + 86400 * 400)])
        if not dates:
            continue
        dates.sort()
        title = cap(r.get("short_title") or r.get("title") or "")
        url = good_url(r.get("site_url"))
        if not title or not url:
            continue
        ev = {"t": title[:110], "k": cats[:3], "d": dates[:8], "u": url, "fav": r.get("favorites_count") or 0}
        place = r.get("place") if isinstance(r.get("place"), dict) else None
        if place:
            la, lo = kg_coords(place)
            if la is not None:
                ev["la"], ev["lo"] = la, lo
            if place.get("title"):
                ev["p"] = cap(place["title"])[:80]
            if place.get("address"):
                ev["a"] = clean_text(place["address"], 80)
        if r.get("is_free"):
            ev["fr"] = 1
        if r.get("price"):
            ev["pr"] = clean_text(r["price"], 60)
        age = re.sub(r"\D", "", str(r.get("age_restriction") or ""))
        if age:
            ev["ag"] = int(age)
        out.append(ev)
    out.sort(key=lambda e: -e["fav"])
    out = out[:MAX_EVENTS]
    log("  событий: %d (из %d)" % (len(out), len(raw)))
    return out


# ---------------------------------------------------------------- сборка
def merge(osm, kg):
    """Склеивает одно и то же место из двух источников: совпало имя и точки ближе 300 м."""
    grid = {}
    for p in osm:
        grid.setdefault((int(p["la"] * 200), int(p["lo"] * 200)), []).append(p)
    merged, added = 0, 0
    for k in kg:
        full = k.pop("_full", "")
        names = {norm_name(k["n"]), norm_name(full)} - {""}
        gx, gy = int(k["la"] * 200), int(k["lo"] * 200)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for p in grid.get((gx + dx, gy + dy), []):
                    if dist_km(k["la"], k["lo"], p["la"], p["lo"]) > 0.3:
                        continue
                    pn = norm_name(p["n"])
                    if any(same_name(pn, n) for n in names) and (best is None or p["s"] > best["s"]):
                        best = p
        if best:
            best["s"] += k["s"]
            for f in ("kg", "d"):
                if k.get(f):
                    best[f] = k[f]
            for f in ("a", "h"):
                if k.get(f) and not best.get(f):
                    best[f] = k[f]
            merged += 1
        else:
            osm.append(k)
            grid.setdefault((gx, gy), []).append(k)
            added += 1
    log("Склейка: совпало %d, добавлено из KudaGo %d" % (merged, added))
    return osm


def finalize(places, metro):
    by_kind = {}
    for p in places:
        by_kind.setdefault(p["k"], []).append(p)
    out = []
    for kind, lst in by_kind.items():
        lst.sort(key=lambda p: -p["s"])
        out.extend(lst[:LIMITS.get(kind, DEFAULT_LIMIT)])
    for p in out:
        p["s"] = round(p["s"], 1)
        if not p.get("kg"):
            p.pop("kg", None)
        if metro:
            m = min(metro, key=lambda m: dist_km(p["la"], p["lo"], m[1], m[2]))
            d = dist_km(p["la"], p["lo"], m[1], m[2])
            if d <= 2.5:
                p["m"] = m[0]
                p["md"] = round(d, 1)
    out.sort(key=lambda p: (p["k"], -p["s"]))
    return out


def load_old():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    now = int(time.time())
    old = load_old()
    old_real = bool(old.get("places")) and not old.get("demo")
    notes = []

    places, metro, osm_ok = [], [], False
    try:
        places, metro = fetch_osm()
        osm_ok = len(places) >= 500
        if not osm_ok:
            warn("OpenStreetMap вернул подозрительно мало мест (%d) — считаю это сбоем" % len(places))
    except Exception as e:
        warn("OpenStreetMap недоступен: %s" % e)

    kg_places, kg_ok = [], False
    try:
        kg_places = fetch_kudago_places()
        kg_ok = len(kg_places) > 0
    except Exception as e:
        warn("KudaGo (места) недоступен: %s" % e)

    events, ev_ok = [], False
    try:
        events = fetch_kudago_events(now)
        ev_ok = True
    except Exception as e:
        warn("KudaGo (события) недоступен: %s" % e)
    if not ev_ok:
        # оставляем прежние события, которые ещё не прошли
        events = [e for e in (old.get("events") or []) if any(d[1] >= now for d in e.get("d", []))]

    if osm_ok:
        final = finalize(merge(places, kg_places), metro)
        if not metro:
            metro = old.get("metro") or []
        if not kg_ok:
            notes.append("без подборок KudaGo")
        demo = False
    elif old_real:
        warn("Оставляю прежнюю базу мест, обновляю только события")
        final, metro, demo = old["places"], old.get("metro") or [], False
    elif kg_ok:
        final, demo = finalize(kg_places_clean(kg_places), old.get("metro") or []), False
        metro = old.get("metro") or []
        notes.append("только KudaGo, без OpenStreetMap")
    else:
        warn("Ни один источник не ответил — на сайте остаётся демо-набор. Запустите обновление ещё раз позже.")
        final, metro, demo = old.get("places") or [], old.get("metro") or [], bool(old.get("demo", True))

    counts = {}
    for p in final:
        counts[p["k"]] = counts.get(p["k"], 0) + 1
    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "demo": demo, "notes": notes, "counts": counts,
        "metro": metro, "places": final, "events": events,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(OUT) / 1024
    log("Готово: мест %d, событий %d, станций %d, файл %.0f КБ" % (len(final), len(events), len(metro), size))
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        log("  %-12s %d" % (k, v))
    return 0


def kg_places_clean(kg):
    for k in kg:
        k.pop("_full", None)
    return kg


if __name__ == "__main__":
    sys.exit(main())
