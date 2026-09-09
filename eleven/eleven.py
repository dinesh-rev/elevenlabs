from elevenlabs import ElevenLabs
from elevenlabs.types import VoiceSettings
from elevenlabs.core.api_error import ApiError
import os
import random
import re
import sys
import threading
import time
from pathlib import Path

# Anchor everything to this file's folder so the script works from any cwd.
BASE_DIR = Path(__file__).resolve().parent

def load_env_file(path):
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


load_env_file(BASE_DIR / ".env")

# The key lives in .env (gitignored), never in this file: it is tracked by git
# and pushed to a public remote.
API_KEY = os.getenv("ELEVENLABS_API_KEY")
if not API_KEY:
    print("ELEVENLABS_API_KEY not set. Put it in eleven/.env as:\n"
          "    ELEVENLABS_API_KEY=sk_...", file=sys.stderr)
    sys.exit(1)

elevenlabs = ElevenLabs(api_key=API_KEY)

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
# The feed carries every court in the tournament; live.py drops the rest.
# "" would latch onto whichever court reported first.
COURT = "1"
COURT_NAME = ""
MATCH_ID = ""
PLAYER1 = ""        # doubles pairs arrive joined: "Low H Y and Ng E C"
PLAYER2 = ""
COUNTRY1 = ""       # this feed carries no country field; see notes below
COUNTRY2 = ""
SCORE1 = 0          # points in the current game
SCORE2 = 0
GAMES1 = 0          # games won so far
GAMES2 = 0
SET_NO = 0
STATUS = ""         # "LIVE", "COMPLETED", ...
COMPLETED = False
WINNER = ""         # filled with a name once the match is decided
CONFIRMED = False   # the feed's own nameState; False = names still settling
SWAPPED = False     # feed's "swapped" flag; see the note in fill()
LAST_SCORER = ""    # "1" or "2": which side won the most recent point

LIVE = True         # False = ignore the socket and use generic phrases

# update() runs on live.py's websocket thread while the main thread reads these
# variables, so both sides take this lock to avoid reading half of one frame
# and half of the next.
STATE_LOCK = threading.Lock()


def update(record):
    """Copy one court record from the socket into the variables above."""
    global COURT, COURT_NAME, MATCH_ID, PLAYER1, PLAYER2, COUNTRY1, COUNTRY2
    global SCORE1, SCORE2, GAMES1, GAMES2, SET_NO, STATUS, COMPLETED
    global WINNER, CONFIRMED, SWAPPED, LAST_SCORER
    if COURT and record["court"] and record["court"] != COURT:
        return                      # another court on the same feed
    with STATE_LOCK:
        COURT = COURT or record["court"]   # latch onto the first court seen
        if record["match_id"] != MATCH_ID:
            # A new match on this court. The previous match's names would
            # otherwise stay until the feed confirms the new ones, pairing old
            # players with the new score.
            PLAYER1 = PLAYER2 = COUNTRY1 = COUNTRY2 = ""
            WINNER = LAST_SCORER = ""
            SCORE1 = SCORE2 = 0
        elif record["score1"] == SCORE1 + 1 and record["score2"] == SCORE2:
            LAST_SCORER = "1"       # only +1 is a point; bigger jumps and any
        elif record["score2"] == SCORE2 + 1 and record["score1"] == SCORE1:
            LAST_SCORER = "2"       # decrease are the feed correcting itself
        COURT_NAME, MATCH_ID = record["court_name"], record["match_id"]
        if record["confirmed"]:
            # scores still track while the feed settles on the names, but an
            # unconfirmed name must never reach the commentary
            PLAYER1, COUNTRY1 = record["player1"], record["country1"]
            PLAYER2, COUNTRY2 = record["player2"], record["country2"]
        SCORE1, SCORE2 = record["score1"], record["score2"]
        GAMES1, GAMES2 = record["games1"], record["games2"]
        SET_NO, STATUS = record["set_no"], record["status"]
        COMPLETED, WINNER = record["completed"], record["winner"]
        CONFIRMED, SWAPPED = record["confirmed"], record["swapped"]


def connect(timeout=30):
    """Start the listener and wait for the first match. True if names arrived."""
    import live
    live.start(on_change=update)
    deadline = time.time() + timeout
    while time.time() < deadline:
        # CONFIRMED is the feed's own signal that it has settled on the names
        if PLAYER1 and PLAYER2 and CONFIRMED:
            return True
        time.sleep(0.5)
    return False


def fill(text):
    """Substitute {player}, {winner}, {opponent}, {country}, {court}."""
    # the feed names the winner once the match is decided; before that, fall
    # back to whoever leads on games, then on points
    if WINNER:
        winner = WINNER
    elif (GAMES1, SCORE1) >= (GAMES2, SCORE2):
        winner = PLAYER1
    else:
        winner = PLAYER2
    opponent = PLAYER2 if winner == PLAYER1 else PLAYER1
    # {player} is whoever won the most recent point, not always side 1. The
    # feed never says who played the shot, so this is the closest it can get.
    # NOTE: if SWAPPED ever flips mid-match, side 1/2 may map to the other
    # player; the frame log will show whether this feed does that.
    player = PLAYER2 if LAST_SCORER == "2" else PLAYER1
    for key, value in {"player": player, "winner": winner, "opponent": opponent,
                       "country": COUNTRY1, "court": COURT}.items():
        if value:
            text = text.replace("{%s}" % key, str(value))
    # a name ending in an initial ("C.W.") plus the phrase's own full stop reads
    # as a stumble; the lookarounds keep deliberate "..." pauses intact
    return re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)


def pick(category, section=None):
    """A phrase with every placeholder filled, or None if names are missing."""
    blocks = load_phrases(category, section)
    random.shuffle(blocks)
    for block in blocks:
        filled = fill(block)
        if not re.search(r"\{\w+\}", filled):
            return filled
    return None


CATEGORY = "smash"  # match_start, rally, smash, highlights, winners, convo
SECTION = None  # for smash: "no-players" or "with-players"

if LIVE:
    if connect():
        print(f"{COURT_NAME or 'court ' + COURT}: {PLAYER1} {SCORE1}"
              f" - {SCORE2} {PLAYER2}"
              f"  (games {GAMES1}-{GAMES2}, set {SET_NO}, {STATUS})")
    else:
        print("no live match data; falling back to generic phrases",
              file=sys.stderr)

# held across the whole selection so the phrase cannot be built from two
# different frames while the socket thread keeps updating
with STATE_LOCK:
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

AUDIO_DIR = BASE_DIR / "configs" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
output_path = AUDIO_DIR / f"output_Audio1_{CATEGORY}.mp3"

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
