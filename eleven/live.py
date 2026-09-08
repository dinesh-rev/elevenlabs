"""Listen to the match websocket and keep the current match variables up to date.

The OCR runs on another machine and publishes to this socket; this is purely a
consumer, so the feed is treated as the source of truth.

Run it standalone to watch the values change:
    python3 live.py
    python3 live.py --key 655

Use it from another script (see eleven.py):
    import live
    live.start(on_change=lambda c: print(c))   # background thread
    live.COURTS["0"]["player1"]

Every frame is appended to logs/frames-<date>.jsonl so the event schema can be
checked after a match.
"""
import argparse
import asyncio
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

WS_URL = "wss://5a7amc2f17.execute-api.ap-south-1.amazonaws.com/prod"
TOURNAMENT_KEY = "655"

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

# The scoreboard is per court ("COURT 0") and one tournament subscription
# carries every court, so state is keyed by court number.
COURTS = {}  # court -> dict(court player1 player2 country1 country2 score1 score2 round status)

BLANK = {"court": "", "player1": "", "player2": "", "country1": "", "country2": "",
         "score1": 0, "score2": 0, "round": "", "status": ""}

# Names on a scoreboard are tailed with a country code. Matching a bare
# [A-Z]{3} is not safe -- it eats real surnames like LEE, WEI or TAN.
COUNTRY_CODES = {
    "AUS", "AUT", "BEL", "BRA", "BUL", "CAN", "CHN", "TPE", "CZE", "DEN", "EGY",
    "ENG", "ESP", "EST", "FIN", "FRA", "GER", "GRE", "HKG", "HUN", "IND", "INA",
    "IRL", "ISR", "ITA", "JPN", "KAZ", "KOR", "LAT", "MAS", "MEX", "NED", "NGR",
    "NOR", "NZL", "PER", "PHI", "POL", "POR", "ROU", "RUS", "SCO", "SGP", "SIN",
    "SLO", "SRI", "SUI", "SWE", "THA", "TUR", "UKR", "USA", "VIE", "WAL",
}

NAME_KEY = re.compile(r"(player|athlete|competitor|team|name|winner|opponent)", re.I)
SKIP_KEY = re.compile(r"(id|key|uuid|url|type)$", re.I)
COURT_KEY = re.compile(r"court", re.I)
COUNTRY_KEY = re.compile(r"(country|nation|flag|noc)", re.I)
SCORE_KEY = re.compile(r"(score|points?)", re.I)
ROUND_KEY = re.compile(r"round", re.I)
STATUS_KEY = re.compile(r"(status|state)", re.I)


def clean(raw):
    """Strip scoreboard decoration from a name. Returns '' if it is not one."""
    if not isinstance(raw, str):
        return ""
    s = re.sub(r"[\[(]\s*\d+\s*[\])]", " ", raw)   # seeding marks: [1], (2)
    s = re.sub(r"\s+", " ", s).strip(" ,-/|")      # keep dots: they are initials
    if not 2 <= len(s) <= 40 or not re.search(r"[A-Za-z]", s):
        return ""
    parts = s.split()
    if len(parts) > 1 and parts[-1].upper().strip(".") in COUNTRY_CODES:
        parts.pop()                                # country is kept separately
    if not parts or s.upper().strip(".") in COUNTRY_CODES:
        return ""
    s = " ".join(parts)
    if s.isupper():  # capitalise each letter run so "C.W." survives as "C.W."
        s = re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), s)
    return s


def country_of(raw, holder=None):
    """Country code for a name: from a sibling field, else the name's own tail."""
    if isinstance(holder, dict):
        for k, v in holder.items():
            if COUNTRY_KEY.search(k) and isinstance(v, str):
                code = v.strip().upper()
                if code in COUNTRY_CODES:
                    return code
    if isinstance(raw, str):
        parts = raw.split()
        if len(parts) > 1:
            tail = parts[-1].upper().strip(".")
            if tail in COUNTRY_CODES:
                return tail
    return ""


def find_people(obj, out=None):
    """Every (name, country) pair in a frame, in the order the feed lists them."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and NAME_KEY.search(k) and not SKIP_KEY.search(k):
                name = clean(v)
                if name and name not in [n for n, _ in out]:
                    out.append((name, country_of(v, obj)))
        for v in obj.values():
            if not isinstance(v, str):
                find_people(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_people(v, out)
    return out


def find_value(obj, key_re, types=(str, int)):
    """First value in the frame whose key matches, at any depth."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_re.search(k) and isinstance(v, types) and not isinstance(v, bool):
                return v
        for v in obj.values():
            got = find_value(v, key_re, types)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = find_value(v, key_re, types)
            if got is not None:
                return got
    return None


def find_scores(obj):
    """(score1, score2) from a two-element score list, or score1/score2 keys."""
    pair = find_value(obj, SCORE_KEY, types=(list,))
    if isinstance(pair, list) and len(pair) >= 2:
        try:
            return int(pair[0]), int(pair[1])
        except (TypeError, ValueError):
            pass
    a = find_value(obj, re.compile(r"(score1|score_1|scoreA|homeScore)", re.I), (int,))
    b = find_value(obj, re.compile(r"(score2|score_2|scoreB|awayScore)", re.I), (int,))
    return (a or 0, b or 0)


def read_frame(frame):
    """Turn one frame into a court record, or None if it carries no match data."""
    people = find_people(frame)
    if len(people) < 2:
        # an idle board sends blank names, and a single-name event frame names
        # only one player: neither describes a full match
        return None
    court = find_value(frame, COURT_KEY)
    s1, s2 = find_scores(frame)
    return {
        "court": "" if court is None else str(court),
        "player1": people[0][0], "country1": people[0][1],
        "player2": people[1][0], "country2": people[1][1],
        "score1": s1, "score2": s2,
        "round": str(find_value(frame, ROUND_KEY) or ""),
        "status": str(find_value(frame, STATUS_KEY) or ""),
    }


async def listen(key=TOURNAMENT_KEY, on_change=None, log=True):
    """Keep COURTS current. Calls on_change(record) whenever a court changes."""
    import websockets

    sub = json.dumps({"action": "EXT_SUBSCRIBE", "tournamentKey": key})
    fh = None
    if log:
        LOG_DIR.mkdir(exist_ok=True)
        fh = open(LOG_DIR / f"frames-{datetime.now():%Y%m%d}.jsonl", "a", encoding="utf-8")
    delay = 1
    try:
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    await ws.send(sub)
                    print(f"connected, subscribed to {key}", flush=True)
                    delay = 1
                    async for raw in ws:
                        if fh:
                            fh.write(raw + "\n")
                            fh.flush()
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        record = read_frame(frame)
                        if record and record != COURTS.get(record["court"]):
                            COURTS[record["court"]] = record
                            print("court {court}: {player1} ({country1}) {score1}"
                                  " - {score2} {player2} ({country2}) "
                                  "[{round} {status}]".format(**record), flush=True)
                            if on_change:
                                on_change(record)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # API Gateway drops idle sockets at 10 min, so this is routine.
                print(f"reconnecting in {delay}s ({type(e).__name__}: {e})",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
    finally:
        if fh:
            fh.close()


def start(key=TOURNAMENT_KEY, on_change=None, log=True):
    """Run listen() in a daemon thread, for use from ordinary sync scripts."""
    t = threading.Thread(target=lambda: asyncio.run(listen(key, on_change, log)),
                         daemon=True, name="live-ws")
    t.start()
    return t


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=TOURNAMENT_KEY)
    args = ap.parse_args()
    try:
        asyncio.run(listen(args.key))
    except KeyboardInterrupt:
        print("\nstopped")
