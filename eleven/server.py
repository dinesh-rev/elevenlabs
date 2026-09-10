"""The commentary server. Start it once and leave it running.

Two ways to make it speak:

  1. You supply the words
        curl "localhost:8080/play?text=[excited] What a smash from&name=Kannama"
        curl "localhost:8080/play?text=[emphasis] {name} takes it&name=Akmal"

  2. It picks from phrases/<category>.txt using the live match names
        curl localhost:8080/smash
        curl localhost:8080/winners

Either way the line is broken into pieces -- the words, and the player name --
each rendered once and kept. The phrase half is shared by every player, so the
number of API calls is (phrases + players), not (phrases x players).

Pieces are raw PCM so they join by plain byte concatenation, with no gaps. The
join is encoded to mp3 only at the end, because mp3 frame padding is exactly
what makes joined mp3 clips click at the seams.

    python3 server.py --dry            # choose and split, never call the API
    python3 server.py --host 0.0.0.0   # reachable from another machine
"""
import argparse
import queue
import random
import re
import threading

from flask import Flask, request, jsonify, send_file

import core
import live
from core import BASE_DIR, CATEGORIES, AUDIO_DIR, PLACEHOLDER, SAMPLE_RATE

app = Flask(__name__)
TAG = re.compile(r"\[[a-z ]+\]")

DRY = False
STATE = core.MatchState()
JOBS = queue.Queue()
RECENT = []                 # last few blocks, so phrases do not repeat


# ---------------------------------------------------------------------------
# splitting a line into pieces
# ---------------------------------------------------------------------------
def speaks(text):
    """True if there are real words here, once audio tags are removed."""
    return bool(re.search(r"[A-Za-z0-9]", TAG.sub("", text)))


def split_phrase(raw, values):
    """[(kind, text)] in speaking order, kind being "phrase" or "name".

    Text with no placeholder comes back as a single phrase piece.
    """
    pieces, seen = [], ""
    for chunk in re.split(r"(\{\w+\})", raw):
        if not chunk:
            continue
        seen += chunk
        name = PLACEHOLDER.fullmatch(chunk)
        if name:
            # A name read on its own sounds flat and final, so give it the
            # audio tag that is in force around it.
            tag = TAG.findall(seen[:-len(chunk)])
            value = values.get(name.group(1), "")
            if value:
                pieces.append(["name", f"{tag[-1]} {value}" if tag else value])
        elif speaks(chunk):
            pieces.append(["phrase", chunk])
        elif pieces:
            # "!" or ". [emphasis] " has no words. Rendering it alone would be
            # a paid call for near-silence, so its punctuation joins the piece
            # before it -- which also lets the "!" shape how the name is said.
            pieces[-1][1] += TAG.sub("", chunk).strip()
    return [tuple(p) for p in pieces]


def readable(e):
    """A short message from an ElevenLabs error.

    ApiError stringifies to the whole HTTP header dump, which is unreadable in
    the UI. The useful part is body["detail"].
    """
    body = getattr(e, "body", None)
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        d = body["detail"]
        return d.get("message") or d.get("code") or str(d)
    return str(e)


def resolved(raw, values):
    """The line as a human reads it: placeholders filled, tags not duplicated.

    Not the same as joining the pieces -- those carry a repeated tag so each
    can be rendered on its own, which looks wrong written down.
    """
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), raw)


def build(pieces, stem):
    """Render every piece, join in order, save one clip. Returns a report."""
    audio, detail, made, reused = [], [], 0, 0
    for kind, text in pieces:
        pcm, cached = core.render_pcm(text, dry=DRY)
        audio.append(pcm)
        detail.append({"kind": kind, "text": text, "cached": cached})
        if cached:
            reused += len(text)
        else:
            made += len(text)
    joined = b"".join(audio)          # same rate and channels, so this is it
    saved = core.save_clip(stem, joined) if joined and stem else None
    if saved:
        JOBS.put(saved)
    return {"pieces": detail, "synthesized_chars": made, "reused_chars": reused,
            "seconds": round(len(joined) / 2 / SAMPLE_RATE, 2),
            "saved_as": str(saved) if saved else None}


def speaker():
    """One clip at a time, so two triggers never talk over each other."""
    while True:
        path = JOBS.get()
        try:
            core.play(path)
        finally:
            JOBS.task_done()


# ---------------------------------------------------------------------------
# 1. you supply the words
# ---------------------------------------------------------------------------
@app.route("/play", methods=["GET", "POST"])
def play():
    src = request.values                  # query string or form body
    text = (src.get("text") or "").strip()
    name = (src.get("name") or "").strip()
    if not text:
        return jsonify(error="text is required"), 400
    if name and "{name}" not in text:
        text += " {name}"                 # no marker given, so name goes last
    values = {"name": name}
    try:
        report = build(split_phrase(text, values), None if DRY else AUDIO_DIR / "play")
    except Exception as e:
        return jsonify(error=readable(e)), 502
    spoken = resolved(text, values)
    print(f"[play] {spoken[:70]}", flush=True)
    return jsonify(text=spoken, **report)


