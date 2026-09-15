# /// script
# requires-python = ">=3.9"
# dependencies = ["requests==2.32.3"]
# ///
"""
au_grid.py — population-weighted geographic grid search across Australia.

WHY: gmaps.py's `search()` for a single query point caps at Google's soft
limit of ~120 results per viewport, no matter how the query text is
phrased. To get thousands-to-tens-of-thousands of results per ICP profile,
you don't ask harder — you ask from more places. This module tiles
Australia into hundreds of overlapping map viewports, weighted toward
where the population (and therefore the businesses) actually is, and
fires the SAME bare keyword (no suburb text) at each one. Google's local
pack scopes results to the viewport you're centered on — confirmed
empirically: "electricians" from a Sydney CBD point vs a Melbourne CBD
point returns ZERO overlapping place_ids.

TIERS OF COVERAGE
  metro   — the 5 state capitals with real metro population (Sydney,
            Melbourne, Brisbane, Perth, Adelaide). Dense square-lattice
            grid, ~5-7km spacing, since these hold the bulk of AU small
            businesses and a single point badly undercounts them.
  mid     — ~20 significant urban areas (Gold Coast, Newcastle, Canberra,
            Sunshine Coast, Wollongong, Geelong, Hobart, etc.) — a light
            2x2 or single-point grid depending on size.
  regional — ~35 smaller regional centers, one point each with a wider
            search radius, covering the rest of populated Australia
            without wasting requests on empty outback.

Dedup across all of this is handled downstream by pipeline.py's
place_id-keyed upsert — overlapping grid cells are expected and fine.

USAGE
  python au_grid.py "electricians" --out-dir data/grid_electricians --max-per-point 120
  python au_grid.py "electricians" --tier metro --out-dir data/grid_test  # just the 5 metros, for a quick pass
"""
import argparse
import json
import math
import random
import re
import sys
import time
from pathlib import Path

import requests

from gmaps import search as gmaps_search

KM_PER_DEG_LAT = 111.0

# Australia's real bounding box (mainland + Tasmania + near islands). Google's
# tbm=map endpoint occasionally ignores the pb geo-bias entirely and serves an
# unlocalized fallback batch instead — empirically observed as a cluster of
# genuine Chandigarh/Mohali/Punjab (India) businesses bleeding into a Sydney-
# centered grid run on 2026-09-15. Any result whose OWN lat/lng (not the
# request's) falls outside this box is definitely not an AU business — drop it.
AU_BBOX = {"lat_min": -44.0, "lat_max": -10.0, "lng_min": 112.0, "lng_max": 154.0}


def in_au_bbox(lat, lng) -> bool:
    if lat is None or lng is None:
        return False
    return AU_BBOX["lat_min"] <= lat <= AU_BBOX["lat_max"] and AU_BBOX["lng_min"] <= lng <= AU_BBOX["lng_max"]


def km_to_deg(lat_deg: float, dx_km: float, dy_km: float):
    """Convert an (east, north) km offset at a given latitude to (dlng, dlat) degrees."""
    dlat = dy_km / KM_PER_DEG_LAT
    dlng = dx_km / (KM_PER_DEG_LAT * math.cos(math.radians(lat_deg)))
    return dlng, dlat


