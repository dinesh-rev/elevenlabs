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

# One tournament subscription carries every court, so frames for other courts
# arrive whether we want them or not and are dropped on arrival.
# Set COURT = "" to follow all of them again (COURTS is keyed by court number).
COURT = "2"
COURTS = {}  # court -> record, see read_frame() for the fields

# Names on a scoreboard are tailed with a country code. Matching a bare
# [A-Z]{3} is not safe -- it eats real surnames like LEE, WEI or TAN.
COUNTRY_CODES = {
    "AUS", "AUT", "BEL", "BRA", "BUL", "CAN", "CHN", "TPE", "CZE", "DEN", "EGY",
    "ENG", "ESP", "EST", "FIN", "FRA", "GER", "GRE", "HKG", "HUN", "IND", "INA",
    "IRL", "ISR", "ITA", "JPN", "KAZ", "KOR", "LAT", "MAS", "MEX", "NED", "NGR",
    "NOR", "NZL", "PER", "PHI", "POL", "POR", "ROU", "RUS", "SCO", "SGP", "SIN",
    "SLO", "SRI", "SUI", "SWE", "THA", "TUR", "UKR", "USA", "VIE", "WAL",
}

def tidy_name(raw):
    """'LOW H Y  / NG E C' -> 'Low H Y and Ng E C'.

    Doubles pairs arrive slash-separated; "and" reads better than "/" aloud.
    """
    if not isinstance(raw, str):
        return ""
    people = []
    for part in raw.split("/"):
        part = re.sub(r"[\[(]\s*\d+\s*[\])]", " ", part)   # seeding marks
        part = re.sub(r"\s+", " ", part).strip(" ,-|")
        toks = part.split()
        if len(toks) > 1 and toks[-1].upper().strip(".") in COUNTRY_CODES:
            toks.pop()                                       # kept separately
        part = " ".join(toks)
        if part.isupper():  # capitalise letter runs so "C.W." stays "C.W."
            part = re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), part)
        if part and re.search(r"[A-Za-z]", part):
            people.append(part)
    return " and ".join(people)


def country_of(raw):
    """Trailing country code on a name, if the feed puts one there."""
    if not isinstance(raw, str):
        return ""
    for part in raw.split("/"):
        toks = part.split()
        if len(toks) > 1 and toks[-1].upper().strip(".") in COUNTRY_CODES:
            return toks[-1].upper().strip(".")
    return ""


def read_frame(frame):
    """Turn an EXT_MATCH_UPDATED frame into a court record, else None."""
    if not str(frame.get("type", "")).startswith("EXT_MATCH"):
        return None                       # ACKs and anything else
    m = frame.get("match") or {}
    p1, p2 = tidy_name(m.get("t1_tabname")), tidy_name(m.get("t2_tabname"))
    if not (p1 and p2):
        return None                       # board not populated yet
    # the feed says whether it has settled on the names; anything else is a
    # reading still in flux and must not be spoken
    confirmed = m.get("nameState") == "CONFIRMED"
    side = m.get("winner")                # "A", "B" or null
    return {
        "court": str(m.get("courtNo", "")),
        "court_name": str(m.get("court", "")),
        "match_id": str(m.get("extMatchId", "")),
        "player1": p1, "player2": p2,
        "country1": country_of(m.get("t1_tabname")),
        "country2": country_of(m.get("t2_tabname")),
        "score1": int(m.get("scoreA") or 0), "score2": int(m.get("scoreB") or 0),
        "games1": int(m.get("winsA") or 0), "games2": int(m.get("winsB") or 0),
        "set_no": int(m.get("currentSet") or 0),
        "status": str(m.get("status") or ""),
        "completed": bool(m.get("completed")),
        "winner": p1 if side == "A" else p2 if side == "B" else "",
        "confirmed": confirmed,
        # passed through unused: if this ever flips mid-match it may mean the
        # board swapped sides, which would invert the A/B -> player1/2 mapping
        "swapped": bool(m.get("swapped")),
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
                        if record and COURT and record["court"] != COURT:
                            continue  # another court on the same feed
                        if record and record != COURTS.get(record["court"]):
                            COURTS[record["court"]] = record
                            print("court {court}: {player1} {score1} - {score2}"
                                  " {player2}  (games {games1}-{games2}, set"
                                  " {set_no}, {status}"
                                  "{unconfirmed})".format(
                                      unconfirmed="" if record["confirmed"]
                                      else ", NAMES UNCONFIRMED",
                                      **record), flush=True)
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
    ap.add_argument("--court", default=COURT, help='court to follow; "" for all')
    args = ap.parse_args()
    COURT = args.court
    try:
        asyncio.run(listen(args.key))
    except KeyboardInterrupt:
        print("\nstopped")
