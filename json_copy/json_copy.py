import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
DEFAULT_SOURCE = BASE_DIR / "singles_combined_zones_orbit.json"
TARGET = BASE_DIR / "zonal_player.json"
VALID_MODES = ("singles", "doubles")


def copy_to_zonal_player(source_path, target_path=TARGET):
    with open(source_path) as f:
        source = json.load(f)

    mode = source.get("mode")
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode {mode!r} in {source_path}, expected one of {VALID_MODES}")

    target = {}
    if Path(target_path).exists():
        with open(target_path) as f:
            target = json.load(f)

    # Replace only the section matching the source's mode; the other mode is left untouched
    target[mode] = source

    with open(target_path, "w") as f:
        json.dump(target, f, indent=2)
        f.write("\n")

    return mode


if __name__ == "__main__":
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    mode = copy_to_zonal_player(source_path)
    print(f"Updated '{mode}' in {TARGET.name} from {source_path.name}")
