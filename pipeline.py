# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
pipeline.py — turns raw gmaps.py JSON output into a queryable, deduped,
signal-scored SQLite database. This is the "organize it better" layer on
top of the v1 scraper: one database instead of a folder of JSON files,
one row per real business (deduped on place_id) instead of one row per
query, and a lead-quality score instead of a flat unranked list.

USAGE
  # ingest every *.json in a folder (or a single file) into leads.db
  python pipeline.py ingest data/batch_run --db leads.db

  # ingest one file
  python pipeline.py ingest data/painters_in_Bondi_NSW.json --db leads.db

  # print a quick summary (counts by tier, top signal leads)
  python pipeline.py summary --db leads.db

  # export the current database to a flat JSON (for the dashboard / map)
  python pipeline.py export --db leads.db --out leads_export.json

SCHEMA
  businesses   — one row per unique place_id (or name+address fallback for
                 the rare record with no place_id). Upserted, not replaced —
                 first_seen is preserved across re-runs, last_seen and the
                 live fields (rating/review_count/website/phone) are updated
                 each time so re-running the pipeline tracks change over time.
  pulls_log    — one row per ingest event per business, snapshotting
                 rating/review_count at that moment. Lets you see review-
                 count growth (an engagement signal) across repeated pulls.

SIGNAL SCORE (0-6, higher = better outreach target per the Halvard brief)
  +2  no website at all (or Facebook-only) — strongest legacy-lock-in tell
  +1  has reviews but rating page shows a decent volume (>= 10) — real,
      active, established business rather than a shell listing
  +1  business has a phone number captured (reachable without web research)
  +1  tier is 1 or 2 (closest fit to the brief's own archetype)
  +1  review_count >= 50 (established, likely busy enough to be dropping
      quotes/follow-ups — the exact pain this product solves)
"""
import argparse
import json
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    place_id        TEXT PRIMARY KEY,
    name            TEXT,
    address         TEXT,
    suburb          TEXT,
    state           TEXT,
    lat             REAL,
    lng             REAL,
    primary_category TEXT,
    categories      TEXT,
    phone           TEXT,
    website          TEXT,
    rating          REAL,
    review_count    INTEGER,
    maps_url        TEXT,
    photo           TEXT,
    tier            INTEGER,
    profile         TEXT,
    source_query    TEXT,
    has_website     INTEGER,
    signal_score    INTEGER,
    outreach_status TEXT DEFAULT 'new',
    first_seen      TEXT,
    last_seen       TEXT
);
CREATE TABLE IF NOT EXISTS pulls_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id     TEXT,
    pulled_at    TEXT,
    rating       REAL,
    review_count INTEGER,
    source_query TEXT
);
CREATE INDEX IF NOT EXISTS idx_biz_tier ON businesses(tier);
CREATE INDEX IF NOT EXISTS idx_biz_signal ON businesses(signal_score DESC);
"""

STATE_ABBRS = ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]

# Google's tbm=map endpoint occasionally ignores the request's geo-bias and
# serves an unlocalized fallback batch (empirically: a cluster of genuine
# Chandigarh/India businesses bleeding into a Sydney grid run, 2026-09-15).
# Defensive belt-and-braces guard here too, even though au_grid.py already
# filters at collection time — never let an offshore row reach the DB.
AU_BBOX = {"lat_min": -44.0, "lat_max": -10.0, "lng_min": 112.0, "lng_max": 154.0}


def in_au_bbox(lat, lng) -> bool:
    if lat is None or lng is None:
        return False
    try:
        return AU_BBOX["lat_min"] <= float(lat) <= AU_BBOX["lat_max"] and AU_BBOX["lng_min"] <= float(lng) <= AU_BBOX["lng_max"]
    except (TypeError, ValueError):
        return False


def parse_address(address: str, source_query: str = ""):
    """Best-effort suburb/state split from the full formatted address."""
    if not address:
        # fall back to parsing "<profile> in <suburb> <state>" from the query
        m = re.search(r"\bin\s+([A-Za-z\s]+?)\s+(" + "|".join(STATE_ABBRS) + r")\b", source_query or "")
        if m:
            return m.group(1).strip(), m.group(2)
        return None, None
    for state in STATE_ABBRS:
        m = re.search(rf"\b([A-Za-z' ]+)\s+{state}\s*\d{{4}}\b", address)
        if m:
            return m.group(1).strip(), state
    return None, None


def score(row: dict) -> int:
    s = 0
    if not row.get("website"):
        s += 2
    if row.get("phone"):
        s += 1
    if (row.get("review_count") or 0) >= 10:
        s += 1
    if (row.get("review_count") or 0) >= 50:
        s += 1
    if row.get("tier") in (1, 2):
        s += 1
    return s


