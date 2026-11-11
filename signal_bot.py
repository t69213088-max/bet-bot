"""
Мульти-спорт LIVE-сигналы в Telegram (API-Sports, один ключ на все спорты).

ФУТБОЛ:  ТБ, ТБ 1-го тайма, Обе забьют, Угловые ТБ, ЖК ТБ, ИТБ команды, Удаление
ДРУГИЕ:  баскетбол, NBA, хоккей, гандбол, волейбол, бейсбол, регби, NFL, AFL
         (тоталы по темпу, близкая концовка; волейбол: равные сеты)

Что умеет сверх сигналов:
  * журнал всех сигналов в signals.csv
  * АВТО-ПРОВЕРКА футбольных сигналов после матча: ✅/❌ и общая проходимость по типам
  * состояние хранится в bot_state.json: после перезапуска дублей не будет
  * лог в bot.log, повторы при сбоях сети, защита от лимитов Telegram и квоты API

Установка:  pip install requests
Запуск:     python signal_bot.py
"""
import csv
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone

import requests

# ============ ДОСТУПЫ: ТОЛЬКО через переменные окружения / GitHub Secrets ============
# Никогда не вписывайте ключи прямо в код, если репозиторий публичный.
API_KEY = os.getenv("API_KEY", "")
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# ============ ВИДЫ СПОРТА ============
# on: включён ли, every: период опроса в секундах.
# Каждый спорт = отдельный API со своей квотой (на Free 100 запросов/сутки на каждый).
SPORTS = {
    "football":   {"on": True, "every": 90},
    "basketball": {"on": True, "every": 120},
    "nba":        {"on": True, "every": 120},
    "hockey":     {"on": True, "every": 120},
    "handball":   {"on": True, "every": 120},
    "volleyball": {"on": True, "every": 120},
    "baseball":   {"on": True, "every": 180},
    "rugby":      {"on": True, "every": 180},
    "nfl":        {"on": True, "every": 180},
    "afl":        {"on": True, "every": 180},
}
HOSTS = {
    "football": "v3.football.api-sports.io",
    "basketball": "v1.basketball.api-sports.io",
    "nba": "v2.nba.api-sports.io",
    "hockey": "v1.hockey.api-sports.io",
    "handball": "v1.handball.api-sports.io",
    "volleyball": "v1.volleyball.api-sports.io",
    "baseball": "v1.baseball.api-sports.io",
    "rugby": "v1.rugby.api-sports.io",
    "nfl": "v1.american-football.api-sports.io",
    "afl": "v1.afl.api-sports.io",
}
ICON = {"football": "⚽", "basketball": "🏀", "nba": "🏀", "hockey": "🏒", "handball": "🤾",
        "volleyball": "🏐", "baseball": "⚾", "rugby": "🏉", "nfl": "🏈", "afl": "🏉"}

RESERVE = 3        # столько запросов оставляем в запасе; потом спорт спит до завтра
LEAGUE_IDS = []    # футбол: [39, 140, 135, 78, 61] -> только эти лиги; пусто = все
MAX_MATCHES = 25   # футбол: максимум матчей за цикл (каждый = 1 запрос статистики)
WINDOW_MIN = 10    # окно «давления» в минутах (футбол)

# ============ ФУТБОЛ: ПОРОГИ ============
GOAL = dict(min_from=20, min_to=88, shots=6, on_target=3, max_diff=1)      # ТБ
CORN = dict(min_from=20, min_to=88, corners=3, shots=5)                    # угловые
CARD = dict(min_from=20, min_to=88, fouls=8, max_diff=1)                   # ЖК
FH = dict(min_from=15, min_to=40, shots=5, on_target=2)                    # ТБ 1Т при 0:0
BTTS = dict(min_from=25, min_to=80, opp_sot=3, opp_shots_window=3)         # обе забьют
ITB = dict(min_from=20, min_to=85, shots=5, on_target=2, opp_max=1)        # ИТБ команды
RED = dict(min_from=15, min_to=80)                                         # удаление

