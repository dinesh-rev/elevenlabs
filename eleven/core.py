"""Shared pieces for the commentary servers.

live_commentary.py and split_commentary.py both need the same things: the
phrase files, the match state fed by live.py, an ElevenLabs client, a way to
play a clip, and the JSON plumbing for the HTTP handler. They live here so
there is one copy to fix rather than two.

eleven.py deliberately does NOT use this module. It is a top-to-bottom script
that does its work at import time, so importing it would spend credits as a
side effect -- and keeping it self-contained means it still runs on its own.
"""
import array
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
import os
import re
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path

log = logging.getLogger("commentary")

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "configs" / "audio"

VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"   # George, the commentary voice
MODEL_ID = "eleven_v3"

# Two-voice dialogues in convo.txt name their speaker: "Rose: [curious] ...".
# Anyone not listed here gets the default commentary voice.
VOICES = {"rose": "zxPaDs5RuZh7fQDkY6mP"}


def voice_for(speaker):
    return VOICES.get(speaker.strip().lower(), VOICE_ID)

CATEGORIES = ["match_start", "rally", "smash", "highlights", "winners",
              "convo", "weather", "voting", "analytics", "score"]

# Audio is rendered as raw PCM rather than mp3 because PCM at one sample rate
# joins by plain byte concatenation -- no ffmpeg, no re-encode, and none of the
# small silences mp3 frame padding leaves at every join.
PIECES_DIR = AUDIO_DIR / "pieces"
SAMPLE_RATE = 24000
PCM_FORMAT = f"pcm_{SAMPLE_RATE}"      # 16-bit signed mono, little endian

PLACEHOLDER = re.compile(r"\{(\w+)\}")


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
def load_env_file(path=None):
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    path = path or BASE_DIR / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# phrases
# ---------------------------------------------------------------------------
def load_phrases(category, section=None):
    """Blocks from phrases/<category>.txt.

    One phrase per block, blank line between blocks, "#" lines are labels and
    are ignored. A "## <name>" line starts a section; pass section= to load
    only that part, e.g. smash.txt has "no-players" and "with-players".
    """
    text = (BASE_DIR / "phrases" / f"{category}.txt").read_text(encoding="utf-8")
    blocks, current = [], None
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


# ---------------------------------------------------------------------------
# match state, fed by live.py from its own thread
# ---------------------------------------------------------------------------
BLANK = {
    "court": "", "court_name": "", "match_id": "",
    "player1": "", "player2": "", "country1": "", "country2": "",
    "score1": 0, "score2": 0, "games1": 0, "games2": 0, "set_no": 0,
    "status": "", "completed": False, "winner": "", "confirmed": False,
    "swapped": False, "last_scorer": "",
    "team1": [], "team2": [],      # the sides split into individual players
}


# Everything that names a player. Held back until the feed confirms it, and
# cleared when the match changes. team1/team2 belong here too: values_for
# prefers them, so leaving them out let an unconfirmed misread be spoken
# through {team1} while {player1} was correctly withheld.
NAME_KEYS = ("player1", "player2", "country1", "country2", "team1", "team2")


