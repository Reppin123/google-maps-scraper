# /// script
# requires-python = ">=3.9"
# dependencies = ["requests==2.32.3"]
# ///
"""
gmaps.py — HTTP-only Google Maps business scraper.

No headless browser, no API key, no login/cookies. Hits the same internal
endpoint the Google Maps web frontend calls when you type a search
(google.com/search?tbm=map), which returns a JSON-ish payload (protobuf-over-
JSON, prefixed with the anti-hijacking string ")]}'"). This is the exact
technique Apify's paid "compass/crawler-google-places" actor and every major
open-source Playwright-based scraper (gosom, noworneverev) are built on top
of — except they wrap it in a full headless browser, which is 10-50x slower
and needs a proxy pool to survive. Going straight to the endpoint with plain
requests is faster, cheaper, and has no browser/proxy infra to babysit.

Field mapping below was empirically reverse-engineered (not copied from a
blog) by capturing a real response and indexing every field by hand — see
notes inline. If Google changes the response shape this will need re-mapping;
that's the one real fragility of this approach (documented in README.md
under "If this breaks").

WHAT YOU GET per business: name, full address, lat/lng, category list,
phone, website, Google rating, review count, place_id, CID, opening hours,
a thumbnail photo URL, and a Maps deep link.

PAGINATION: Google returns up to 20 results per request. Paginate by
incrementing the `8i` offset segment of the `pb` parameter in steps of 20.
Text search (not geocoded lat/lng) does the real location matching — the
lat/lng in `pb` only needs to be roughly in the right country/state to bias
results, so no separate geocoding step is required.

USAGE
  # one query, paginated up to --max results (default 120, Google's soft cap)
  python gmaps.py search "electricians in Fitzroy VIC" --max 120 --out data/electricians_fitzroy.json

  # batch: many queries from a text file (one query per line), paced between calls
  python gmaps.py batch data/queries.txt --out-dir data/batch_run --max 60

  # batch from a JSON list of {profile, suburb, state} — builds "<profile> in <suburb> <state>" queries
  python gmaps.py batch-profiles data/targets.json --out-dir data/batch_run --max 60
"""
import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

import requests

UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

# Rough country-level centroid — text query drives the actual location match,
# this just needs to be "in the right hemisphere" to bias Google's ranking.
AU_CENTER = (-25.2744, 133.7751)

PAGE_SIZE = 20


def build_pb(lat: float, lng: float, offset: int) -> str:
    return (
        f"!4m12!1m3!1d10000!2d{lng}!3d{lat}!2m3!1f0!2f0!3f0"
        f"!3m2!1i1024!2i768!4f13.1!7i20!8i{offset}!10b1"
        f"!12m4!1m3!1e2!2b1!3e2!14b1"
    )


