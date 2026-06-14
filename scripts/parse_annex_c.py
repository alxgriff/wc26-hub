#!/usr/bin/env python3
"""Generate data/annex_c.csv — the 2026 World Cup R32 third-place assignment table.

Annex C of the FIFA WC2026 Competition Regulations maps each of the C(12,8)=495
possible SETS of eight groups (those whose third-placed team qualified) to which
group's third fills each of the eight "group winner vs best third" R32 matches.
We vendor it from the public Wikipedia template (independent of the FIFA PDF),
parse it, integrity-check it, and emit a machine-readable CSV. One-off dev tool;
the CSV is committed static data (the table never changes).

CSV columns: `combo` (the 8 sorted group letters, e.g. EFGHIJKL) + the third's
GROUP letter for each winner slot 1A,1B,1D,1E,1G,1I,1K,1L — i.e. R32 matches
79,85,81,74,82,77,87,80 respectively.

Source: https://en.wikipedia.org/wiki/Template:2026_FIFA_World_Cup_third-place_table
"""
import csv
import re
import sys
import urllib.request
from pathlib import Path

SRC = "https://en.wikipedia.org/wiki/Template:2026_FIFA_World_Cup_third-place_table?action=raw"
SLOTS = ("1A", "1B", "1D", "1E", "1G", "1I", "1K", "1L")   # header column order
OUT = Path(__file__).resolve().parents[1] / "data" / "annex_c.csv"


def parse(wikitext: str) -> list[tuple[int, list[str], list[str]]]:
    """One (row_no, combo_groups, assigned_thirds) per numbered row."""
    parts = re.split(r'!\s*scope="row"\s*\|\s*(\d+)', wikitext)
    rows = []
    for i in range(1, len(parts), 2):
        num, body = int(parts[i]), parts[i + 1]
        combo = re.findall(r"'''([A-L])'''", body)        # bolded = that group's third advanced
        thirds = re.findall(r"\b3([A-L])\b", body)         # the 8 assignments, in column order
        rows.append((num, combo, thirds))
    return rows


def main(argv: list | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:                                               # optional local file (offline)
        wt = Path(argv[0]).read_text(encoding="utf-8")
    else:
        req = urllib.request.Request(SRC, headers={"User-Agent": "wc26-hub/1.0 (annex-C table)"})
        wt = urllib.request.urlopen(req, timeout=40).read().decode("utf-8")
    rows = parse(wt)
    if len(rows) != 495:
        raise SystemExit(f"error: expected 495 rows, parsed {len(rows)}")
    seen = set()
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["combo", *SLOTS])
        for num, combo, thirds in rows:
            if len(combo) != 8 or len(thirds) != 8:
                raise SystemExit(f"error: row {num}: |combo|={len(combo)} |thirds|={len(thirds)}")
            if sorted(thirds) != sorted(combo):
                raise SystemExit(f"error: row {num}: thirds {thirds} not a permutation of combo {combo}")
            combo_s = "".join(sorted(combo))
            if combo_s in seen:
                raise SystemExit(f"error: duplicate combo {combo_s}")
            seen.add(combo_s)
            w.writerow([combo_s, *thirds])
    print(f"wrote {OUT.relative_to(OUT.parents[1])} ({len(rows)} rows, all integrity checks passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
