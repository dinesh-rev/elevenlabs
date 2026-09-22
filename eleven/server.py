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
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid

from flask import Flask, request, jsonify, send_file
from werkzeug.exceptions import HTTPException

import analytics
import core
import live
import logging
from core import BASE_DIR, CATEGORIES, AUDIO_DIR, PLACEHOLDER, SAMPLE_RATE

log = logging.getLogger("commentary")
app = Flask(__name__)
TAG = re.compile(r"\[[a-z ]+\]")

DRY = False
# False = do not play clips on this machine. The caller fetches "url" from the
# reply and plays it through its own audio system instead, which is the only
# way to get control of when a clip starts: core.play() blocks to the end.
LOCAL_AUDIO = True
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


KEEP_CLIPS = 30      # how many finished clips to leave on disk


SPEAKER = re.compile(r"^\s*([A-Za-z][A-Za-z'-]*)\s*:\s*(.+)$")


def parse_turns(block):
    """[(voice_id, text)] for a "Speaker: line" block, or [] if it is not one."""
    turns = []
    for line in block.split("\n"):
        m = SPEAKER.match(line)
        if m:
            turns.append([core.voice_for(m.group(1)), m.group(2).strip()])
        elif turns and line.strip():
            turns[-1][1] += " " + line.strip()      # a turn wrapped onto two lines
    return [tuple(t) for t in turns]


def build_dialogue(block, label):
    """Render a two-voice block whole. Returns the same report shape as build()."""
    t0 = time.monotonic()
    turns = parse_turns(block)
    mp3, cached = core.render_dialogue(turns, dry=DRY)
    chars = sum(len(t) for _, t in turns)
    saved = None
    if mp3 and label:
        saved = AUDIO_DIR / f"clip_{label}_{uuid.uuid4().hex[:8]}.mp3"
        tmp = saved.with_suffix(".mp3.part")
        tmp.write_bytes(mp3)
        os.replace(tmp, saved)
        prune_clips()
        if LOCAL_AUDIO:
            JOBS.put(saved)
    return {"pieces": [{"kind": "dialogue", "turns": len(turns), "cached": cached,
                        "chars": chars}],
            "synthesized_chars": 0 if cached else chars,
            "reused_chars": chars if cached else 0,
            "seconds": None,                # mp3 in, so no sample count to read
            "saved_as": str(saved) if saved else None,
            "url": f"/audio/{saved.name}" if saved else None,
            "timings": {"total": round(time.monotonic() - t0, 2)}}


def build(pieces, label):
    """Render every piece, join in order, save one clip. Returns a report.

    Each call writes its own file. A fixed name per category would be
    overwritten by a second request arriving while the first is still queued,
    losing one clip and playing the other twice.
    """
    t_start = time.monotonic()
    audio, detail, made, reused = [], [], 0, 0
    for kind, text in pieces:
        t0 = time.monotonic()
        pcm, cached = core.render_pcm(text, dry=DRY)
        took = round(time.monotonic() - t0, 2)
        audio.append(pcm)
        detail.append({"kind": kind, "text": text, "cached": cached,
                       "chars": len(text), "seconds": took})
        if cached:
            reused += len(text)
        else:
            made += len(text)
    t_render = time.monotonic() - t_start
    joined = b"".join(audio)          # same rate and channels, so this is it
    t0 = time.monotonic()
    saved = None
    if joined and label:
        saved = core.save_clip(
            AUDIO_DIR / f"clip_{label}_{uuid.uuid4().hex[:8]}", joined)
        # a stable "most recent" name as well, for anything watching one file
        shutil.copyfile(saved, AUDIO_DIR / f"output_Audio1_{label}{saved.suffix}")
        prune_clips()
        if LOCAL_AUDIO:
            JOBS.put(saved)
    log.info("%s: %d pieces, render %.2fs, total %.2fs", label or "dry",
             len(pieces), t_render, time.monotonic() - t_start)
    return {"pieces": detail, "synthesized_chars": made, "reused_chars": reused,
            "seconds": round(len(joined) / 2 / SAMPLE_RATE, 2),
            "saved_as": str(saved) if saved else None,
            # where the caller can fetch it, for when we do not play it here
            "url": f"/audio/{saved.name}" if saved else None,
            "timings": {"render": round(t_render, 2),
                        "total": round(time.monotonic() - t_start, 2)}}


