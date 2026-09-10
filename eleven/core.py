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
import os
import re
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "configs" / "audio"

VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"   # George
MODEL_ID = "eleven_v3"

CATEGORIES = ["match_start", "rally", "smash", "highlights", "winners", "convo"]

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
}


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
                for k in ("player1", "player2", "country1", "country2", "winner"):
                    d[k] = ""
                d["last_scorer"] = ""
                d["score1"] = d["score2"] = 0
            elif (record["score1"] == d["score1"] + 1
                    and record["score2"] == d["score2"]):
                d["last_scorer"] = "1"   # only +1 is a real point; bigger jumps
            elif (record["score2"] == d["score2"] + 1
                    and record["score1"] == d["score1"]):
                d["last_scorer"] = "2"   # and decreases are the feed correcting

            for k, v in record.items():
                if k in ("player1", "player2", "country1", "country2") \
                        and not record["confirmed"]:
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
        "country": s["country1"],
        "court": s["court"],
    }


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
        return path.read_bytes(), True
    if dry:
        return b"", False
    audio = client().text_to_speech.convert(
        text=text, voice_id=VOICE_ID, model_id=MODEL_ID,
        output_format=PCM_FORMAT, voice_settings=voice_settings())
    pcm = trim_silence(b"".join(audio))   # buffer first: it can fail mid-stream
    PIECES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pcm.part")
    tmp.write_bytes(pcm)
    os.replace(tmp, path)
    print(f"[render] {len(text):4} chars  {text[:50]!r}", flush=True)
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