def fetch_page(query: str, offset: int, lat: float, lng: float, session: requests.Session) -> str:
    params = {"tbm": "map", "q": query, "pb": build_pb(lat, lng, offset)}
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept-Language": "en-AU,en;q=0.9",
    }
    resp = session.get("https://www.google.com/search", params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_page(raw: str) -> list:
    """Parse one page of the tbm=map response into a list of business dicts.

    Field indices below (into each business's inner array `b`) were mapped by
    hand against a captured real response:
      b[11]  name
      b[39]  full formatted address (street, suburb, state, postcode, country)
      b[9]   [_, _, lat, lng]
      b[13]  category list (first = primary category)
      b[7]   [website_url, domain, ...] or None
      b[4]   [_, _, _, [review_search_url, "N reviews", ...], _, _, _, rating_float, review_count_int]
      b[78]  place_id (ChIJ... — usable in Google Places URLs / API)
      b[10]  CID hex string (alt place identifier, usable in maps URL)
      b[178] [[phone_display, [[local,1],[intl,2]], _, phone_e164, _, [tel_link,...]]] or None
      b[157] thumbnail photo URL
      b[203] structured opening-hours data (raw, left as-is — see README)
    """
    if not raw.strip():
        return []
    body = raw[raw.index("\n") + 1:] if "\n" in raw else raw  # strip )]}' prefix
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    try:
        entries = data[0][1]
    except (IndexError, TypeError, KeyError):
        return []

    out = []
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 15:
            continue
        b = entry[14]
        if not isinstance(b, list) or len(b) < 40:
            continue
        name = b[11] if len(b) > 11 else None
        if not name:
            continue

        address = b[39] if len(b) > 39 and isinstance(b[39], str) else None
        lat = lng = None
        if len(b) > 9 and isinstance(b[9], list) and len(b[9]) >= 4:
            lat, lng = b[9][2], b[9][3]
        categories = b[13] if len(b) > 13 and isinstance(b[13], list) else []
        website = None
        if len(b) > 7 and isinstance(b[7], list) and b[7]:
            website = b[7][0]
        rating = review_count = None
        if len(b) > 4 and isinstance(b[4], list) and len(b[4]) >= 9:
            rating = b[4][7]
            review_count = b[4][8]
        place_id = b[78] if len(b) > 78 else None
        cid = b[10] if len(b) > 10 and isinstance(b[10], str) else None
        phone = None
        if len(b) > 178 and isinstance(b[178], list) and b[178]:
            phone_entry = b[178][0]
            if isinstance(phone_entry, list) and phone_entry:
                phone = phone_entry[3] if len(phone_entry) > 3 and phone_entry[3] else phone_entry[0]
        photo = b[157] if len(b) > 157 and isinstance(b[157], str) else None

        maps_url = None
        if place_id:
            maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"

        out.append({
            "name": name,
            "address": address,
            "lat": lat,
            "lng": lng,
            "categories": categories,
            "primary_category": categories[0] if categories else None,
            "website": website,
            "phone": phone,
            "rating": rating,
            "review_count": review_count,
            "place_id": place_id,
            "cid": cid,
            "maps_url": maps_url,
            "photo": photo,
        })
    return out


def search(query: str, max_results: int = 120, pace: tuple = (1.5, 3.0), verbose: bool = True) -> list:
    lat, lng = AU_CENTER
    session = requests.Session()
    seen_ids = set()
    all_results = []
    offset = 0
    while len(all_results) < max_results:
        raw = fetch_page(query, offset, lat, lng, session)
        page = parse_page(raw)
        if not page:
            break
        new = 0
        for biz in page:
            key = biz["place_id"] or biz["name"]
            if key in seen_ids:
                continue
            seen_ids.add(key)
            all_results.append(biz)
            new += 1
        if verbose:
            print(f"  [{query}] offset={offset} -> {len(page)} results, {new} new (total {len(all_results)})")
        if new == 0 or len(page) < PAGE_SIZE:
            break  # no more fresh results / hit Google's soft cap
        offset += PAGE_SIZE
        time.sleep(random.uniform(*pace))
    return all_results[:max_results]


def cmd_search(args):
    results = search(args.query, max_results=args.max)
    out_path = Path(args.out) if args.out else Path("data") / (re.sub(r"[^a-zA-Z0-9]+", "_", args.query).strip("_") + ".json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(results, open(out_path, "w"), indent=2)
    print(f"Saved {len(results)} businesses -> {out_path}")


def cmd_batch(args):
    queries = [l.strip() for l in open(args.queries_file) if l.strip()]
    _run_batch(queries, args.out_dir, args.max, args.pace)


def cmd_batch_profiles(args):
    targets = json.load(open(args.targets_file))
    queries = [f"{t['profile']} in {t['suburb']} {t['state']}" for t in targets]
    _run_batch(queries, args.out_dir, args.max, args.pace, targets=targets)


def _run_batch(queries, out_dir, max_results, pace, targets=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged = []
    for i, query in enumerate(queries):
        print(f"[{i+1}/{len(queries)}] {query}")
        try:
            results = search(query, max_results=max_results, pace=(pace, pace + 1.5))
        except requests.RequestException as e:
            print(f"  ERROR: {e} — skipping")
            continue
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", query).strip("_").lower()
        json.dump(results, open(out_dir / f"{slug}.json", "w"), indent=2)
        for r in results:
            r["_query"] = query
            if targets:
                r["_profile"] = targets[i].get("profile")
                r["_tier"] = targets[i].get("tier")
        merged.extend(results)
        if i < len(queries) - 1:
            time.sleep(random.uniform(pace, pace + 2))
    json.dump(merged, open(out_dir / "_merged.json", "w"), indent=2)
    print(f"\nBatch done: {len(merged)} total businesses across {len(queries)} queries -> {out_dir / '_merged.json'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("search", help="Run one query, paginate, save JSON.")
    s1.add_argument("query", help='e.g. "electricians in Fitzroy VIC"')
    s1.add_argument("--max", type=int, default=120)
    s1.add_argument("--out", default=None)
    s1.set_defaults(func=cmd_search)

    s2 = sub.add_parser("batch", help="Run many queries from a text file (one per line).")
    s2.add_argument("queries_file")
    s2.add_argument("--out-dir", default="data/batch_run")
    s2.add_argument("--max", type=int, default=60)
    s2.add_argument("--pace", type=float, default=1.5)
    s2.set_defaults(func=cmd_batch)

    s3 = sub.add_parser("batch-profiles", help='Run from JSON [{"profile","suburb","state","tier"}].')
    s3.add_argument("targets_file")
    s3.add_argument("--out-dir", default="data/batch_run")
    s3.add_argument("--max", type=int, default=60)
    s3.add_argument("--pace", type=float, default=1.5)
    s3.set_defaults(func=cmd_batch_profiles)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
