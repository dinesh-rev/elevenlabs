"""Keep the audio cache warm, so a live trigger never waits on a render.

Every phrase piece that contains no player name is the same for every match,
so it is worth rendering once and keeping. This walks the phrase files, works
out which pieces are missing, and renders them cheapest category first within
a credit budget.

    python3 automation.py                  # show what is missing, spend nothing
    python3 automation.py --budget 3000    # render up to 3000 characters
    python3 automation.py --category score # one category only

Nothing is ever deleted. A cached piece costs a few hundred kilobytes and is
reused for as long as the phrase or the player exists, and a name piece cannot
be told from an abandoned one -- the filename is only a hash of the text.

Nothing here changes the server. It reads the same phrase files and writes to
the same cache, so warming can run while the server is up or down.
"""
import argparse
import time
from collections import defaultdict

import core
import server  # for readable() on a failed render


def missing(categories=None):
    """The pieces not yet on disk, grouped by category."""
    by_category = defaultdict(list)
    for text, category in server.phrase_pieces(categories).items():
        if not core.piece_path(text).exists():
            by_category[category].append(text)
    return by_category


def cost(texts):
    """Characters, which is what eleven_v3 bills."""
    return sum(len(t) for t in texts)


def report(by_category):
    total = 0
    print(f"  {'category':16} {'pieces':>6} {'credits':>8}")
    for category in sorted(by_category, key=lambda c: cost(by_category[c])):
        texts = by_category[category]
        total += cost(texts)
        print(f"  {category:16} {len(texts):6} {cost(texts):8}")
    print(f"  {'TOTAL':16} {'':6} {total:8}")
    return total


def warm(by_category, budget, pause=0.0):
    """Render missing pieces, cheapest category first, within `budget`.

    Cheapest first so a small budget finishes whole categories rather than
    leaving several half done.
    """
    spent = done = 0
    for category in sorted(by_category, key=lambda c: cost(by_category[c])):
        for text in sorted(by_category[category], key=len):
            if spent + len(text) > budget:
                continue      # too big for what is left; a smaller one may fit
            try:
                core.render_pcm(text)
            except Exception as e:
                print(f"  stopped: {server.readable(e)}")
                return spent, done, False
            spent += len(text)
            done += 1
            print(f"  [{category}] {len(text):4} chars  {text[:46]!r}")
            if pause:
                time.sleep(pause)
    return spent, done, True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget", type=int, default=0,
                    help="characters to render; 0 only reports")
    ap.add_argument("--category", action="append",
                    help="limit to a category; repeatable")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds between renders, to stay polite")
    args = ap.parse_args()

    for category in args.category or []:
        if category not in core.CATEGORIES:
            raise SystemExit(f"unknown category {category!r}; "
                             f"one of: {', '.join(core.CATEGORIES)}")

    by_category = missing(args.category)
    if not by_category:
        print("  nothing missing; the cache is warm")
        return
    total = report(by_category)

    if not args.budget:
        print(f"\n  add --budget N to render. All of it is {total} characters.")
        return
    print()
    spent, done, ok = warm(by_category, args.budget, args.pause)
    print(f"\n  rendered {done} pieces, {spent} characters")
    if not ok:
        # so a scheduled run that never warmed anything is visible
        raise SystemExit(1)


if __name__ == "__main__":
    main()
