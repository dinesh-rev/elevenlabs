"""Court-map analytics for phrases/analytics.txt.

The analytics system writes a landing map per match: each side's activity
split across three zones, running front to back.

    {"match_id": "5", "mode": "singles",
     "players": {"Ravee":  {"zones": {"R1": 42.8, "R2": 28.6, "R3": 28.6}},
                 "Arshad": {"zones": {"R1": 25.0, "R2": 41.7, "R3": 33.3}}}}

Doubles reports "teams" instead of "players"; either way the two sides come
back as team1 and team2, which is what the phrases ask for. Values are
strings rounded for speech -- 42.8 is read as "43" -- because they are read
aloud, and empty when there is no data so the phrase is skipped rather than
spoken with a hole in it.

    import analytics
    analytics.current()    # {"team1_front_percent": "43", ...}
"""
import glob
import json
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "configs"
PATTERN = "*region_percentages*.json"

# Which zones count as front and which as back.
#
# ASSUMPTION worth confirming with the analytics side: R1 is the forecourt and
# R2/R3 are deeper. These two lines are the only place it is written down.
FRONT = ("R1",)
BACK = ("R2", "R3")

BLANK = {f"team{n}_{half}_percent": ""
         for n in (1, 2) for half in ("front", "back")}
BLANK.update(team1="", team2="")      # the names the file itself carries
BLANK.update(team1_zone="", team1_percent="",
             team2_zone="", team2_percent="")


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


def pick_mode(data, mode=None):
    """The singles or doubles block, whichever shape the file has.

    A live file is one match, with "mode" at the top. The sample file holds
    both under "singles" and "doubles" keys.
    """
    if not isinstance(data, dict):
        return {}
    if "mode" in data:
        return data
    block = data.get(mode or "singles")
    return block if isinstance(block, dict) else {}


def mode(path=None):
    """"singles", "doubles", or "" if the file does not say."""
    data = load(path)
    if isinstance(data, dict) and data.get("mode") in ("singles", "doubles"):
        return data["mode"]
    return ""


def number(value):
    """A float, or 0.0 for anything that is not plainly one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# Doubles names its sides, so they are looked up rather than taken in order --
# the same two keys zonal_player.py uses, so {team1_front_percent} and
# {team1_top_zone} cannot end up describing opposite sides of the same line.
DOUBLES_SIDES = ("team_near", "team_far")


def sides(block):
    """The two sides as [(name, body)], team1 first.

    Doubles is keyed by team_near / team_far and looked up by name. Singles is
    keyed by the players' own names, so file order is all there is to go on.
    """
    group = block.get("teams")
    if isinstance(group, dict):
        found = [(k, group[k]) for k in DOUBLES_SIDES if k in group]
        return found if len(found) == 2 else []
    group = block.get("players")
    return list(group.items())[:2] if isinstance(group, dict) else []


def names(pair):
    """{"team1": ..., "team2": ...} from the players the file lists, if any.

    Separate from the percentages: the file can name its players even when the
    zones are missing or in a shape this reader does not understand, and a
    name is still worth having.
    """
    out = {}
    for number_, (_, body) in enumerate(pair, 1):
        listed = (body or {}).get("players")
        if isinstance(listed, list) and listed:
            # "and" reads better than a list when spoken
            out[f"team{number_}"] = " and ".join(str(n) for n in listed)
    return out


def strongest(pair):
    """Each side's busiest zone, named and as a percentage.

    The file reports whichever zones it measured, so the zone is taken from
    the file rather than assumed. Underscores become spaces and the name is
    lower-cased, because it is read aloud mid-sentence.
    """
    out = {}
    for number_, (_, body) in enumerate(pair, 1):
        zones = (body or {}).get("zones")
        if not isinstance(zones, dict):
            continue
        usable = {name: number(value) for name, value in zones.items()
                  if number(value) > 0}
        if not usable:
            continue
        top = max(usable, key=usable.get)
        out[f"team{number_}_zone"] = top.replace("_", " ").lower()
        out[f"team{number_}_percent"] = str(round(usable[top]))
    return out


def percentages(pair):
    """The front/back split per side, or {} if the zones do not give one."""
    out = {}
    for number_, (_, body) in enumerate(pair, 1):
        zones = (body or {}).get("zones")
        if not isinstance(zones, dict):
            return {}
        front = sum(number(zones.get(z)) for z in FRONT)
        back = sum(number(zones.get(z)) for z in BACK)
        if front + back <= 0:
            return {}               # half an answer is worse than none
        out[f"team{number_}_front_percent"] = str(round(front))
        out[f"team{number_}_back_percent"] = str(round(back))
    return out


def current(path=None, mode=None):
    """Speech-ready values from the court map. Never raises.

    Every request for every category passes through here, so a half-written
    or malformed court map has to mean no analytics, not no commentary.
    """
    pair = sides(pick_mode(load(path), mode))
    if len(pair) < 2:
        return dict(BLANK)
    return {**BLANK, **names(pair), **strongest(pair), **percentages(pair)}


if __name__ == "__main__":
    for mode in ("singles", "doubles"):
        print(f"{mode}: {current(mode=mode)}")