EARLY_UNTIL = 45                       # до этой минуты пороги строже
GOAL_EARLY = dict(shots=8, on_target=4)
CORN_EARLY = dict(corners=4, shots=7)
CARD_EARLY = dict(fouls=10)

# ============ ДРУГИЕ СПОРТЫ: ПОРОГИ ============
# periods: число периодов, over/under: пороги прогноза итогового тотала,
# min_period: с какого периода анализировать, close: разница для «близкой концовки»
TOTALS = {
    "basketball": dict(periods=4, over=175, under=145, min_period=2, close=5),
    "nba":        dict(periods=4, over=235, under=200, min_period=2, close=6),
    "hockey":     dict(periods=3, over=6.5, under=4.5, min_period=2, close=1),
    "handball":   dict(periods=2, over=64, under=50, min_period=1, close=3),
    "baseball":   dict(periods=9, over=11, under=6.5, min_period=4, close=1, close_from=8),
    "rugby":      dict(periods=2, over=60, under=35, min_period=1, close=7),
    "nfl":        dict(periods=4, over=55, under=36, min_period=2, close=7),
    "afl":        dict(periods=4, over=190, under=140, min_period=2, close=12),
}

# ============ СЛУЖЕБНОЕ ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
JOURNAL_FILE = os.path.join(BASE_DIR, "signals.csv")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("bot")

HEAD = {"x-apisports-key": API_KEY}
LABEL = {"goal": "ТБ", "fh": "ТБ 1Т", "btts": "ОЗ", "corn": "Угловые", "card": "ЖК", "itb": "ИТБ"}

history = {}     # football: fixture_id -> deque[(minute, stats)]
nostats = {}     # football: fixture_id -> сколько раз статистика пришла пустой
remaining = {}   # host -> сколько запросов осталось (по заголовкам API)
paused = {}      # sport -> дата (UTC), когда ушёл в паузу из-за квоты
sent = {}        # ключ сигнала -> время (антидубли)
pending = []     # футбольные сигналы, ждущие результата
tally = {}       # тип -> [зашло, всего]


# ---------- состояние ----------
def load_state():
    global sent, pending, tally
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        sent, pending, tally = s.get("sent", {}), s.get("pending", []), s.get("tally", {})
        cutoff = time.time() - 2 * 86400
        sent = {k: v for k, v in sent.items() if v > cutoff}
    except FileNotFoundError:
        pass
    except Exception as e:
        log.info("state load error: %s", e)


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sent": sent, "pending": pending, "tally": tally}, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.info("state save error: %s", e)


def journal(meta):
    new = not os.path.exists(JOURNAL_FILE)
    try:
        with open(JOURNAL_FILE, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["время", "спорт", "матч", "сигнал", "минута/период", "счёт"])
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), meta["sport"],
                        meta["match"], meta["kind"], meta["minute"], meta["score"]])
    except Exception as e:
        log.info("journal error: %s", e)


# ---------- Telegram ----------
def tg(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(wait, 30) + 1)
                continue
            if r.status_code != 200:
                log.info("telegram %s: %s", r.status_code, r.text[:200])
            return
        except requests.RequestException as e:
            log.info("telegram error: %s", e)
            time.sleep(2)


# ---------- API ----------
def api(sport, path, **params):
    host = HOSTS[sport]
    r = None
    for attempt in range(3):
        try:
            r = requests.get(f"https://{host}{path}", headers=HEAD, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(10)
                continue
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2)
    left = r.headers.get("x-ratelimit-requests-remaining")
    if left and left.isdigit():
        remaining[host] = int(left)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        log.info("%s API errors: %s", sport, data["errors"])
    return data.get("response", [])


def quota_ok(sport):
    """Квота API-Sports обновляется в 00:00 UTC: утром спорт просыпается сам."""
    host = HOSTS[sport]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if sport in paused:
        if paused[sport] == today:
            return False
        del paused[sport]
        remaining.pop(host, None)
    left = remaining.get(host)
    if left is not None and left <= RESERVE:
        paused[sport] = today
        tg(f"⚠️ {sport}: квота запросов на сегодня закончилась, спорт на паузе до 00:00 UTC")
        return False
    return True


