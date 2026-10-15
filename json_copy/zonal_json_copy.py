import json
import os
import sys
from pathlib import Path

# Relative to this file, so the script works wherever the project is checked
# out rather than only on the machine the paths were written on.
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = BASE_DIR / "singles_combined_zones_orbit.json"
TARGET = BASE_DIR.parent / "eleven" / "configs" / "zonal_player.json"
VALID_MODES = ("singles", "doubles")


def copy_to_zonal_player(source_path, target_path=TARGET):
    with open(source_path) as f:
        source = json.load(f)

    # Source is either a single section ({"mode": "singles", ...})
    # or a combined file ({"singles": {...}, "doubles": {...}})
    if "mode" in source:
        sections = {source["mode"]: source}
    else:
        sections = {key: value for key, value in source.items() if key in VALID_MODES}

    if not sections or any(mode not in VALID_MODES for mode in sections):
        raise ValueError(f"No valid mode found in {source_path}, expected one of {VALID_MODES}")

    target = {}
    if Path(target_path).exists():
        try:
            with open(target_path) as f:
                target = json.load(f)
        except (ValueError, OSError):
            # Unreadable, so there is nothing to merge with. Start fresh
            # rather than failing on every copy from now on: the reader
            # already ignores a corrupt file, so nothing usable is lost.
            target = {}

    # Replace only the sections present in the source; other modes are left untouched
    target.update(sections)

    # eleven/configs/ is gitignored, so on a fresh clone it does not exist yet
    Path(target_path).parent.mkdir(parents=True, exist_ok=True)

    # Written to a temp file and moved into place, so a reader never catches
    # the file half-written and a crash mid-write cannot corrupt it. os.replace
    # is atomic within a filesystem, which is why the temp file sits alongside.
    target_path = Path(target_path)
    tmp = target_path.with_suffix(".json.part")
    with open(tmp, "w") as f:
        json.dump(target, f, indent=2)
        f.write("\n")
    os.replace(tmp, target_path)

    return list(sections)


if __name__ == "__main__":
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    modes = copy_to_zonal_player(source_path)
    print(f"Updated {', '.join(modes)} in {TARGET} from {source_path.name}")
