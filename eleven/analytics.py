"""Court-map analytics for phrases/analytics.txt.

The analytics system writes a JSON file of court regions as percentages:

    {"r1": 7.69, "r2": 15.39, "r3": 30.77,
     "r4": 7.69, "r5": 15.38, "r6": 23.08}

The six regions are folded into front and back for each side. Values are the
file's own percentages, rounded for speech: 7.69 is read as "8".

    import analytics
    analytics.current()    # {"team1_front_percent": "8", ...}

Empty strings when there is no data, which makes the phrases needing them
unselectable rather than speaking a blank.
"""
import glob
import json
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "configs"
PATTERN = "*region_percentages*.json"

# Which regions make up each part of the court.
#
# ASSUMPTION, and the one thing to confirm with the analytics side: r1-r3 are
# team 1's half running front to back, r4-r6 team 2's. This table is the only
# place it is written down, so correcting it is a four-line edit.
ZONES = {
    "team1_front_percent": ("r1",),
    "team1_back_percent": ("r2", "r3"),
    "team2_front_percent": ("r4",),
    "team2_back_percent": ("r5", "r6"),
}

BLANK = {k: "" for k in ZONES}

def load(path=None):
    """The raw JSON, or {} if it is missing or unreadable."""
    if path is None:
        found = glob.glob(str(CONFIG_DIR / PATTERN))
        # newest by modification time: sorting by name puts match_9 after
        # match_10, which would read yesterday's court map as today's
        path = max(found, key=lambda f: Path(f).stat().st_mtime) if found else None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (TypeError, OSError, json.JSONDecodeError):
        return {}


def number(value):
    """A float, or 0.0 for anything that is not plainly one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def current(path=None):
    """Speech-ready per-side percentages for analytics.txt.

    Never raises: this is called on every request for every category, so a
    half-written or malformed court map has to mean no analytics, not no
    commentary at all.
    """
    data = load(path)
    if not isinstance(data, dict) or not data:
        return dict(BLANK)

    raw = {k: sum(number(data.get(r)) for r in regions)
           for k, regions in ZONES.items()}
    if not any(raw.values()):                   # already folded upstream?
        raw = {k: number(data.get(k)) for k in ZONES}
    if sum(raw.values()) <= 0:
        return dict(BLANK)
    # the file's own percentages, rounded to whole numbers for speech, so
    # 7.69 is read as "8 percent"
    return {k: str(round(v)) for k, v in raw.items()}


if __name__ == "__main__":
    print("raw    :", load())
    print("current:", current())