def fire(key_parts, text, meta, ev=None):
    key = "|".join(map(str, key_parts))
    if key in sent:
        return
    sent[key] = time.time()
    tg(text)
    log.info("SIGNAL %s | %s | %s", meta["sport"], meta["match"], meta["kind"])
    journal(meta)
    if ev:
        pending.append({"fid": key_parts[1], "t": ev[0], "ev": ev[1], "match": meta["match"],
                        "ts": time.time(), "checked": time.time()})
    save_state()


def num(v):
    if v is None:
        return 0
    if isinstance(v, str):
        v = v.replace("%", "")
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


# ============ ФУТБОЛ ============
def parse_stats(resp):
    if len(resp) < 2:
        return {}
    out = {}
    for side, block in zip(("home", "away"), resp[:2]):
        d = {s["type"]: num(s["value"]) for s in block["statistics"]}
        out[side] = dict(
            shots=d.get("Total Shots", 0),
            on_target=d.get("Shots on Goal", 0),
            corners=d.get("Corner Kicks", 0),
            fouls=d.get("Fouls", 0),
            yellow=d.get("Yellow Cards", 0),
            red=d.get("Red Cards", 0),
        )
    return out


def delta(fid, minute, key):
    """Прирост за WINDOW_MIN минут: (сумма, (дом, гости))."""
    h = history[fid]
    old = next((s for m, s in h if m >= minute - WINDOW_MIN), h[0][1])
    new = h[-1][1]
    dh = new["home"][key] - old["home"][key]
    da = new["away"][key] - old["away"][key]
    return dh + da, (dh, da)