def prune_clips():
    """Keep the newest KEEP_CLIPS, so per-request files do not pile up."""
    clips = sorted(AUDIO_DIR.glob("clip_*"), key=lambda f: f.stat().st_mtime)
    for old in clips[:-KEEP_CLIPS]:
        old.unlink(missing_ok=True)


def speaker():
    """One clip at a time, so two triggers never talk over each other.

    Every error is caught: without this, one failed clip ends the thread and
    nothing is ever played again, silently.
    """
    while True:
        path = JOBS.get()
        try:
            core.play(path)
        except Exception as e:
            print(f"[speaker] {type(e).__name__}: {e}", flush=True)
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
        report = build(split_phrase(text, values), None if DRY else "play")
    except Exception as e:
        return jsonify(error=readable(e)), 502
    spoken = resolved(text, values)
    print(f"[play] {spoken[:70]}", flush=True)
    return jsonify(text=spoken, **report)


# ---------------------------------------------------------------------------
# 2. it picks from your phrase files, using the live match
# ---------------------------------------------------------------------------
# Categories that have a richer section and a plain fallback. The value named
# first decides which one is used, so a phrase never asks for data we lack.
SECTIONS = {
    # category: the value that decides, the rich section, the plain fallback.
    # smash keys on {player} (the point winner); rally names both sides, so it
    # keys on {player1}.
    "smash": ("player",  "with-players", "no-players"),
    "rally": ("player1", "with-players", "no-players"),
    "match_start": ("player1", "with-players", "no-players"),
}


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


@app.route("/say", methods=["GET", "POST"])
def say():
    """Pick a phrase and speak it. Everything is a query parameter:

        /say?category=smash            which phrase file to pick from
             &player=A|B               which side of the live match (optional)
             &player=Heena             or the name itself, if the feed has none
             &player1=..&player2=..    both sides, for phrases that name two
             &section=with-players     override the smash section (optional)

    Any placeholder a phrase uses can be passed as a query parameter of the
    same name; anything not given falls back to the live match.
    """
    category = (request.values.get("category") or "").strip()
    if not category:
        return jsonify(error="category is required",
                       categories=CATEGORIES), 400
    return say_category(category)


def say_category(category):
    """The work behind /say, shared with the per-category shortcut routes."""
    if category not in CATEGORIES:
        return jsonify(error=f"unknown category {category!r}",
                       categories=CATEGORIES), 404
    s = STATE.snapshot()
    values = core.values_for(s)
    # "player=A" or "player=B" picks a side of the live match; without it,
    # whoever won the last point is used, which is all the feed can tell us
    chosen = (request.values.get("player") or "").strip()
    # which placeholders the caller named, as opposed to inherited from the
    # live match -- the section is chosen on these alone
    supplied = {"player"} if chosen else set()
    if chosen.upper() in ("A", "1"):
        values["player"] = s["player1"]
    elif chosen.upper() in ("B", "2"):
        values["player"] = s["player2"]
    # Any placeholder can also be given literally -- player=Heena,
    # player1=..., winner=..., country1=... -- so a line can be spoken for
    # someone the feed does not know, or before a match is on court.
    for key in list(values):
        given = request.values.get(key)
        if given and not (key == "player" and chosen.upper() in ("A", "1", "B", "2")):
            values[key] = given.strip()
            supplied.add(key)
    # whatever the request did not give, the court map may supply
    for key, value in analytics.current().items():
        if key in values and not values[key] and value:
            values[key] = value
    # and then the live conditions
    for key, value in core.weather_now().items():
        if key in values and not values[key] and value:
            values[key] = value
    section = request.values.get("section")
    if section is None and category in SECTIONS:
        # named lines only when the caller asks for them. A live match still
        # fills the names, but it does not by itself switch the section --
        # otherwise every call would silently become a per-player render.
        key, rich, plain = SECTIONS[category]
        section = rich if (key in supplied and values.get(key)) else plain
    raw = pick_raw(category, section, values)
    if raw is None and category in SECTIONS:
        raw = pick_raw(category, SECTIONS[category][2], values)   # plain fallback
    if raw is None:
        return jsonify(error=f"no usable {category} phrase without player names",
                       hint="the feed has not confirmed names for this court yet",
                       state=s), 409
    RECENT.append(raw)
    del RECENT[:-6]
    try:
        if parse_turns(raw):          # "Rose: ..." -- two voices, rendered whole
            report = build_dialogue(raw, None if DRY else category)
        else:
            report = build(split_phrase(raw, values), None if DRY else category)
    except Exception as e:
        return jsonify(error=readable(e), category=category), 502
    spoken = resolved(raw, values)
    print(f"[{category}] {spoken[:70]}  (new {report['synthesized_chars']}, "
          f"reused {report['reused_chars']})", flush=True)
    return jsonify(category=category, text=spoken, **report)