class MatchState:
    """The match currently on court. update() is the live.py callback."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = dict(BLANK)

    def update(self, record):
        """Copy one court record in. Runs on the websocket thread."""
        with self._lock:
            d = self._data
            if record["match_id"] != d["match_id"]:
                # New match on this court. The previous match's names would
                # otherwise linger while the feed settles on the new ones,
                # pairing old players with the new score.
                for k in NAME_KEYS + ("winner",):
                    d[k] = [] if k in ("team1", "team2") else ""
                d["last_scorer"] = ""
                d["score1"] = d["score2"] = 0
            elif (record["score1"] == d["score1"] + 1
                    and record["score2"] == d["score2"]):
                d["last_scorer"] = "1"   # only +1 is a real point; bigger jumps
            elif (record["score2"] == d["score2"] + 1
                    and record["score1"] == d["score1"]):
                d["last_scorer"] = "2"   # and decreases are the feed correcting

            for k, v in record.items():
                if k in NAME_KEYS and not record["confirmed"]:
                    continue   # an unconfirmed name must never be spoken
                d[k] = v

    def snapshot(self):
        """A consistent copy, so a phrase is never built from two frames."""
        with self._lock:
            return dict(self._data)


def values_for(s):
    """What each {placeholder} means for the match in snapshot `s`."""
    if s["winner"]:
        winner = s["winner"]
    elif (s["games1"], s["score1"]) >= (s["games2"], s["score2"]):
        winner = s["player1"]
    else:
        winner = s["player2"]
    return {
        # whoever won the most recent point. The feed never says who played
        # the shot, so this is the closest it can get.
        "player": s["player2"] if s["last_scorer"] == "2" else s["player1"],
        "winner": winner,
        "opponent": s["player2"] if winner == s["player1"] else s["player1"],
        # the two sides by position, which match_start.txt and rally.txt use to
        # name both players in one line
        "player1": s["player1"],
        "player2": s["player2"],
        # as strings: the score is legitimately 0 at the start of a game, and
        # a bare 0 would read as "missing" to pick_raw
        "score1": str(s["score1"]),
        "score2": str(s["score2"]),
        # the same two sides under the names players.json uses. Separate keys,
        # so player1/player2 are untouched and existing phrases are unaffected.
        "team1": " and ".join(s.get("team1") or []) or s["player1"],
        "team2": " and ".join(s.get("team2") or []) or s["player2"],
        "country1": s["country1"],
        "country2": s["country2"],
        "country": s["country1"],
        "court": s["court"],
        "court_no": s["court"],     # match_start.txt spells it this way
        # weather.txt: the feed knows nothing about these, so they arrive as
        # query parameters -- /say?category=weather&temperature=28C&condition=sunny
        "temperature": "",
        "condition": "",
        "wind": "",
        # court map, from analytics.py
        "team1_front_percent": "",
        "team1_back_percent": "",
        "team2_front_percent": "",
        "team2_back_percent": "",
        "high": "",          # today's maximum
        "low": "",           # today's minimum
        "rain_chance": "",
        "outlook": "",       # tomorrow's conditions
    }


# ---------------------------------------------------------------------------
# outdoor conditions, for weather.txt
#
# The match feed knows nothing about weather, so this polls Open-Meteo, which
# needs no API key. Values are phrased for speech, not for a dashboard --
# "22 degrees" rather than "22.0C", because they are read aloud.
# ---------------------------------------------------------------------------
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# WMO codes, worded to drop into a sentence: "It is 22 degrees and <condition>."
WEATHER_CODES = {
    0: "clear and sunny", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "drizzling lightly", 53: "drizzling", 55: "drizzling heavily",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "raining lightly", 63: "raining", 65: "raining heavily",
    66: "freezing rain", 67: "freezing rain",
    71: "snowing lightly", 73: "snowing", 75: "snowing heavily", 77: "snowing",
    80: "showery", 81: "showery", 82: "heavy showers",
    85: "snow showers", 86: "snow showers",
    95: "thundery", 96: "thundery with hail", 99: "thundery with hail",
}

# Australian venues, so the location can be given by name rather than degrees.
AU_CITIES = {
    "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "brisbane": (-27.47, 153.03), "perth": (-31.95, 115.86),
    "adelaide": (-34.93, 138.60), "canberra": (-35.28, 149.13),
    "gold-coast": (-28.02, 153.40), "newcastle": (-32.93, 151.78),
    "wollongong": (-34.42, 150.89), "geelong": (-38.15, 144.36),
    "hobart": (-42.88, 147.33), "darwin": (-12.46, 130.84),
    "cairns": (-16.92, 145.77), "townsville": (-19.26, 146.82),
    "ballarat": (-37.56, 143.85), "bendigo": (-36.76, 144.28),
}

_weather_lock = threading.Lock()
_weather = {"temperature": "", "condition": "", "wind": "", "humidity": "",
            "high": "", "low": "", "rain_chance": "", "outlook": "",
            "place": "", "updated_at": ""}


def fetch_weather(lat, lon, timeout=10):
    """One reading, phrased for speech. Raises on network or parse failure."""
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
        # BOM's ACCESS model returns nulls through this API, so the default
        # blend is used -- it already includes BOM data over Australia.
        "daily": "temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,weather_code",
    })
    with urllib.request.urlopen(f"{WEATHER_URL}?{query}", timeout=timeout) as r:
        data = json.load(r)
    now = data.get("current") or {}
    day = data.get("daily") or {}

    def first(key, index=0):
        values = day.get(key) or []
        return values[index] if len(values) > index else None

    temp, wind = now.get("temperature_2m"), now.get("wind_speed_10m")
    humid = now.get("relative_humidity_2m")
    high, low = first("temperature_2m_max"), first("temperature_2m_min")
    rain, tomorrow = first("precipitation_probability_max"), first("weather_code", 1)
    return {
        "temperature": f"{round(temp)} degrees" if temp is not None else "",
        "condition": WEATHER_CODES.get(now.get("weather_code"), ""),
        "wind": f"{round(wind)} kilometres an hour" if wind is not None else "",
        "humidity": f"{humid} percent" if humid is not None else "",
        "high": f"{round(high)} degrees" if high is not None else "",
        "low": f"{round(low)} degrees" if low is not None else "",
        "rain_chance": f"{rain} percent" if rain is not None else "",
        "outlook": WEATHER_CODES.get(tomorrow, ""),      # tomorrow
        "place": data.get("timezone", ""),
        "updated_at": now.get("time", ""),
    }


def weather_now():
    """The most recent reading. Empty strings until the first fetch lands."""
    with _weather_lock:
        return dict(_weather)


def start_weather(lat, lon, minutes=15):
    """Refresh in the background. Conditions move slowly; 15 minutes is plenty."""
    def loop():
        while True:
            try:
                reading = fetch_weather(lat, lon)
                with _weather_lock:
                    _weather.update(reading)
                log.info("weather: %s, %s, wind %s (%s)", reading["temperature"],
                         reading["condition"], reading["wind"], reading["place"])
            except Exception as e:
                # a missed reading is not worth failing over; the old one stands
                log.warning("weather fetch failed (%s: %s)", type(e).__name__, e)
            time.sleep(minutes * 60)

    t = threading.Thread(target=loop, daemon=True, name="weather")
    t.start()
    return t


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------
_client = None


def client():
    """One client for the process, so the TLS connection is reused."""
    global _client
    if _client is None:
        from elevenlabs import ElevenLabs
        key = os.getenv("ELEVENLABS_API_KEY")
        if not key:
            raise RuntimeError("ELEVENLABS_API_KEY not set (put it in eleven/.env)")
        _client = ElevenLabs(api_key=key)
    return _client


def render_dialogue(turns, dry=False):
    """(mp3_bytes, was_cached) for a two-voice dialogue.

    Dialogue is generated as one unit with the voices interleaved, so unlike
    commentary it cannot be split into reusable pieces -- it caches whole-block
    only, keyed on the speakers as well as the words.
    """
    from elevenlabs.types import DialogueInput

    key = "|".join(f"{v}:{t}" for v, t in turns)
    digest = hashlib.sha1(f"{MODEL_ID}|{key}".encode()).hexdigest()
    path = PIECES_DIR / f"dlg_{digest}.mp3"
    if path.exists():
        log.debug("cache hit  dialogue %d turns", len(turns))
        return path.read_bytes(), True
    if dry:
        return b"", False
    t0 = time.monotonic()
    audio = client().text_to_dialogue.convert(
        inputs=[DialogueInput(text=t, voice_id=v) for v, t in turns],
        model_id=MODEL_ID, output_format="mp3_44100_128")
    data = b"".join(audio)              # buffer first: it can fail mid-stream
    PIECES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".mp3.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    log.info("dialogue %d turns, %d chars in %.2fs", len(turns),
             sum(len(t) for _, t in turns), time.monotonic() - t0)
    return data, False


def voice_settings():
    from elevenlabs.types import VoiceSettings
    return VoiceSettings(stability=0.0, similarity_boost=0.75,
                         style=1.0, use_speaker_boost=True)


# ---------------------------------------------------------------------------
# rendering and joining audio pieces
# ---------------------------------------------------------------------------
def piece_path(text):
    """Where the audio for this exact text lives. Same text, same file."""
    digest = hashlib.sha1(
        f"{VOICE_ID}|{MODEL_ID}|{SAMPLE_RATE}|{text}".encode()).hexdigest()[:16]
    return PIECES_DIR / f"{digest}.pcm"


def render_pcm(text, dry=False):
    """(pcm_bytes, was_cached). Only calls the API when not already on disk."""
    path = piece_path(text)
    if path.exists():
        log.debug("cache hit  %4d chars  %r", len(text), text[:40])
        return path.read_bytes(), True
    if dry:
        return b"", False
    t0 = time.monotonic()
    audio = client().text_to_speech.convert(
        text=text, voice_id=VOICE_ID, model_id=MODEL_ID,
        output_format=PCM_FORMAT, voice_settings=voice_settings())
    raw = b"".join(audio)                 # buffer first: it can fail mid-stream
    api = time.monotonic() - t0
    pcm = trim_silence(raw)
    PIECES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pcm.part")
    tmp.write_bytes(pcm)
    os.replace(tmp, path)
    secs = len(pcm) / 2 / SAMPLE_RATE
    # chars/s and the ratio of wall time to audio produced are the two numbers
    # that say whether the model or the text length is the problem
    log.info("render %4d chars -> %5.2fs audio in %5.2fs  (%.0f chars/s, %.2fx)",
             len(text), secs, api, len(text) / api if api else 0,
             api / secs if secs else 0)
    return pcm, False


def trim_silence(pcm, threshold=600, keep_ms=25):
    """Cut the padding ElevenLabs leaves at each end, or joins sound gappy."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    start, end = 0, len(samples)
    while start < end and abs(samples[start]) < threshold:
        start += 1
    while end > start and abs(samples[end - 1]) < threshold:
        end -= 1
    keep = SAMPLE_RATE * keep_ms // 1000
    return samples[max(0, start - keep):min(len(samples), end + keep)].tobytes()