def analyse_football(fx):
    fid = fx["fixture"]["id"]
    if nostats.get(fid, 0) >= 2:           # у этого матча нет статистики: не тратим запросы
        return
    minute = fx["fixture"]["status"].get("elapsed") or 0
    gh, ga = fx["goals"]["home"] or 0, fx["goals"]["away"] or 0
    diff, goals = abs(gh - ga), gh + ga
    home, away = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
    head = (f"⚽ <b>{home} — {away}</b>\n{fx['league']['name']} | {minute}' | "
            f"счёт {gh}:{ga}\n")

    stats = parse_stats(api("football", "/fixtures/statistics", fixture=fid))
    if not stats:
        nostats[fid] = nostats.get(fid, 0) + 1
        return
    hist = history.setdefault(fid, deque(maxlen=40))
    hist.append((minute, stats))
    if len(hist) < 2:
        return

    def emit(kind, text, t=None, **ev):
        meta = dict(sport="football", match=f"{home} — {away}", kind=LABEL.get(t, "Удаление" if kind.startswith("red") else kind),
                    minute=f"{minute}'", score=f"{gh}:{ga}")
        fire(("football", fid, kind), head + text, meta, (t, ev) if t else None)

    d = {k: delta(fid, minute, k) for k in ("shots", "on_target", "corners", "fouls")}
    shots, sot = d["shots"][0], d["on_target"][0]
    corners, fouls = d["corners"][0], d["fouls"][0]
    early = minute < EARLY_UNTIL

    # --- ТБ ---
    g = {**GOAL, **(GOAL_EARLY if early else {})}
    if g["min_from"] <= minute <= g["min_to"] and diff <= g["max_diff"] \
            and shots >= g["shots"] and sot >= g["on_target"]:
        emit(f"goal{goals}", f"🔥 <b>ТБ {goals + 0.5}</b>\n"
             f"За {WINDOW_MIN} мин: ударов {shots}, в створ {sot}", "goal", goals=goals)

    # --- ТБ 1-го тайма при 0:0 ---
    f = FH
    if goals == 0 and f["min_from"] <= minute <= f["min_to"] \
            and shots >= f["shots"] and sot >= f["on_target"]:
        emit("fh", "⏱ <b>ТБ 1-го тайма 0.5</b>\n"
             f"За {WINDOW_MIN} мин: ударов {shots}, в створ {sot}", "fh")

    # --- Обе забьют: одна забила, вторая давит ---
    b = BTTS
    if b["min_from"] <= minute <= b["min_to"] and (gh > 0) != (ga > 0):
        i = 1 if gh > 0 else 0                       # кто ещё не забил
        side, team = ("away", away) if i == 1 else ("home", home)
        if stats[side]["on_target"] >= b["opp_sot"] and d["shots"][1][i] >= b["opp_shots_window"]:
            emit("btts", "🎯 <b>Обе забьют — ДА</b>\n"
                 f"{team} не забивал, но давит: в створ {stats[side]['on_target']}, "
                 f"ударов за {WINDOW_MIN} мин {d['shots'][1][i]}", "btts")

    # --- Угловые ---
    c = {**CORN, **(CORN_EARLY if early else {})}
    if c["min_from"] <= minute <= c["min_to"] and corners >= c["corners"] and shots >= c["shots"]:
        cur = stats["home"]["corners"] + stats["away"]["corners"]
        emit(f"corn{cur}", f"🚩 <b>Угловые ТБ {cur + 1.5}</b>\n"
             f"За {WINDOW_MIN} мин: угловых {corners}, ударов {shots}\nВсего угловых: {cur}",
             "corn", cur=cur)

    # --- Жёлтые карточки ---
    k = {**CARD, **(CARD_EARLY if early else {})}
    if k["min_from"] <= minute <= k["min_to"] and fouls >= k["fouls"] and diff <= k["max_diff"]:
        cur = stats["home"]["yellow"] + stats["away"]["yellow"]
        emit(f"card{cur}", f"🟨 <b>ЖК ТБ {cur + 0.5}</b>\n"
             f"За {WINDOW_MIN} мин: фолов {fouls}, игра грубая\nВсего ЖК: {cur}", "card", cur=cur)

    # --- ИТБ: одна команда доминирует ---
    t = ITB
    if t["min_from"] <= minute <= t["min_to"]:
        for i, (team, team_goals) in enumerate(((home, gh), (away, ga))):
            my_sh, opp_sh = d["shots"][1][i], d["shots"][1][1 - i]
            my_sot = d["on_target"][1][i]
            if my_sh >= t["shots"] and my_sot >= t["on_target"] and opp_sh <= t["opp_max"]:
                emit(f"itb{i}_{team_goals}", f"💪 <b>ИТБ {team} {team_goals + 0.5}</b>\n"
                     f"За {WINDOW_MIN} мин: ударов {my_sh} (в створ {my_sot}) против {opp_sh}",
                     "itb", i=i, goals=team_goals)

    # --- Удаление ---
    r = RED
    if r["min_from"] <= minute <= r["min_to"]:
        rh, ra = stats["home"]["red"], stats["away"]["red"]
        if rh != ra:
            bad, good = (home, away) if rh > ra else (away, home)
            emit(f"red{rh}_{ra}", f"🟥 <b>Удаление: {bad}</b>\n"
                 f"Игра в большинстве у {good}: смотрите победу/ИТБ {good}, ТБ")


# ---------- авто-проверка футбольных сигналов ----------
FINISHED = {"FT", "AET", "PEN"}
VOID = {"PST", "CANC", "ABD", "AWD", "WO"}


def judge(p, fx):
    """True/False = итог сигнала, 'void' = не считать, None = матч ещё не закончен."""
    st = fx["fixture"]["status"]["short"]
    if st in VOID:
        return "void"
    if st not in FINISHED:
        return None
    gh, ga = fx["goals"]["home"] or 0, fx["goals"]["away"] or 0
    t, ev = p["t"], p["ev"]
    if t == "goal":
        return gh + ga > ev["goals"]
    if t == "fh":
        ht = (fx.get("score") or {}).get("halftime") or {}
        return (ht.get("home") or 0) + (ht.get("away") or 0) >= 1
    if t == "btts":
        return gh > 0 and ga > 0
    if t == "itb":
        return (ga if ev["i"] == 1 else gh) > ev["goals"]
    if t in ("corn", "card"):
        s = parse_stats(api("football", "/fixtures/statistics", fixture=p["fid"]))
        if not s:
            return "void"
        if t == "corn":
            return s["home"]["corners"] + s["away"]["corners"] >= ev["cur"] + 2
        return s["home"]["yellow"] + s["away"]["yellow"] >= ev["cur"] + 1
    return "void"


