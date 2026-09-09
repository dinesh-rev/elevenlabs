"""Commentary server: holds the match feed open, speaks a line when you ask.

Start it once and leave it running:

    python3 live_commentary.py                  # 127.0.0.1:8080
    python3 live_commentary.py --host 0.0.0.0   # reachable from the OBS box
    python3 live_commentary.py --dry            # pick lines, never call the API

Then trigger from anywhere:

    curl localhost:8080/say/smash
    curl localhost:8080/state
    curl "localhost:8080/speak?text=Great+smash+down+the+line"

Because the socket is already connected there is no startup wait: the match
state is in memory before the request arrives.

Phrase loading and filling are copied from eleven.py rather than imported --
that script does its work at module level, so importing it would fire a TTS
request as a side effect. eleven.py keeps working exactly as before.
"""
import argparse
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import live

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "configs" / "audio"
CACHE_DIR = AUDIO_DIR / "cache"

VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"   # George
MODEL_ID = "eleven_v3"
OUTPUT_FORMAT = "mp3_44100_128"

CATEGORIES = ["match_start", "rally", "smash", "highlights", "winners", "convo"]

DRY_RUN = False


def load_env_file(path):
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# match state, kept current by live.py on its own thread
# ---------------------------------------------------------------------------
STATE_LOCK = threading.Lock()
STATE = {
    "court": "", "court_name": "", "match_id": "",
    "player1": "", "player2": "", "country1": "", "country2": "",
    "score1": 0, "score2": 0, "games1": 0, "games2": 0, "set_no": 0,
    "status": "", "completed": False, "winner": "", "confirmed": False,
    "swapped": False, "last_scorer": "",
}


def update(record):
    """Copy one court record into STATE. Runs on the websocket thread."""
    with STATE_LOCK:
        if record["match_id"] != STATE["match_id"]:
            # a new match on this court: the previous match's names must not
            # linger while the feed settles on the new ones
            for k in ("player1", "player2", "country1", "country2", "winner"):
                STATE[k] = ""
            STATE["last_scorer"] = ""
            STATE["score1"] = STATE["score2"] = 0
        elif (record["score1"] == STATE["score1"] + 1
                and record["score2"] == STATE["score2"]):
            STATE["last_scorer"] = "1"   # only +1 is a point; larger jumps and
        elif (record["score2"] == STATE["score2"] + 1
                and record["score1"] == STATE["score1"]):
            STATE["last_scorer"] = "2"   # any decrease are the feed correcting

        keep_names = record["confirmed"]
        for k, v in record.items():
            if k in ("player1", "player2", "country1", "country2") and not keep_names:
                continue     # an unconfirmed name must never reach commentary
            STATE[k] = v


def snapshot():
    with STATE_LOCK:
        return dict(STATE)


# ---------------------------------------------------------------------------
# phrases  (copies of eleven.py's loader/filler, see the module docstring)
# ---------------------------------------------------------------------------
def load_phrases(category, section=None):
    text = (BASE_DIR / "phrases" / f"{category}.txt").read_text(encoding="utf-8")
    blocks = []
    current = None
    for block in text.split("\n\n"):
        lines = []
        for l in block.strip().split("\n"):
            if l.startswith("##"):
                current = l.lstrip("#").strip()
            elif not l.startswith("#"):
                lines.append(l)
        if lines and (section is None or current == section):
            blocks.append("\n".join(lines))
    return blocks


def fill(text, s):
    """Substitute {player}, {winner}, {opponent}, {country}, {court}."""
    if s["winner"]:
        winner = s["winner"]
    elif (s["games1"], s["score1"]) >= (s["games2"], s["score2"]):
        winner = s["player1"]
    else:
        winner = s["player2"]
    opponent = s["player2"] if winner == s["player1"] else s["player1"]
    # {player} is whoever won the most recent point; the feed never says who
    # played the shot, so this is the closest it can get
    player = s["player2"] if s["last_scorer"] == "2" else s["player1"]
    for key, value in {"player": player, "winner": winner, "opponent": opponent,
                       "country": s["country1"], "court": s["court"]}.items():
        if value:
            text = text.replace("{%s}" % key, str(value))
    # a name ending in an initial ("C.W.") plus the phrase's own full stop reads
    # as a stumble; the lookarounds keep deliberate "..." pauses intact
    return re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)


