import json
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
        with open(target_path) as f:
            target = json.load(f)

    # Replace only the sections present in the source; other modes are left untouched
    target.update(sections)

    with open(target_path, "w") as f:
        json.dump(target, f, indent=2)
        f.write("\n")

    return list(sections)


if __name__ == "__main__":
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    modes = copy_to_zonal_player(source_path)
    print(f"Updated {', '.join(modes)} in {TARGET} from {source_path.name}")