def resolve_pending(live_ids):
    now, keep = time.time(), []
    for p in pending:
        if now - p["ts"] > 12 * 3600:
            continue                                       # слишком старое: выбрасываем
        if p["fid"] in live_ids or now - p["checked"] < 300 or not quota_ok("football"):
            keep.append(p)
            continue
        p["checked"] = now
        try:
            resp = api("football", "/fixtures", id=p["fid"])
            res = judge(p, resp[0]) if resp else None
        except Exception as e:
            log.info("resolve error: %s", e)
            res = None
        if res is None:
            keep.append(p)
        elif res != "void":
            w, n = tally.get(p["t"], [0, 0])
            w, n = w + int(res), n + 1
            tally[p["t"]] = [w, n]
            tg(f"{'✅ ЗАШЁЛ' if res else '❌ НЕ ЗАШЁЛ'}: {LABEL[p['t']]} — {p['match']}\n"
               f"Итого {LABEL[p['t']]}: {w}/{n} ({round(100 * w / n)}%)")
    pending[:] = keep
    save_state()


# ============ ДРУГИЕ СПОРТЫ ============
def pts(side):
    if isinstance(side, (int, float)):
        return int(side)
    if isinstance(side, dict):
        for k in ("total", "points", "score"):
            if isinstance(side.get(k), (int, float)):
                return int(side[k])
        ls = side.get("linescore")
        if isinstance(ls, list):
            return sum(num(x) for x in ls)
    return 0


def sides(g):
    t, s = g.get("teams") or {}, g.get("scores") or {}
    home = (t.get("home") or {}).get("name", "Хозяева")
    away = (t.get("away") or t.get("visitors") or {}).get("name", "Гости")
    sa = s.get("away") if "away" in s else s.get("visitors")
    return home, away, s.get("home"), sa


def league_of(g):
    lg = g.get("league")
    return lg.get("name", "") if isinstance(lg, dict) else (lg or "")


def progress(g, total_periods):
    """(доля сыгранного, номер текущего периода) или None."""
    st = g.get("status") or {}
    short = str(st.get("short") or "").upper()
    per = g.get("periods")
    if isinstance(per, dict) and isinstance(per.get("current"), int):   # NBA
        cur = per["current"]
    elif short == "HT":
        return 0.5, total_periods // 2
    else:
        m = re.search(r"(\d+)", short)
        if not m:
            return None
        cur = int(m.group(1))
    if cur < 1 or cur > total_periods:
        return None
    return (cur - 0.5) / total_periods, cur


def analyse_totals(sport, g):
    cfg = TOTALS[sport]
    pr = progress(g, cfg["periods"])
    if not pr:
        return
    frac, cur = pr
    if cur < cfg["min_period"]:
        return
    home, away, sh, sa = sides(g)
    ph, pa = pts(sh), pts(sa)
    total, gid = ph + pa, g.get("id")
    proj = round(total / frac)
    head = (f"{ICON[sport]} <b>{home} — {away}</b>\n{league_of(g)} | "
            f"период {cur}/{cfg['periods']} | счёт {ph}:{pa}\n")

    def emit(kind, text, label):
        meta = dict(sport=sport, match=f"{home} — {away}", kind=label,
                    minute=f"период {cur}", score=f"{ph}:{pa}")
        fire((sport, gid, kind), head + text, meta)

    if proj >= cfg["over"]:
        emit("over", "🔥 <b>ТБ</b>: высокий темп\n"
             f"Прогноз тотала ≈ {proj} (порог {cfg['over']}). Берите ТБ, если линия заметно ниже.", "ТБ")
    elif proj <= cfg["under"] and total > 0:
        emit("under", "🧊 <b>ТМ</b>: низкий темп\n"
             f"Прогноз тотала ≈ {proj} (порог {cfg['under']}). Берите ТМ, если линия заметно выше.", "ТМ")
    if cur >= cfg.get("close_from", cfg["periods"]) and abs(ph - pa) <= cfg["close"]:
        emit("close", "⚔️ <b>Близкая концовка</b>\n"
             f"Разница {abs(ph - pa)}: ждите борьбы до конца (ТБ/овертайм/фора).", "Концовка")


