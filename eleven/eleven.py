from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
from elevenlabs.core.api_error import ApiError
import os
import random
import re
import sys
import time
from pathlib import Path

# Anchor everything to this file's folder so the script works from any cwd.
BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

# Hardcoded key - note this file is tracked by git, unlike .env.
API_KEY = "sk_085bf22fe1e67e0c5d223a3289e40463fe4e61d6bf9fdcdf"

elevenlabs = ElevenLabs(
  api_key=os.getenv("ELEVENLABS_API_KEY") or API_KEY,
)

# Phrases live in phrases/<category>.txt - one phrase per block, blank line
# between blocks, "#" lines are labels and are ignored. A "## <name>" line
# starts a section; pass section= to load only that part of the file, e.g.
# smash.txt has "no-players" and "with-players".
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


# ---------------------------------------------------------------------------
# Live match variables. These start empty and are refilled by live.py every
# time the websocket reports a change on the court we are following.
# ---------------------------------------------------------------------------
COURT = "0"        # court to follow; "" follows whichever court reports first
PLAYER1 = ""
PLAYER2 = ""
COUNTRY1 = ""
COUNTRY2 = ""
SCORE1 = 0
SCORE2 = 0
ROUND = ""
STATUS = ""

LIVE = True        # False = ignore the socket and use generic phrases


def update(record):
    """Copy one court record from the socket into the variables above."""
    global PLAYER1, PLAYER2, COUNTRY1, COUNTRY2, SCORE1, SCORE2, ROUND, STATUS
    if COURT and record["court"] and record["court"] != COURT:
        return  # another court on the same feed
    PLAYER1, COUNTRY1 = record["player1"], record["country1"]
    PLAYER2, COUNTRY2 = record["player2"], record["country2"]
    SCORE1, SCORE2 = record["score1"], record["score2"]
    ROUND, STATUS = record["round"], record["status"]


def connect(timeout=30):
    """Start the listener and wait for the first match. True if names arrived."""
    import live
    live.start(on_change=update)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if PLAYER1 and PLAYER2:
            return True
        time.sleep(0.5)
    return False


def fill(text):
    """Substitute {player}, {winner}, {opponent}, {country}, {court}."""
    # whoever is ahead is the winner; at match end that is the actual result
    winner, opponent = ((PLAYER1, PLAYER2) if SCORE1 >= SCORE2
                        else (PLAYER2, PLAYER1))
    for key, value in {"player": PLAYER1, "winner": winner, "opponent": opponent,
                       "country": COUNTRY1, "court": COURT}.items():
        if value:
            text = text.replace("{%s}" % key, str(value))
    # "Lee C.W." + "." reads as a stumble; leave deliberate "..." pauses alone
    return re.sub(r"\.\.(?!\.)", ".", text)


def pick(category, section=None):
    """A phrase with every placeholder filled, or None if names are missing."""
    blocks = load_phrases(category, section)
    random.shuffle(blocks)
    for block in blocks:
        filled = fill(block)
        if not re.search(r"\{\w+\}", filled):
            return filled
    return None


CATEGORY = "winners"  # match_start, rally, smash, highlights, winners, convo
SECTION = None  # for smash: "no-players" or "with-players"

if LIVE:
    if connect():
        print(f"court {COURT}: {PLAYER1} ({COUNTRY1}) {SCORE1}"
              f" - {SCORE2} {PLAYER2} ({COUNTRY2}) [{ROUND} {STATUS}]")
    else:
        print("no live match data; falling back to generic phrases",
              file=sys.stderr)

if CATEGORY == "smash" and SECTION is None:
    SECTION = "with-players" if PLAYER1 else "no-players"

text = pick(CATEGORY, SECTION)
if text is None:
    # every phrase in this category needs a name we do not have
    text = pick(CATEGORY, "no-players") if CATEGORY == "smash" else None
if text is None:
    print(f"No usable {CATEGORY} phrase without player names.", file=sys.stderr)
    sys.exit(1)

print(f"[{CATEGORY}] {text}")

output_path = BASE_DIR / f"output_{CATEGORY}.mp3"

try:
    audio = elevenlabs.text_to_speech.convert(
    text=text,

    voice_id="JBFqnCBsd6RMkjVDRZzb",  # George
    model_id="eleven_v3",
    output_format="mp3_44100_128",

    voice_settings=VoiceSettings(
        stability=0.0,
        similarity_boost=0.75,
        style=1.0,
        use_speaker_boost=True,
    ),
)
    # Buffer the stream first: the API can fail mid-response, and we do not
    # want to have already truncated a good file on disk.
    data = b"".join(audio)
except ApiError as e:
    detail = (e.body or {}).get("detail", {}) if isinstance(e.body, dict) else {}
    code = detail.get("code", "api_error")
    msg = detail.get("message", str(e.body))
    print(f"ElevenLabs request failed [{code}]: {msg}", file=sys.stderr)
    if code == "quota_exceeded":
        print(
            f"Text is {len(text)} characters; eleven_v3 bills ~1 credit per "
            f"character. Shorten the text or top up at "
            f"elevenlabs.io/app/subscription.",
            file=sys.stderr,
        )
    print(f"{output_path} left unchanged.", file=sys.stderr)
    sys.exit(1)

tmp_path = output_path.with_suffix(".mp3.part")
with open(tmp_path, "wb") as f:
    f.write(data)
os.replace(tmp_path, output_path)

print(f"Saved {output_path} ({len(data)} bytes)")