def write_wav(path, pcm):
    """Wrap joined PCM in a wav header, atomically."""
    tmp = path.with_suffix(".wav.part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)      # 16-bit
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    os.replace(tmp, path)


def write_mp3(path, pcm):
    """Encode joined PCM to mp3 with ffmpeg. False if ffmpeg is not installed.

    Encoding happens once, after the join -- never before, because mp3 frame
    padding is exactly what makes joined mp3s click at the seams.
    """
    if not shutil.which("ffmpeg"):
        return False
    tmp = path.with_suffix(".mp3.part")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
             "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", str(tmp)],
            input=pcm, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, path)
    return True


def save_clip(stem, pcm):
    """Write joined PCM next to `stem`, as mp3 if ffmpeg is here, else wav.

    Returns the path actually written, so callers play whichever they got.
    """
    mp3 = stem.with_suffix(".mp3")
    if write_mp3(mp3, pcm):
        return mp3
    wav = stem.with_suffix(".wav")
    write_wav(wav, pcm)
    return wav


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------
# ffplay is preferred everywhere: it blocks until the clip ends, which is what
# keeps two triggers from overlapping. vlc is the Windows spelling of cvlc.
PLAYER_CMD = next(
    (c for c in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
                 ["mpv", "--no-video", "--really-quiet"],
                 ["aplay", "-q"],
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
        os.startfile(str(path))
    else:
        print("[no audio player found: install ffmpeg]", file=sys.stderr, flush=True)


load_env_file()
