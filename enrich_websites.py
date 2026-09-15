# /// script
# requires-python = ">=3.9"
# dependencies = ["requests==2.32.3"]
# ///
"""
enrich_websites.py — website-crawl enrichment pass for businesses already in leads.db.

WHY: Google Maps has no email field. Every real-world tool (scrap.io, Outscraper,
the open-source omkarcloud/gosom scrapers) does the same underlying trick: take the
website URL off the Maps listing, crawl it, regex out an email / social link / ABN.
That's what this does — no magic, just the crawl layer we were missing.

EFFICIENCY: unlike the Google Maps grid scraper (rate-limited against one shared
endpoint), each business here has its OWN domain, so there's no shared rate limit
to respect — we crawl dozens of sites concurrently via a thread pool. I/O-bound
work (waiting on network), so threads are the right tool, no need for asyncio.

DATA POINTS PULLED per business (all from plain regex over page text/HTML,
no headless browser needed):
  - email          first plausible business email found (filtered against junk:
                    image/font files misparsed as emails, wixpress/sentry noise,
                    literal placeholder domains like example.com)
  - social_facebook / social_instagram / social_linkedin  first matching profile link
  - abn            Australian Business Number if printed on the site (common in
                    AU business site footers), digits only, validated to 11 digits
  - enrich_status  'ok' (email found), 'partial' (site loaded, no email but got
                    socials/ABN), 'no_data' (site loaded, nothing extractable),
                    'unreachable' (timeout/connection/DNS failure)

CRAWL DEPTH: homepage first. If no email found there, try up to 2 common contact
paths (/contact, /contact-us) — bounded, so a slow/dead site can't blow up runtime.
"""
import argparse
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
FB_RE = re.compile(r"https?://(?:www\.)?facebook\.com/[a-zA-Z0-9._\-/]+", re.I)
IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[a-zA-Z0-9._\-/]+", re.I)
LI_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[a-zA-Z0-9._\-/]+", re.I)
ABN_RE = re.compile(r"ABN[:\s]*([\d\s]{11,17})", re.I)

JUNK_EMAIL_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "godaddy.com", "yourdomain.com",
    "domain.com", "schema.org", "w3.org", "gstatic.com", "googleapis.com",
    "cloudflare.com", "email.com",
)
JUNK_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
CONTACT_PATHS = ("/contact", "/contact-us")


def clean_emails(text: str):
    found = []
    for m in EMAIL_RE.findall(text or ""):
        e = m.strip().strip(".,;:'\"").lower()
        if any(e.endswith(ext) for ext in JUNK_EMAIL_EXT):
            continue
        if any(d in e for d in JUNK_EMAIL_DOMAINS):
            continue
        if e.count("@") != 1:
            continue
        if e not in found:
            found.append(e)
    return found


def extract(text: str):
    emails = clean_emails(text)
    fb = FB_RE.search(text or "")
    ig = IG_RE.search(text or "")
    li = LI_RE.search(text or "")
    abn_m = ABN_RE.search(text or "")
    abn = None
    if abn_m:
        digits = re.sub(r"\D", "", abn_m.group(1))
        if len(digits) == 11:
            abn = digits
    return {
        "email": emails[0] if emails else None,
        "social_facebook": fb.group(0) if fb else None,
        "social_instagram": ig.group(0) if ig else None,
        "social_linkedin": li.group(0) if li else None,
        "abn": abn,
    }


def normalize_url(website: str) -> str:
    website = (website or "").strip()
    if not website:
        return ""
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    return website


def fetch(session, url, timeout=(5, 8)):
    try:
        r = session.get(url, timeout=timeout, headers={"User-Agent": UA}, allow_redirects=True)
        if r.status_code < 400 and r.text:
            return r.text
    except requests.RequestException:
        pass
    return None


def enrich_one(place_id, website):
    url = normalize_url(website)
    if not url:
        return place_id, {"enrich_status": "unreachable"}
    session = requests.Session()
    home_text = fetch(session, url)
    if home_text is None:
        return place_id, {"enrich_status": "unreachable"}

    data = extract(home_text)

    # If no email yet, try a couple of common contact paths (bounded).
    if not data["email"]:
        base = url.rstrip("/")
        for path in CONTACT_PATHS:
            more_text = fetch(session, base + path, timeout=(4, 6))
            if more_text:
                more = extract(more_text)
                for k, v in more.items():
                    if v and not data.get(k):
                        data[k] = v
            if data["email"]:
                break

    if data["email"]:
        status = "ok"
    elif data["social_facebook"] or data["social_instagram"] or data["social_linkedin"] or data["abn"]:
        status = "partial"
    else:
        status = "no_data"
    data["enrich_status"] = status
    return place_id, data


def main():
    ap = argparse.ArgumentParser(description="Website-crawl enrichment pass over leads.db")
    ap.add_argument("--db", default="leads.db")
    ap.add_argument("--profile-like", default="lectric", help="SQL LIKE fragment to match the profile column")
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(businesses)")}
    add_cols = {
        "email": "TEXT", "social_facebook": "TEXT", "social_instagram": "TEXT",
        "social_linkedin": "TEXT", "abn": "TEXT", "enrich_status": "TEXT", "enriched_at": "TEXT",
    }
    for col, typ in add_cols.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE businesses ADD COLUMN {col} {typ}")
    conn.commit()

    q = f"""SELECT place_id, website FROM businesses
            WHERE profile LIKE ? AND website IS NOT NULL AND website != ''
              AND (enrich_status IS NULL OR enrich_status = '')"""
    params = [f"%{args.profile_like}%"]
    if args.limit:
        q += " LIMIT ?"
        params.append(args.limit)
    rows = conn.execute(q, params).fetchall()
    total = len(rows)
    print(f"Enriching {total} businesses (profile LIKE '%{args.profile_like}%') with {args.workers} workers", flush=True)

    counts = {"ok": 0, "partial": 0, "no_data": 0, "unreachable": 0}
    start = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(enrich_one, r["place_id"], r["website"]): r["place_id"] for r in rows}
        for fut in as_completed(futures):
            place_id, data = fut.result()
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                """UPDATE businesses SET email=?, social_facebook=?, social_instagram=?,
                   social_linkedin=?, abn=?, enrich_status=?, enriched_at=? WHERE place_id=?""",
                (data.get("email"), data.get("social_facebook"), data.get("social_instagram"),
                 data.get("social_linkedin"), data.get("abn"), data["enrich_status"], now, place_id),
            )
            counts[data["enrich_status"]] += 1
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = (time.time() - start) / 60
                conn.commit()
                print(f"  [{done}/{total}] ok={counts['ok']} partial={counts['partial']} "
                      f"no_data={counts['no_data']} unreachable={counts['unreachable']} ({elapsed:.1f}m elapsed)",
                      flush=True)

    conn.commit()
    elapsed = (time.time() - start) / 60
    print(f"\nDone in {elapsed:.1f}m: {counts}", flush=True)


if __name__ == "__main__":
    main()