def square_grid(center_lat, center_lng, half_width_km, spacing_km, label):
    """Points on a square lattice covering a circle of radius half_width_km."""
    pts = []
    n = int(half_width_km // spacing_km)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            dx_km = i * spacing_km
            dy_km = j * spacing_km
            if math.hypot(dx_km, dy_km) > half_width_km:
                continue
            dlng, dlat = km_to_deg(center_lat, dx_km, dy_km)
            pts.append({"lat": center_lat + dlat, "lng": center_lng + dlng, "label": label})
    return pts


# 5 state capitals — real metro population centers, dense grid
METROS = [
    # label, lat, lng, half_width_km, spacing_km
    ("Sydney NSW", -33.8688, 151.2093, 35, 6),
    ("Melbourne VIC", -37.8136, 144.9631, 35, 6),
    ("Brisbane QLD", -27.4698, 153.0251, 28, 6),
    ("Perth WA", -31.9523, 115.8613, 28, 6),
    ("Adelaide SA", -34.9285, 138.6007, 20, 6),
]

# Significant urban areas — light grid (2x2-ish via smaller half-width/spacing)
MID_CITIES = [
    ("Gold Coast QLD", -28.0167, 153.4000, 15, 7),
    ("Newcastle NSW", -32.9283, 151.7817, 12, 7),
    ("Canberra ACT", -35.2809, 149.1300, 14, 7),
    ("Sunshine Coast QLD", -26.6500, 153.0667, 14, 7),
    ("Central Coast NSW", -33.4269, 151.3428, 12, 7),
    ("Wollongong NSW", -34.4278, 150.8931, 10, 7),
    ("Geelong VIC", -38.1499, 144.3617, 10, 7),
    ("Hobart TAS", -42.8821, 147.3272, 10, 7),
]

# Regional centers — one point each, wider radius, covers the rest of populated AU
REGIONAL = [
    ("Townsville QLD", -19.2590, 146.8169),
    ("Cairns QLD", -16.9186, 145.7781),
    ("Darwin NT", -12.4634, 130.8456),
    ("Toowoomba QLD", -27.5598, 151.9507),
    ("Ballarat VIC", -37.5622, 143.8503),
    ("Bendigo VIC", -36.7570, 144.2794),
    ("Albury Wodonga NSW", -36.0737, 146.9135),
    ("Launceston TAS", -41.4332, 147.1441),
    ("Mackay QLD", -21.1550, 149.1868),
    ("Rockhampton QLD", -23.3791, 150.5100),
    ("Bunbury WA", -33.3271, 115.6414),
    ("Bundaberg QLD", -24.8661, 152.3489),
    ("Coffs Harbour NSW", -30.2963, 153.1157),
    ("Wagga Wagga NSW", -35.1082, 147.3598),
    ("Hervey Bay QLD", -25.2882, 152.8535),
    ("Mildura VIC", -34.2080, 142.1246),
    ("Shepparton VIC", -36.3833, 145.4000),
    ("Port Macquarie NSW", -31.4333, 152.9000),
    ("Tamworth NSW", -31.0833, 150.9167),
    ("Orange NSW", -33.2833, 149.1000),
    ("Dubbo NSW", -32.2500, 148.6167),
    ("Bathurst NSW", -33.4192, 149.5778),
    ("Nowra NSW", -34.8833, 150.6000),
    ("Warrnambool VIC", -38.3833, 142.4833),
    ("Mount Gambier SA", -37.8286, 140.7828),
    ("Alice Springs NT", -23.6980, 133.8807),
    ("Broome WA", -17.9614, 122.2359),
    ("Kalgoorlie WA", -30.7489, 121.4658),
    ("Whyalla SA", -33.0333, 137.5833),
    ("Devonport TAS", -41.1795, 146.3499),
    ("Frankston VIC", -38.1418, 145.1233),
    ("Ipswich QLD", -27.6171, 152.7594),
    ("Geraldton WA", -28.7774, 114.6150),
    ("Warragul VIC", -38.1667, 145.9333),
    ("Ballina NSW", -28.8667, 153.5667),
]


def generate_points(tiers=("metro", "mid", "regional")):
    pts = []
    if "metro" in tiers:
        for label, lat, lng, hw, sp in METROS:
            pts.extend(square_grid(lat, lng, hw, sp, label))
    if "mid" in tiers:
        for label, lat, lng, hw, sp in MID_CITIES:
            pts.extend(square_grid(lat, lng, hw, sp, label))
    if "regional" in tiers:
        for label, lat, lng in REGIONAL:
            pts.append({"lat": lat, "lng": lng, "label": label})
    return pts


def run_grid(keyword, out_dir, max_per_point=120, pace=(1.2, 2.5), tiers=("metro", "mid", "regional"),
             profile=None, tier_num=None):
    points = generate_points(tiers)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    merged = []
    seen_ids = set()
    t0 = time.time()
    print(f"Grid: {len(points)} points across {tiers} for '{keyword}'")
    for i, pt in enumerate(points):
        try:
            results = gmaps_search(keyword, max_results=max_per_point, pace=pace, verbose=False,
                                     center=(pt["lat"], pt["lng"]), session=session)
        except requests.RequestException as e:
            print(f"  [{i+1}/{len(points)}] {pt['label']}: ERROR {e} — skipping")
            time.sleep(3)
            continue
        new = 0
        dropped_offshore = 0
        for r in results:
            if not in_au_bbox(r.get("lat"), r.get("lng")):
                dropped_offshore += 1
                continue
            key = r["place_id"] or r["name"]
            r["_query"] = keyword
            r["_grid_point"] = pt["label"]
            if profile:
                r["_profile"] = profile
            if tier_num:
                r["_tier"] = tier_num
            if key in seen_ids:
                continue
            seen_ids.add(key)
            merged.append(r)
            new += 1
        elapsed = time.time() - t0
        flag = f", {dropped_offshore} offshore DROPPED" if dropped_offshore else ""
        print(f"  [{i+1}/{len(points)}] {pt['label']}: {len(results)} results, {new} new (total {len(merged)}, {elapsed/60:.1f}m elapsed){flag}")
        if (i + 1) % 25 == 0:
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", keyword).strip("_").lower()
            json.dump(merged, open(out_dir / f"{slug}_partial.json", "w"), indent=2)
        time.sleep(random.uniform(*pace))
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", keyword).strip("_").lower()
    out_path = out_dir / f"{slug}_full.json"
    json.dump(merged, open(out_path, "w"), indent=2)
    print(f"\nDone: {len(merged)} unique businesses across {len(points)} grid points -> {out_path}")
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keyword", help='e.g. "electricians"')
    ap.add_argument("--out-dir", default="data/grid_run")
    ap.add_argument("--max-per-point", type=int, default=120)
    ap.add_argument("--pace", type=float, default=1.2)
    ap.add_argument("--tier", choices=["metro", "mid", "regional", "all"], default="all")
    ap.add_argument("--profile", default=None, help="ICP profile name to tag results with (_profile field)")
    ap.add_argument("--tier-num", type=int, default=None, help="ICP tier number to tag results with (_tier field)")
    args = ap.parse_args()
    tiers = ("metro", "mid", "regional") if args.tier == "all" else (args.tier,)
    run_grid(args.keyword, args.out_dir, max_per_point=args.max_per_point,
             pace=(args.pace, args.pace + 1.3), tiers=tiers, profile=args.profile, tier_num=args.tier_num)


if __name__ == "__main__":
    main()