def pick(category, section, s, exclude=()):
    """A phrase with every placeholder filled, or None if names are missing."""
    blocks = load_phrases(category, section)
    random.shuffle(blocks)
    for block in blocks:
        filled = fill(block, s)
        if re.search(r"\{\w+\}", filled):
            continue                      # needs a name we do not have
        if filled not in exclude:
            return filled
    return None


# recently spoken lines, so the same phrase is not repeated back to back
RECENT = []
RECENT_MAX = 6


# ---------------------------------------------------------------------------
# synthesis + playback
# ---------------------------------------------------------------------------
_client = None


def client():
    global _client
    if _client is None:
        from elevenlabs import ElevenLabs
        key = os.getenv("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY not set (put it in eleven/.env)")
        _client = ElevenLabs(api_key=key)
    return _client


def render(text):
    """Synthesise to a content-addressed mp3. A repeated line costs nothing."""
    from elevenlabs.types import VoiceSettings
    from elevenlabs.core.api_error import ApiError

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(f"{VOICE_ID}|{MODEL_ID}|{text}".encode()).hexdigest()[:16]
    path = CACHE_DIR / f"{digest}.mp3"
    if path.exists():
        return path, True
    if DRY_RUN:
        return None, False
    try:
        audio = client().text_to_speech.convert(
            text=text, voice_id=VOICE_ID, model_id=MODEL_ID,
            output_format=OUTPUT_FORMAT,
            voice_settings=VoiceSettings(stability=0.0, similarity_boost=0.75,
                                         style=1.0, use_speaker_boost=True),
        )
        data = b"".join(audio)   # buffer first: the API can fail mid-stream
    except ApiError as e:
        detail = (e.body or {}).get("detail", {}) if isinstance(e.body, dict) else {}
        code = detail.get("code", "api_error")
        print(f"[tts failed {code}] {detail.get('message', e.body)}",
              file=sys.stderr, flush=True)
        return None, False
    tmp = path.with_suffix(".mp3.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    print(f"[rendered] {len(text)} chars -> {path.name}", flush=True)
    return path, False


# ffplay is preferred everywhere: it blocks until the clip ends, which is what
# keeps two triggers from overlapping. vlc/vlc.exe is the Windows spelling.
PLAYER_CMD = next(
    (c for c in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
                 ["mpv", "--no-video", "--really-quiet"],
                 ["cvlc", "--play-and-exit", "--quiet"],
                 ["vlc", "--play-and-exit", "--intf", "dummy"])
     if shutil.which(c[0])), None)


