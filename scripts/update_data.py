#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Собирает базу мест и событий Москвы для «Генератора вечера».

Источники:
  * OpenStreetMap (через Overpass API) — места, адреса, часы работы, метро.
  * Афиша mos.ru — события на ближайшие дни с ценами, площадками и метро.

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
CURATED = os.path.join(ROOT, "data", "curated.txt")
GEOCACHE = os.path.join(ROOT, "data", "geocache.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
MAX_GEOCODE = 450        # сколько новых адресов искать за один запуск (1 запрос в секунду)
UA = "evening-generator/1.0 (personal non-commercial project; https://github.com/Den3d-droid/vecher)"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
MOS_AFISHA = "https://www.mos.ru/api/newsfeed/v4/frontend/json/ru/afisha"

EVENT_DAYS = 10          # на сколько дней вперёд брать афишу
MAX_EVENTS = 1500
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
        time.sleep(8 * (attempt + 1))
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
out center meta;""" % Q_AREA

Q_FUN = """[out:json][timeout:240];
%s
(
  nwr(area.a)["amenity"~"^(cinema|theatre|arts_centre|planetarium|karaoke_box|nightclub|public_bath|music_venue)$"]["name"];
  nwr(area.a)["tourism"~"^(museum|gallery|zoo|aquarium|viewpoint|attraction|theme_park)$"]["name"];
  nwr(area.a)["leisure"~"^(ice_rink|bowling_alley|escape_game|amusement_arcade|trampoline_park|water_park|sauna|park|miniature_golf)$"]["name"];
  nwr(area.a)["leisure"="sports_centre"]["sport"~"climbing"]["name"];
  nwr(area.a)["shop"="books"]["name"];
);
out center meta;""" % Q_AREA

Q_METRO = """[out:json][timeout:120];
%s
(
  node(area.a)["railway"="station"]["station"~"^(subway|light_rail|monorail)$"]["name"];
);
out body;""" % Q_AREA

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
          "ёлки-палки", "столовая", "вареничная №1", "chaihona", "мир шашлыков", "шашлычный мир", "тануки", "бургер кинг", "чайхона №1", "андерсон", "anderson", "кофикс", "хинкальная №1" , "pizza hut", "il forno", "торро гриль", "корчма тарас бульба", "урюк", "кофе хауз", "costa coffee", "paul", "вкусвилл", "restoran-bar", "грабли")


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
    if kind in ("restaurant", "cafe", "bar", "market") and (t.get("brand") or t.get("brand:wikidata")):
        s -= 6      # сеть, которой нет в списке CHAINS
    return s


# заведения, которые часто закрываются: для них свежесть данных важнее всего
VOLATILE = {"restaurant", "cafe", "bar", "club", "quest", "karaoke", "anticafe", "arcade", "trampoline", "bowling",
            "market", "banya", "climbing", "fun", "books"}
# примерный уровень трат по типу места: 0 бесплатно, 1 недорого, 2 средне, 3 дорого
PRICE = {"park": 0, "viewpoint": 0, "attraction": 0, "books": 0, "gallery": 1, "museum": 1, "arts": 1, "cinema": 1,
         "rink": 1, "anticafe": 1, "arcade": 1, "zoo": 1, "planetarium": 1, "market": 1, "cafe": 1, "bowling": 2,
         "fun": 2, "bar": 2, "restaurant": 2, "club": 2, "theatre": 2, "concert": 2, "climbing": 2, "trampoline": 2,
         "karaoke": 3, "quest": 3, "waterpark": 3, "banya": 3}
CHEAP_EATS = {"бургеры", "пицца", "шаурма", "пельмени и хинкали", "лапша", "вок", "блины", "сэндвичи", "курица",
              "шашлык и кебаб", "выпечка", "пончики", "вьетнамская", "узбекская", "поке", "рамен"}
PRICEY_EATS = {"стейки", "морепродукты", "французская", "рыба", "перуанская", "вино"}
CLOSED_RE = re.compile(r"закрыт|не работает|ликвидир|closed|снесен|снесён", re.I)


def last_seen(el, t):
    """Дата, когда место последний раз правили или проверяли в OpenStreetMap (ГГГГ-ММ-ДД)."""
    dates = [str(el.get("timestamp") or "")[:10]]
    for k, v in t.items():
        if k.startswith("check_date") or k == "survey:date":
            dates.append(str(v)[:10])
    dates = [d for d in dates if re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    return max(dates) if dates else ""


def osm_place(el, today):
    t = el.get("tags") or {}
    kind = osm_kind(t)
    if not kind:
        return None
    name = (t.get("name:ru") or t.get("name") or "").strip()
    if len(name) < 2:
        return None
    if t.get("access") in ("private", "no") or t.get("opening_hours") in ("closed", "off"):
        return None
    # крупные сети помечаем: если место не попало в подборки, его уберём
    chain = False
    if kind in ("restaurant", "cafe", "bar"):
        flat = re.sub(r"\s+", " ", name.lower().replace("ё", "е").replace("\u00a0", " ")).replace("№ ", "№")
        chain = any(c.replace("ё", "е") in flat for c in CHAINS)
    # явные признаки закрытия
    if any(k.split(":")[0] in ("disused", "abandoned", "demolished", "removed", "was", "razed", "construction") for k in t):
        return None
    if CLOSED_RE.search(name) or CLOSED_RE.search(t.get("note", "") + " " + t.get("fixme", "") + " " + t.get("description", "")):
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

    score = osm_score(t, kind)
    seen = last_seen(el, t)
    age = 99.0
    if seen:
        try:
            age = (today - datetime.date.fromisoformat(seen)).days / 365.0
        except ValueError:
            pass
    if kind in VOLATILE:
        # давно не проверявшиеся заведения выбрасываем: слишком часто они уже закрыты
        if age >= 4 and not notable:
            return None
        score += 5 if age < 1 else 3 if age < 2 else 0 if age < 3 else -4
    else:
        score += 2 if age < 2 else 0
    try:
        score += min(2.0, int(el.get("version") or 1) / 5.0)   # много правок = место живое и заметное
    except (TypeError, ValueError):
        pass

    p = {"n": name[:90], "k": kind, "la": round(la, 5), "lo": round(lo, 5), "s": score}
    if chain:
        p["_ch"] = 1
    if seen:
        p["y"] = int(seen[:4])
    street, house = t.get("addr:street"), t.get("addr:housenumber")
    if street:
        p["a"] = (street + (", " + house if house else ""))[:90]
    if t.get("opening_hours"):
        p["h"] = t["opening_hours"][:80]
    site = good_url(t.get("website") or t.get("contact:website"))
    if site:
        p["w"] = site
    cs = []
    if t.get("cuisine"):
        for c in re.split(r"[;,]", t["cuisine"]):
            ru = CUISINE.get(c.strip().lower())
            if ru and ru not in cs:
                cs.append(ru)
        if cs:
            p["c"] = ", ".join(cs[:3])
    if t.get("fee") == "no":
        p["f"] = 1
    # уровень трат
    pc = PRICE.get(kind, 2)
    low_name = name.lower()
    if kind == "restaurant":
        if any(c in CHEAP_EATS for c in cs) or re.search(r"столовая|чайхана|хинкальная|пельменная|чебуречная|шашлычная|бургер|пицц", low_name):
            pc = 1
        if any(c in PRICEY_EATS for c in cs) or t.get("reservation") in ("required", "recommended") or t.get("stars"):
            pc = 3
    if kind == "bar" and re.search(r"рюмочная|пивная|разливн|паб|pub|beer|крафт", low_name):
        pc = 1
    if kind in ("museum", "gallery") and t.get("fee") == "no":
        pc = 0
    p["pc"] = pc
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
    today = datetime.date.today()
    ages = {}
    for el in els + els2:
        y = str(el.get("timestamp") or "?")[:4]
        ages[y] = ages.get(y, 0) + 1
        p = osm_place(el, today)
        if not p:
            continue
        key = (norm_name(p["n"]), round(p["la"], 3), round(p["lo"], 3))
        if key in seen:
            if seen[key]["s"] < p["s"]:
                seen[key].update(p)
            continue
        seen[key] = p
        places.append(p)
    log("  последняя правка по годам: %s" % ", ".join("%s: %d" % x for x in sorted(ages.items())))
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


# ---------------------------------------------------------------- афиша mos.ru
MSK = datetime.timezone(datetime.timedelta(hours=3))
# сфера на mos.ru (по подстроке) -> наша категория события
SPHERES = [("выстав", "exhibition"), ("спектакл", "theater"), ("театр", "theater"), ("концерт", "concert"),
           ("музык", "concert"), ("экскурс", "tour"), ("мастер-класс", "workshop"), ("лекци", "lecture"),
           ("образован", "lecture"), ("встреч", "lecture"), ("квест", "quest"), ("игр", "quest"),
           ("кино", "cinema"), ("фестивал", "festival"), ("праздник", "festival"), ("ярмар", "festival"),
           ("спорт", "sport"), ("каток", "sport"), ("катк", "sport"), ("для детей", "kids"), ("детям", "kids"),
           ("зоопарк", "kids"), ("парк", "park"), ("шоу", "show"), ("цирк", "show"), ("стендап", "show"),
           ("танц", "party"), ("вечерин", "party"), ("представлен", "show"), ("чтени", "lecture"),
           ("досуг", "festival"), ("новый год", "festival"), ("зима в москве", "festival")]
# это не события, а услуги и технические метки
SPHERE_SKIP = ("бронирование", "дополнительные услуги", "аренда", "долголетие", "абонемент", "услуг")


def msk_ts(s):
    try:
        return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).timestamp())
    except (TypeError, ValueError):
        return None


def fetch_mos_events(now):
    log("Афиша mos.ru: события на %d дней…" % EVENT_DAYS)
    until = now + EVENT_DAYS * 86400
    fmt = lambda ts: datetime.datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d %H:%M:%S")
    flt = json.dumps({">=date_to": fmt(now), "<=date_from": fmt(until)})
    raw, page, pages = [], 1, 1
    while page <= pages and page <= 40:
        url = MOS_AFISHA + "?" + urllib.parse.urlencode({"per-page": 100, "page": page, "sort": "date_from",
                                                          "filter": flt, "expand": "spheres,spots"})
        res = http_json(url, timeout=120)
        raw.extend(res.get("items") or [])
        pages = int((res.get("_meta") or {}).get("pageCount") or 1)
        page += 1
        time.sleep(0.5)
    out, seen, skipped = [], set(), {}
    for r in raw:
        if r.get("id") in seen:
            continue
        seen.add(r.get("id"))
        title = cap(clean_text(r.get("title"), 110))
        spheres = [str(x.get("title") or "").lower() for x in (r.get("spheres") or []) if isinstance(x, dict)]
        if not title or r.get("ebs_type") == "membership" or any(k in sp for sp in spheres for k in SPHERE_SKIP):
            continue
        if re.match(r"^(комплексн|единый билет|входной билет|абонемент)", title.lower()):
            continue
        cats = []
        for sp in spheres:
            cat = next((c for key, c in SPHERES if key in sp), None)
            if cat is None:
                skipped[sp] = skipped.get(sp, 0) + 1
            elif cat not in cats:
                cats.append(cat)
        if not cats:
            cats = ["other"]
        s, e = msk_ts(r.get("date_from")), msk_ts(r.get("date_to"))
        os_, oe = msk_ts(r.get("occurrence_date_from")), msk_ts(r.get("occurrence_date_to"))
        if s is None:
            continue
        e = e or s
        dates = []
        if os_ and now - 3600 <= os_ <= until:
            dates.append([os_, oe if oe and oe > os_ else os_ + 2 * 3600])     # ближайший сеанс
        if e - s <= 36 * 3600:
            if s >= now - 3600 and not dates:
                dates.append([s, e])
        elif "exhibition" in cats or cats == ["park"]:
            dates.append([s, e])                                             # выставка: «идёт до…»
        if not dates:
            continue
        ev = {"t": title, "k": cats[:3], "d": dates, "u": "https://www.mos.ru/afisha/event/%s/" % r.get("id")}
        spot = next((x for x in (r.get("spots") or []) if isinstance(x, dict)), None)
        if spot:
            try:
                la, lo = float(spot.get("lat")), float(spot.get("lon"))
                if 54.5 < la < 57.0 and 35.5 < lo < 40.0:
                    ev["la"], ev["lo"] = round(la, 5), round(lo, 5)
            except (TypeError, ValueError):
                pass
            if spot.get("title"):
                ev["p"] = clean_text(spot["title"], 80)
            if spot.get("address"):
                ev["a"] = clean_text(spot["address"], 80)
            md = spot.get("ebs_metrodata")
            if isinstance(md, list) and md and isinstance(md[0], dict) and md[0].get("name"):
                ev["m"] = str(md[0]["name"])[:40]
        pf, pt = r.get("ebs_price_from"), r.get("ebs_price_to")
        if r.get("free") or (pf == 0 and pt == 0):
            ev["fr"] = 1
        elif isinstance(pf, (int, float)) and pf > 0:
            ev["pf"] = int(pf)
            ev["pr"] = ("%d ₽" % pf) if not pt or pt == pf else ("от %d до %d ₽" % (pf, pt))
        age = (r.get("restriction") or {}).get("age") if isinstance(r.get("restriction"), dict) else None
        if isinstance(age, int) and age > 0:
            ev["ag"] = age
        out.append(ev)
    # сначала события с известным временем сеанса, потом длинные выставки
    out.sort(key=lambda ev: (ev["d"][0][1] - ev["d"][0][0] > 36 * 3600, ev["d"][0][0]))
    out = out[:MAX_EVENTS]
    if skipped:
        top = sorted(skipped.items(), key=lambda x: -x[1])[:20]
        log("  сферы без категории: %s" % ", ".join("%s×%d" % x for x in top))
    kinds = {}
    for ev in out:
        kinds[ev["k"][0]] = kinds.get(ev["k"][0], 0) + 1
    log("  событий: %d (из %d): %s" % (len(out), len(raw), ", ".join("%s %d" % x for x in sorted(kinds.items(), key=lambda x: -x[1]))))
    return out


# ---------------------------------------------------------------- подборки (гиды и премии)
STREET_WORDS = {"улица": "ул", "ул": "ул", "переулок": "пер", "пер": "пер", "проспект": "пр", "пр-т": "пр",
                "бульвар": "бул", "б-р": "бул", "площадь": "пл", "пл": "пл", "набережная": "наб", "наб": "наб",
                "шоссе": "ш", "ш": "ш", "проезд": "пр-д", "тупик": "туп", "аллея": "ал", "вал": "вал"}


def norm_street(s):
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[.,]", " ", s)
    words = [STREET_WORDS.get(w, w) for w in s.split()]
    return " ".join(sorted(words))


def house_key(s):
    m = re.match(r"\s*(\d+(?:[а-я](?!\d))?(?:/\d+(?:[а-я](?!\d))?)?)", (s or "").lower().replace("ё", "е"))
    return m.group(1) if m else ""


def split_addr(addr):
    """«Москва, Трубная улица, 23, корп. 2» -> ("трубная ул" нормализованная, "23")."""
    parts = [x.strip() for x in (addr or "").split(",")]
    parts = [x for x in parts if x and x.lower() not in ("москва", "россия")]
    for i, part in enumerate(parts):
        if re.match(r"^\d", part) and i > 0:
            return norm_street(parts[i - 1]), house_key(part)
    return "", ""


def geo_query(addr):
    a = re.sub(r",\s*корп\.\s*", "к", addr)
    a = re.sub(r",\s*стр\.\s*", "с", a)
    a = re.sub(r",\s*подъезд.*$", "", a)
    return a


def short_addr(addr):
    return re.sub(r"^\s*Москва,\s*", "", addr or "")[:90]


GUESS_BAR = re.compile(r"\bбар\b|bar\b|паб|pub|beer|бир|пив|крафт|craft|рюмочн|вин|wine|cocktail|коктейл|taproom|"
                       r"lounge|сидр|cidr|настоеч|джин|gin\b|prosecco|просекко|prscco|tipsy|brewer|пивовар", re.I)


def load_curated():
    out = []
    try:
        lines = open(CURATED, encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("# "):
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 3 or not parts[0]:
            continue
        name, addr, tags = parts[0], parts[1], parts[2].split()
        labels, tier, pc, kind = [], 2, None, None
        rank = next((int(t[1:]) for t in tags if re.match(r"^W\d+$", t)), None)
        if "U50" in tags:
            labels.append("Ultima Guide 2026: топ-50 ресторанов"); tier = 1
        if "U20" in tags:
            labels.append("Ultima Guide 2026: топ-20 баров"); tier = 1
        if "WM" in tags:
            labels.append("WhereToEat Москва 2026: топ-10"); tier = 1
        if rank:
            labels.append("WhereToEat Россия 2026: №%d" % rank); tier = 1
        if "AC" in tags:
            labels.append("«Афиша Daily»: лучшие кофейни"); tier = 1
        if "UL" in tags and not labels:
            labels.append("Ultima Guide 2026: длинный список")
        for t in tags:
            if re.match(r"^p[1-4]$", t):
                pc = min(3, int(t[1]))
            if t in ("bar", "cafe", "rest"):
                kind = {"bar": "bar", "cafe": "cafe", "rest": "restaurant"}[t]
        if not kind:
            kind = "bar" if GUESS_BAR.search(name) else "restaurant"
        out.append({"n": name, "addr": addr, "labels": labels, "tier": tier, "pc": pc, "k": kind})
    return out


def load_geocache():
    try:
        with open(GEOCACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def geocode(addr, cache, budget):
    if addr in cache:
        return cache[addr]
    if budget[0] <= 0:
        return None
    result = None
    for q in (geo_query(addr), re.sub(r"(\d+[а-яА-Я]?)(?:[кс/]\S*)?$", r"\1", geo_query(addr))):
        budget[0] -= 1
        try:
            url = NOMINATIM + "?" + urllib.parse.urlencode({"format": "jsonv2", "limit": 1, "countrycodes": "ru", "q": q})
            res = http_json(url, timeout=30, tries=2)
        except Exception as e:
            log("  геокодер: %s (%s)" % (q, e))
            res = []
        time.sleep(1.1)   # правила Nominatim: не чаще раза в секунду
        if res:
            la, lo = float(res[0]["lat"]), float(res[0]["lon"])
            if 55.1 < la < 56.1 and 36.8 < lo < 38.0:
                result = [round(la, 5), round(lo, 5)]
                break
    cache[addr] = result
    return result


def apply_curated(places, today):
    cur = load_curated()
    if not cur:
        return places
    cache = load_geocache()
    budget = [MAX_GEOCODE]
    by_name = {}
    for p in places:
        by_name.setdefault(norm_name(p["n"]), []).append(p)
    names = list(by_name)
    matched = added = missed = 0
    for c in cur:
        cn = norm_name(c["n"])
        cands = []
        if cn:
            for n in names:
                if same_name(n, cn):
                    cands.extend(by_name[n])
        street, house = split_addr(c["addr"])
        target = None
        if street and house:
            for p in cands:
                ps, ph = split_addr("Москва, " + p.get("a", ""))
                if ps == street and ph and (ph == house or ph.split("/")[0] == house.split("/")[0]):
                    target = p
                    break
        if target is None and len(cands) == 1 and len(cn) >= 6:
            target = cands[0]
        coords = None
        if target is None:
            coords = geocode(c["addr"], cache, budget) if street else None
            if coords:
                near = [p for p in cands if dist_km(coords[0], coords[1], p["la"], p["lo"]) < 0.25]
                if near:
                    target = min(near, key=lambda p: dist_km(coords[0], coords[1], p["la"], p["lo"]))
        if target is None and coords:
            target = {"n": c["n"], "k": c["k"], "la": coords[0], "lo": coords[1], "s": 0.0, "a": short_addr(c["addr"])}
            places.append(target)
            by_name.setdefault(cn, []).append(target)
            added += 1
        elif target is None:
            missed += 1
            continue
        else:
            matched += 1
        if target["k"] not in ("restaurant", "cafe", "bar", "market"):
            target["k"] = c["k"]
        target["g"] = sorted(set(target.get("g", []) + c["labels"]))
        target["gt"] = min(target.get("gt", 9), c["tier"])
        target["s"] = target.get("s", 0) + (15 if c["tier"] == 1 else 8)
        target["y"] = today.year
        if c["pc"] is not None:
            target["pc"] = c["pc"]
        elif "pc" not in target:
            target["pc"] = 1 if c["k"] == "cafe" else 2
        target.pop("_ch", None)
    try:
        with open(GEOCACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0, sort_keys=True)
    except OSError:
        pass
    log("Подборки: %d записей, найдено на карте %d, добавлено по адресу %d, не найдено %d%s" % (
        len(cur), matched, added, missed, " (часть адресов поищу в следующий раз)" if budget[0] <= 0 else ""))
    return places


# ---------------------------------------------------------------- сборка
def finalize(places, metro):
    places = [p for p in places if not p.pop("_ch", None)]   # сети без отметок в гидах убираем
    by_kind = {}
    for p in places:
        by_kind.setdefault(p["k"], []).append(p)
    out = []
    for kind, lst in by_kind.items():
        lst.sort(key=lambda p: -p["s"])
        out.extend(lst[:LIMITS.get(kind, DEFAULT_LIMIT)])
    for p in out:
        p["s"] = round(p["s"], 1)
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
    old_real = bool(old.get("places")) and not old.get("demo") and any("pc" in p for p in old["places"][:50])
    notes = []
    # места обновляем по понедельникам и четвергам (и при ручном запуске), афишу — каждый день
    full = os.environ.get("FULL", "true").lower() != "false" or datetime.datetime.now(datetime.timezone.utc).weekday() in (0, 3) or not old_real

    places, metro, osm_ok = [], [], False
    if full:
        try:
            places, metro = fetch_osm()
            osm_ok = len(places) >= 500
            if not osm_ok:
                warn("OpenStreetMap вернул подозрительно мало мест (%d) — считаю это сбоем" % len(places))
        except Exception as e:
            warn("OpenStreetMap недоступен: %s" % e)
    else:
        log("Сегодня обновляю только афишу, база мест остаётся прежней")

    events, ev_ok = [], False
    try:
        events = fetch_mos_events(now)
        ev_ok = len(events) > 0
    except Exception as e:
        warn("Афиша mos.ru недоступна: %s" % e)
    if not ev_ok:
        # оставляем прежние события, которые ещё не прошли
        events = [e for e in (old.get("events") or []) if any(d[1] >= now for d in e.get("d", []))]

    if osm_ok:
        places = apply_curated(places, datetime.date.today())
        final = finalize(places, metro)
        if not metro:
            metro = old.get("metro") or []
        demo = False
    elif old.get("places"):
        if full:
            warn("Оставляю прежнюю базу мест, обновляю только события")
        final, metro, demo = old["places"], old.get("metro") or [], bool(old.get("demo"))
    else:
        warn("Источник мест не ответил, а прежней базы нет. Запустите обновление ещё раз позже.")
        final, metro, demo = [], [], True

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


if __name__ == "__main__":
    sys.exit(main())