def _shortcut(category):
    """Build the view for /<category>, e.g. /smash -> say_category("smash")."""
    def view():
        return say_category(category)
    return view


# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------
@app.errorhandler(HTTPException)
def http_error(e):
    """JSON for every HTTP error, since this is an API.

    Covers 405 and the rest, not just 404; Flask's default is an HTML page,
    which callers parsing JSON cannot read.
    """
    return jsonify(error=e.description, status=e.code,
                   **({"help": "GET /"} if e.code == 404 else {})), e.code


@app.errorhandler(Exception)
def unhandled(e):
    """A bug in here is still reported as JSON, with the cause named.

    Without this a missing phrases file (say) returns an HTML 500 and the
    panel can only say "Unexpected token '<'".
    """
    traceback.print_exc()
    return jsonify(error=readable(e), status=500,
                   type=type(e).__name__), 500
@app.get("/")
def index():
    return ("commentary server\n\n"
            "GET /say?category=...         pick a phrase and speak it\n"
            "        &player=A|B           which side of the live match\n"
            "        &player=Heena         or the name itself\n"
            "        &section=...          override the smash section\n"
            "GET /play?text=...&name=...   speak words you supply\n"
            "                              put {name} in text to place the name\n"
            f"GET /<category>               shortcut: {', '.join(CATEGORIES)}\n"
            "GET /state                    current match\n"
            "GET /conditions               outdoor weather being spoken from\n"
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


@app.get("/conditions")
def conditions():
    """The outdoor conditions weather.txt speaks from.

    Named /conditions rather than /weather because /weather is the shortcut
    that speaks a weather line.
    """
    return jsonify(core.weather_now())


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


# Registered one by one rather than as a single "/<category>" rule. A wildcard
# would NOT shadow the static routes -- Werkzeug ranks those higher whatever
# the registration order -- but it would absorb every unmatched path, so
# /favicon.ico and /metrics come back as "unknown category".
# The cost of naming each route is that a category could collide with a route
# that already exists, which Flask accepts silently: GET would reach one view
# and POST the other. Registered last, so url_map is complete, and checked.
_taken = {rule.rule.strip("/") for rule in app.url_map.iter_rules()}
for _c in CATEGORIES:
    if _c in _taken:
        raise RuntimeError(
            f"category {_c!r} collides with the existing /{_c} route; "
            f"rename the phrase file or the route")
    app.add_url_rule(f"/{_c}", f"shortcut_{_c}", _shortcut(_c),
                     methods=["GET", "POST"])


def main():
    global DRY, LOCAL_AUDIO
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to accept calls from other machines")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--court", default=live.COURT, help='court to follow; "" for all')
    ap.add_argument("--key", default=live.TOURNAMENT_KEY)
    ap.add_argument("--dry", action="store_true", help="never call ElevenLabs")
    ap.add_argument("--debug", action="store_true",
                    help="log cache hits and per-piece timings")
    ap.add_argument("--no-local-audio", action="store_true",
                    help="do not play clips here; the caller plays the url")
    ap.add_argument("--city", default="sydney",
                    help="Australian venue: " + ", ".join(core.AU_CITIES))
    ap.add_argument("--lat", type=float, help="venue latitude, overrides --city")
    ap.add_argument("--lon", type=float, help="venue longitude, overrides --city")
    ap.add_argument("--no-weather", action="store_true",
                    help="do not poll for outdoor conditions")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    DRY = args.dry
    LOCAL_AUDIO = not args.no_local_audio
    live.COURT = args.court

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=speaker, daemon=True, name="speaker").start()
    live.start(key=args.key, on_change=STATE.update)
    if not args.no_weather:
        if args.lat is None or args.lon is None:
            if args.city not in core.AU_CITIES:
                sys.exit(f"unknown city {args.city!r}; "
                         f"one of: {', '.join(core.AU_CITIES)}")
            args.lat, args.lon = core.AU_CITIES[args.city]
        core.start_weather(args.lat, args.lon)

    print(f"commentary server on http://{args.host}:{args.port}"
          f"  court={args.court or 'all'}{'  DRY RUN' if DRY else ''}"
          f"{'' if LOCAL_AUDIO else '  no local audio'}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