def analyse_volleyball(g):
    home, away, sh, sa = sides(g)
    sets_h, sets_a, gid = num(sh), num(sa), g.get("id")
    close = 0
    for v in (g.get("periods") or {}).values():
        m = re.match(r"\s*(\d+)\s*-\s*(\d+)", str(v or ""))
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if max(a, b) >= 20 and abs(a - b) <= 3:
                close += 1
    head = f"🏐 <b>{home} — {away}</b>\n{league_of(g)} | сеты {sets_h}:{sets_a}\n"

    def emit(kind, text, label):
        meta = dict(sport="volleyball", match=f"{home} — {away}", kind=label,
                    minute=f"сеты {sets_h}:{sets_a}", score=f"{sets_h}:{sets_a}")
        fire(("volleyball", gid, kind), head + text, meta)

    if sets_h == 1 and sets_a == 1:
        emit("sets", "🔥 <b>ТБ сетов 3.5</b>\nСчёт по сетам 1:1, матч уйдёт минимум в 3-й сет.", "ТБ сетов")
    if sets_h + sets_a >= 2 and close >= 2:
        emit("close", "⚔️ <b>Равная борьба</b>\nНесколько сетов с разницей до 3 очков: "
             "ТБ очков / ТБ сетов.", "Равные сеты")


# ============ ЦИКЛ ============
def run_sport(sport):
    if not quota_ok(sport):
        return
    if sport == "football":
        params = {"live": "-".join(map(str, LEAGUE_IDS)) if LEAGUE_IDS else "all"}
        all_live = api("football", "/fixtures", **params)
        ids = {f["fixture"]["id"] for f in all_live}
        for store in (history, nostats):
            for fid in [x for x in store if x not in ids]:
                del store[fid]
        live = [f for f in all_live if (f["fixture"]["status"].get("elapsed") or 0) >= 15]
        for fx in live[:MAX_MATCHES]:
            if not quota_ok("football"):
                break
            try:
                analyse_football(fx)
            except Exception as e:
                log.info("football match error: %s", e)
        resolve_pending(ids)
        return

    for g in api(sport, "/games", live="all"):
        try:
            if sport == "volleyball":
                analyse_volleyball(g)
            else:
                analyse_totals(sport, g)
        except Exception as e:
            log.info("%s game error: %s", sport, e)


def main():
    if not (API_KEY and TG_TOKEN and TG_CHAT_ID):
        raise SystemExit("Не заданы API_KEY, TG_TOKEN или TG_CHAT_ID (переменные окружения / Secrets)")
    run_minutes = float(os.getenv("RUN_MINUTES", "0") or 0)      # 0 = работать бесконечно
    deadline = time.time() + run_minutes * 60 if run_minutes else None
    load_state()
    on = [s for s, c in SPORTS.items() if c["on"]]
    msg = "✅ Бот запущен: " + ", ".join(f"{ICON[s]} {s}" for s in on)
    if tally:
        msg += "\n\nПроходимость сигналов:\n" + "\n".join(
            f"{LABEL[t]}: {w}/{n} ({round(100 * w / n)}%)" for t, (w, n) in tally.items() if n)
    tg(msg)
    last = {}
    while deadline is None or time.time() < deadline:
        now = time.time()
        for sport, cfg in SPORTS.items():
            if not cfg["on"] or now - last.get(sport, 0) < cfg["every"]:
                continue
            last[sport] = now
            try:
                run_sport(sport)
            except Exception as e:
                log.info("%s error: %s", sport, e)
        time.sleep(5)
    save_state()
    log.info("Время работы вышло, завершаюсь (следующий запуск подхватит состояние)")


if __name__ == "__main__":
    main()