def ingest_file(conn, path: Path):
    rows = json.load(open(path))
    if isinstance(rows, dict):
        rows = [rows]
    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    n_new = n_updated = n_offshore = 0
    for r in rows:
        if not in_au_bbox(r.get("lat"), r.get("lng")):
            n_offshore += 1
            continue
        place_id = r.get("place_id") or f"noid:{r.get('name')}|{r.get('address')}"
        source_query = r.get("_query") or path.stem.replace("_", " ")
        suburb, state = parse_address(r.get("address"), source_query)
        has_website = 1 if r.get("website") else 0
        row = dict(r, tier=r.get("_tier"), profile=r.get("_profile"))
        sig = score(row)

        cur = conn.execute("SELECT first_seen FROM businesses WHERE place_id=?", (place_id,))
        existing = cur.fetchone()
        if existing:
            conn.execute("""
                UPDATE businesses SET
                    name=?, address=?, suburb=?, state=?, lat=?, lng=?,
                    primary_category=?, categories=?, phone=?, website=?,
                    rating=?, review_count=?, maps_url=?, photo=?,
                    tier=COALESCE(?, tier), profile=COALESCE(?, profile),
                    source_query=?, has_website=?, signal_score=?, last_seen=?
                WHERE place_id=?
            """, (
                r.get("name"), r.get("address"), suburb, state, r.get("lat"), r.get("lng"),
                r.get("primary_category"), json.dumps(r.get("categories") or []),
                r.get("phone"), r.get("website"), r.get("rating"), r.get("review_count"),
                r.get("maps_url"), r.get("photo"), r.get("_tier"), r.get("_profile"),
                source_query, has_website, sig, now, place_id,
            ))
            n_updated += 1
        else:
            conn.execute("""
                INSERT INTO businesses (place_id, name, address, suburb, state, lat, lng,
                    primary_category, categories, phone, website, rating, review_count,
                    maps_url, photo, tier, profile, source_query, has_website, signal_score,
                    first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                place_id, r.get("name"), r.get("address"), suburb, state, r.get("lat"), r.get("lng"),
                r.get("primary_category"), json.dumps(r.get("categories") or []),
                r.get("phone"), r.get("website"), r.get("rating"), r.get("review_count"),
                r.get("maps_url"), r.get("photo"), r.get("_tier"), r.get("_profile"),
                source_query, has_website, sig, now, now,
            ))
            n_new += 1
        conn.execute(
            "INSERT INTO pulls_log (place_id, pulled_at, rating, review_count, source_query) VALUES (?,?,?,?,?)",
            (place_id, now, r.get("rating"), r.get("review_count"), source_query),
        )
    conn.commit()
    return n_new, n_updated, n_offshore


def cmd_ingest(args):
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    target = Path(args.path)
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    files = [f for f in files if f.name != "_merged.json" or len(files) == 1]
    total_new = total_upd = total_off = 0
    for f in files:
        n_new, n_upd, n_off = ingest_file(conn, f)
        off_note = f", {n_off} offshore dropped" if n_off else ""
        print(f"  {f.name}: {n_new} new, {n_upd} updated{off_note}")
        total_new += n_new
        total_upd += n_upd
        total_off += n_off
    off_note = f", {total_off} offshore rows dropped" if total_off else ""
    print(f"\nDone: {total_new} new businesses, {total_upd} updated{off_note}, across {len(files)} file(s) -> {args.db}")


def cmd_summary(args):
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"]
    print(f"Total businesses in {args.db}: {total}\n")
    print("By tier:")
    for r in conn.execute("SELECT tier, COUNT(*) c FROM businesses GROUP BY tier ORDER BY tier"):
        print(f"  Tier {r['tier']}: {r['c']}")
    print("\nTop 15 by signal score:")
    for r in conn.execute("""
        SELECT name, suburb, state, signal_score, review_count, has_website, profile
        FROM businesses ORDER BY signal_score DESC, review_count DESC LIMIT 15
    """):
        web = "no website" if not r["has_website"] else "has website"
        print(f"  [{r['signal_score']}] {r['name']} — {r['suburb']}, {r['state']} — {r['review_count']} reviews, {web} ({r['profile']})")


def cmd_export(args):
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM businesses ORDER BY signal_score DESC")]
    json.dump(rows, open(args.out, "w"), indent=2)
    print(f"Exported {len(rows)} businesses -> {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("ingest", help="Ingest JSON file(s)/folder into the SQLite DB.")
    s1.add_argument("path")
    s1.add_argument("--db", default="leads.db")
    s1.set_defaults(func=cmd_ingest)

    s2 = sub.add_parser("summary", help="Print counts + top leads.")
    s2.add_argument("--db", default="leads.db")
    s2.set_defaults(func=cmd_summary)

    s3 = sub.add_parser("export", help="Export DB to flat JSON.")
    s3.add_argument("--db", default="leads.db")
    s3.add_argument("--out", default="leads_export.json")
    s3.set_defaults(func=cmd_export)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
