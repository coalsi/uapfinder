"""Regenerate ``data/bright_stars.csv.gz`` from the HYG database (developer tool).

    curl -LO https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v40.csv.gz
    python -m skynet_edge.starcal.build_catalog hygdata_v40.csv.gz

HYG v4.0 is licensed CC BY-SA 4.0 (see data/NOTICE.md); the output is an
adapted subset and carries the same licence. Columns written: Hipparcos
number, HR (Yale Bright Star) number, J2000 ICRS RA/Dec (deg, epoch 2000.0),
proper motion (mas/yr; pmra is mu_alpha*cos(dec)) and V magnitude.
"""

from __future__ import annotations

import csv
import gzip
import os
import sys

MAG_LIMIT = 6.5
HEADER = ("# Bright stars V <= 6.5 from the HYG database v4.0 (astronexus.com/hyg), CC BY-SA 4.0.\n"
          "# Adapted (subset, reformatted) by UAP Finder; see NOTICE.md. Epoch/equinox J2000.0 (ICRS).\n"
          "# hip,hr,ra_deg,dec_deg,pmra_masyr,pmdec_masyr,vmag\n")


def build(src: str, dst: str) -> int:
    rows = []
    with gzip.open(src, "rt", newline="") as f:
        for r in csv.DictReader(f):
            if r["id"] == "0":  # the Sun
                continue
            try:
                mag = float(r["mag"])
            except ValueError:
                continue
            if mag > MAG_LIMIT:
                continue
            pmra = float(r["pmra"] or 0.0)
            pmdec = float(r["pmdec"] or 0.0)
            if abs(pmra) > 9000 or abs(pmdec) > 9000:  # HYG placeholder values
                pmra = pmdec = 0.0
            rows.append((r["hip"] or "0", r["hr"] or "0", float(r["ra"]) * 15.0, float(r["dec"]), pmra, pmdec, mag))
    rows.sort(key=lambda x: x[6])
    with gzip.GzipFile(dst, "wb", mtime=0) as gz:
        out = HEADER + "".join(
            f"{h},{hr},{ra:.6f},{dec:.6f},{pmra:.2f},{pmdec:.2f},{mag:.2f}\n" for h, hr, ra, dec, pmra, pmdec, mag in rows)
        gz.write(out.encode("ascii"))
    return len(rows)


if __name__ == "__main__":
    dst = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bright_stars.csv.gz")
    print(build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else dst), "stars written")
