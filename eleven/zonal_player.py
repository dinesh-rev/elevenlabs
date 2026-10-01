"""Court-zone percentages for commentary, from configs/zonal_player.json.

The analytics system reports how much of each side's activity fell in six
named zones. For speech the useful facts are the extremes -- where a player
spent most of their time and where they spent least -- so this picks the
highest and lowest zone for each side.

    import zonal_player
    zonal_player.current()
    # {"team1_top_zone": "midcourt left", "team1_top_percent": "41", ...}

Singles reports players, doubles reports teams; either way the two sides come
back as team1 and team2, matching the placeholders the phrases already use.
Values are strings phrased for speech, and empty when there is no data, so a
phrase that needs them is skipped rather than spoken with a blank.
"""
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "configs" / "zonal_player.json"

# the two sides, in the order the feed calls player1 and player2
SIDES = {"singles": ("player_0", "player_1"),
         "doubles": ("team_near", "team_far")}

KEYS = ("top_zone", "top_percent", "low_zone", "low_percent")
BLANK = {f"team{n}_{k}": "" for n in (1, 2) for k in KEYS}


def load(path=None):
    """The raw JSON, or {} if it is missing or unreadable."""
    try:
        return json.loads(Path(path or CONFIG).read_text(encoding="utf-8"))
    except (TypeError, OSError, json.JSONDecodeError):
        return {}


def pick_mode(data, mode=None):
    """The singles or doubles block, whichever shape the file has.

    A live file is one match, with "mode" at the top. The sample file holds
    both under "singles" and "doubles" keys.
    """
    if not isinstance(data, dict):
        return {}                   # a list, a number: not a court map
    if "mode" in data:
        return data
    block = data.get(mode or "singles")
    return block if isinstance(block, dict) else {}


def extremes(zones):
    """(name, percent) of the busiest and quietest zone, phrased for speech."""
    usable = {name: value for name, value in zones.items()
              if isinstance(value, (int, float))}
    if not usable:
        return None
    top = max(usable, key=usable.get)
    low = min(usable, key=usable.get)
    # lower case so the name drops into a sentence: "...in the midcourt left"
    return {"top_zone": top.lower(), "top_percent": str(round(usable[top])),
            "low_zone": low.lower(), "low_percent": str(round(usable[low]))}


def mode(path=None):
    """"singles", "doubles", or "" if the file does not say."""
    data = load(path)
    if isinstance(data, dict) and data.get("mode") in SIDES:
        return data["mode"]
    return ""


def current(path=None, mode=None):
    """Speech-ready zone extremes for both sides. Never raises."""
    block = pick_mode(load(path), mode)
    sides = block.get("players") or block.get("teams") or {}
    if not isinstance(sides, dict):
        return dict(BLANK)
    names = SIDES.get(block.get("mode"), ())
    if not names or not sides:
        return dict(BLANK)

    out = dict(BLANK)
    for number, side in enumerate(names, 1):
        zones = (sides.get(side) or {}).get("zones") or {}
        found = extremes(zones)
        if not found:
            return dict(BLANK)      # half an answer is worse than none
        for key, value in found.items():
            out[f"team{number}_{key}"] = value
    return out


if __name__ == "__main__":
    for mode in ("singles", "doubles"):
        print(f"{mode}:")
        for key, value in current(mode=mode).items():
            print(f"  {key:20} {value}")
