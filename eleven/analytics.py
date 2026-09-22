"""Court-map analytics for phrases/analytics.txt.

The analytics system writes a JSON file of court regions as percentages:

    {"r1": 7.69, "r2": 15.39, "r3": 30.77,
     "r4": 7.69, "r5": 15.38, "r6": 23.08}

The six regions are folded into front and back for each side. Values come back
as strings phrased for speech -- "54", not 53.85 -- because they are read aloud.

    import analytics
    analytics.current()    # {"team1_front_percent": "14", ...}

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
# place it is written down, so correcting it is a four-line edit -- and having
# it backwards would put a confidently wrong read on air.
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
        found = sorted(glob.glob(str(CONFIG_DIR / PATTERN)))
        path = found[-1] if found else None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (TypeError, OSError, json.JSONDecodeError):
        return {}


def current(path=None):
    """Speech-ready per-side percentages for analytics.txt."""
    data = load(path)
    if not data:
        return dict(BLANK)

    raw = {k: sum(float(data.get(r, 0)) for r in regions)
           for k, regions in ZONES.items()}
    if not any(raw.values()):                   # already folded upstream?
        raw = {k: float(data.get(k, 0)) for k in ZONES}

    out = {}
    for team in ("team1", "team2"):
        front = raw[f"{team}_front_percent"]
        back = raw[f"{team}_back_percent"]
        total = front + back
        if total <= 0:
            return dict(BLANK)
        # out of that side's own activity, so each pair adds up to 100 and
        # reads correctly however the file is scaled
        out[f"{team}_front_percent"] = str(round(front / total * 100))
        out[f"{team}_back_percent"] = str(round(back / total * 100))
    return out


if __name__ == "__main__":
    print("raw    :", load())
    print("current:", current())