# ---------------------------------------------------------------------------
# 2. it picks from your phrase files, using the live match
# ---------------------------------------------------------------------------
def pick_raw(category, section, values):
    """A random block, placeholders intact, whose names we actually have."""
    blocks = core.load_phrases(category, section)
    random.shuffle(blocks)
    for block in blocks:
        if block in RECENT:
            continue
        if all(values.get(n) for n in PLACEHOLDER.findall(block)):
            return block
    return None


@app.route("/<category>", methods=["GET", "POST"])
def say(category):
    if category not in CATEGORIES:
        return jsonify(error=f"unknown category {category!r}",
                       categories=CATEGORIES), 404
    s = STATE.snapshot()
    values = core.values_for(s)
    # the UI can say who the line is about; without it, whoever won the last
    # point is used, which is all the feed can tell us
    chosen = (request.values.get("player") or "").upper()
    if chosen in ("A", "1"):
        values["player"] = s["player1"]
    elif chosen in ("B", "2"):
        values["player"] = s["player2"]
    section = request.values.get("section")
    if category == "smash" and section is None:
        section = "with-players" if values["player"] else "no-players"
    raw = pick_raw(category, section, values)
    if raw is None and category == "smash":
        raw = pick_raw(category, "no-players", values)
    if raw is None:
        return jsonify(error=f"no usable {category} phrase without player names",
                       hint="the feed has not confirmed names for this court yet",
                       state=s), 409
    RECENT.append(raw)
    del RECENT[:-6]
    try:
        report = build(split_phrase(raw, values),
                       None if DRY else AUDIO_DIR / f"output_Audio1_{category}")
    except Exception as e:
        return jsonify(error=readable(e), category=category), 502
    spoken = resolved(raw, values)
    print(f"[{category}] {spoken[:70]}  (new {report['synthesized_chars']}, "
          f"reused {report['reused_chars']})", flush=True)
    return jsonify(category=category, text=spoken, **report)


# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return ("commentary server\n\n"
            "GET /play?text=...&name=...   speak words you supply\n"
            "                              put {name} in text to place the name\n"
            f"GET /<category>               {', '.join(CATEGORIES)}\n"
            "GET /state                    current match\n"
            "GET /pieces                   how many pieces are cached\n"
            "GET /warm                     cost of pre-rendering every phrase\n"
            "GET /warm?confirm=1           actually pre-render them\n"
            "GET /ui                       operator panel, open in a browser\n"
            "GET /health\n"), 200, {"Content-Type": "text/plain"}


@app.get("/audio/<name>")
def audio(name):
    """Serve a finished clip so the browser can play it too.

    The server also plays it locally through ffplay; this is for when the panel
    is open on a different machine from the one running the server.
    """
    f = (AUDIO_DIR / name).resolve()
    if f.parent != AUDIO_DIR.resolve() or f.suffix not in (".mp3", ".wav"):
        return jsonify(error="no such clip"), 404      # keep it to this folder
    if not f.is_file():
        return jsonify(error="no such clip"), 404
    return send_file(f, mimetype="audio/mpeg" if f.suffix == ".mp3" else "audio/wav")


@app.get("/ui")
def ui():
    """The operator panel. Served from here so it can call the API directly."""
    return (BASE_DIR / "ui.html").read_text(encoding="utf-8"), 200, \
           {"Content-Type": "text/html; charset=utf-8"}


@app.get("/state")
def state():
    return jsonify(STATE.snapshot())


@app.get("/health")
def health():
    return jsonify(ok=True, queued=JOBS.qsize(), dry_run=DRY)


@app.get("/pieces")
def pieces():
    files = list(core.PIECES_DIR.glob("*.pcm")) if core.PIECES_DIR.is_dir() else []
    return jsonify(cached=len(files),
                   megabytes=round(sum(f.stat().st_size for f in files) / 1e6, 1))


@app.route("/warm", methods=["GET", "POST"])
def warm():
    """Pre-render every phrase piece, i.e. all the ones with no name in them."""
    todo = []
    for category in CATEGORIES:
        for block in core.load_phrases(category):
            dummy = {n: "x" for n in PLACEHOLDER.findall(block)}
            for kind, text in split_phrase(block, dummy):
                # split_phrase, so these are exactly what a request looks up
                if kind == "phrase" and text not in todo:
                    if not core.piece_path(text).exists():
                        todo.append(text)
    chars = sum(len(t) for t in todo)
    if request.values.get("confirm") != "1":
        return jsonify(pieces_missing=len(todo), characters=chars,
                       estimated_credits=chars,
                       note="eleven_v3 bills about 1 credit per character",
                       to_run="add ?confirm=1")
    for done, text in enumerate(todo):
        try:
            core.render_pcm(text, dry=DRY)
        except Exception as e:
            return jsonify(error=readable(e), rendered=done,
                           remaining=len(todo) - done), 502
    return jsonify(rendered=len(todo), characters=chars)


def main():
    global DRY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to accept calls from other machines")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--court", default=live.COURT, help='court to follow; "" for all')
    ap.add_argument("--key", default=live.TOURNAMENT_KEY)
    ap.add_argument("--dry", action="store_true", help="never call ElevenLabs")
    args = ap.parse_args()
    DRY = args.dry
    live.COURT = args.court

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=speaker, daemon=True, name="speaker").start()
    live.start(key=args.key, on_change=STATE.update)

    print(f"commentary server on http://{args.host}:{args.port}"
          f"  court={args.court or 'all'}{'  DRY RUN' if DRY else ''}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