def play(path):
    """Play one clip, blocking until it finishes."""
    if PLAYER_CMD:
        subprocess.run(PLAYER_CMD + [str(path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif hasattr(os, "startfile"):
        # Windows without ffmpeg: hands the file to the default player. It
        # returns immediately, so clips can overlap -- install ffmpeg to fix.
        os.startfile(str(path))  # noqa: S606
    else:
        print("[no audio player found: install ffmpeg]", file=sys.stderr, flush=True)

JOBS = queue.Queue()


def speaker():
    """One clip at a time, so two triggers never talk over each other."""
    while True:
        text, save_as = JOBS.get()
        try:
            path, cached = render(text)
            if path and save_as:
                # same file eleven.py writes, so the rest of your workflow is
                # unchanged; written via a temp file so a reader never sees half
                tmp = save_as.with_suffix(".mp3.part")
                shutil.copyfile(path, tmp)
                os.replace(tmp, save_as)
            if path:
                play(path)
        except Exception as e:
            print(f"[speaker] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        finally:
            JOBS.task_done()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
HELP = """commentary server

GET /state              current match, as JSON
GET /<category>         one per category, works like eleven.py: random phrase,
                        speaks it, and saves output_Audio1_<category>.mp3
                        {cats}
GET /say/<category>     same, but plays from cache only (writes no named file)
                        optional ?section=with-players|no-players
GET /speak?text=...     speak exact text
GET /queue              how many clips are waiting
GET /stop               drop everything queued
GET /health             ok
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = (json.dumps(payload, indent=2) if not isinstance(payload, str)
                else payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if not isinstance(payload, str)
                         else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # the routes log themselves; this would double every line

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        query = parse_qs(url.query)

        if not parts:
            return self._send(200, HELP.format(cats=", ".join(CATEGORIES)))
        route = parts[0]

        if route == "health":
            return self._send(200, {"ok": True})

        if route == "state":
            return self._send(200, snapshot())

        if route == "queue":
            return self._send(200, {"waiting": JOBS.qsize()})

        if route == "stop":
            dropped = 0
            while not JOBS.empty():
                try:
                    JOBS.get_nowait(); JOBS.task_done(); dropped += 1
                except queue.Empty:
                    break
            return self._send(200, {"dropped": dropped})

        if route == "speak":
            text = (query.get("text") or [""])[0].strip()
            if not text:
                return self._send(400, {"error": "?text= is required"})
            JOBS.put((text, None))
            print(f"[speak] {text[:80]}", flush=True)
            return self._send(200, {"text": text, "queued": JOBS.qsize()})

        if route in CATEGORIES:
            # /smash and friends: behave like eleven.py, including writing
            # configs/audio/output_Audio1_<category>.mp3
            return self._say(route, (query.get("section") or [None])[0],
                             save=True)

        if route == "say":
            if len(parts) < 2:
                return self._send(400, {"error": "use /say/<category>",
                                        "categories": CATEGORIES})
            if parts[1] not in CATEGORIES:
                return self._send(404, {"error": f"unknown category {parts[1]!r}",
                                        "categories": CATEGORIES})
            return self._say(parts[1], (query.get("section") or [None])[0],
                             save=False)

        return self._send(404, {"error": "no such route", "help": "GET /"})

    def _say(self, category, section, save):
        """Pick a phrase for `category` and queue it, exactly as eleven.py does."""
        s = snapshot()
        if category == "smash" and section is None:
            section = "with-players" if s["player1"] else "no-players"
        text = pick(category, section, s, exclude=RECENT)
        if text is None and category == "smash":
            text = pick(category, "no-players", s, exclude=RECENT)
        if text is None:
            return self._send(409, {
                "error": f"no usable {category} phrase without player names",
                "hint": "the feed has not confirmed names for this court yet",
                "state": s})
        RECENT.append(text)
        del RECENT[:-RECENT_MAX]
        save_as = AUDIO_DIR / f"output_Audio1_{category}.mp3" if save else None
        JOBS.put((text, save_as))
        print(f"[{category}] {text[:80]}", flush=True)
        return self._send(200, {"category": category, "section": section,
                                "text": text, "chars": len(text),
                                "queued": JOBS.qsize(),
                                "saved_as": str(save_as) if save_as else None,
                                "player1": s["player1"], "player2": s["player2"]})


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to accept calls from other machines")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--court", default=live.COURT, help='court to follow; "" for all')
    ap.add_argument("--key", default=live.TOURNAMENT_KEY)
    ap.add_argument("--dry", action="store_true",
                    help="choose lines but never call ElevenLabs")
    args = ap.parse_args()
    DRY_RUN = args.dry
    live.COURT = args.court

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=speaker, daemon=True, name="speaker").start()
    live.start(key=args.key, on_change=update)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"commentary server on http://{args.host}:{args.port}"
          f"  court={args.court or 'all'}{'  DRY RUN' if DRY_RUN else ''}",
          flush=True)
    print(f"  curl {args.host}:{args.port}/say/smash", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
