# google-maps-scraper

HTTP-only Google Maps business finder — no headless browser, no API key,
no login. Built by empirically reverse-engineering the internal endpoint
`google.com/search?tbm=map` (the same one the Maps web UI itself calls) and
hand-indexing every field from a captured real response.

## Why this over Apify / a Playwright-based scraper

The official Google Places API caps out at 120 results per area and costs
per call. Apify's `compass/crawler-google-places` actor gets around that but
charges $1.50–4 per 1,000 places and runs a full browser under the hood.
Open-source alternatives (`gosom/google-maps-scraper`, `noworneverev/
google-maps-scraper`) also wrap Playwright — ~120 places/minute, needs a
proxy pool at scale, downloads browser binaries, heavier to run in the
background.

This hits the same endpoint directly with `requests`. No browser process,
no proxy needed at our volumes, sub-second per page, runs anywhere Python
runs (including scheduled/background jobs with no display).

## What you get per business

Name, full address, lat/lng, category list, phone, website, Google rating,
review count, `place_id` + `cid` (both usable to build a direct Maps link),
a thumbnail photo URL, and raw opening-hours data.

## How it works

1. Query Google's map search endpoint with a plain text query (e.g.
   `"electricians in Fitzroy VIC"`) — the text itself does the location
   matching, so no separate geocoding step is needed.
2. Response is JSON prefixed with `)]}'` (Google's anti-hijacking guard) —
   strip the prefix, `json.loads` the rest.
3. Each business is a deeply nested array. Field positions were mapped by
   hand against a captured response (see the docstring at the top of
   `gmaps.py` for the exact index map).
4. Paginate by incrementing the `8i` offset in the `pb` parameter in steps
   of 20 (Google's per-page max) until a page comes back empty/short or
   fully duplicate — that's the natural end of results for that query.

## Usage

```bash
# one query
python gmaps.py search "electricians in Fitzroy VIC" --max 120 --out data/electricians_fitzroy.json

# batch from a plain text file, one query per line
python gmaps.py batch data/queries.txt --out-dir data/batch_run --max 60

# batch from a JSON list of {"profile","suburb","state","tier"} — builds
# "<profile> in <suburb> <state>" queries automatically (this is the shape
# to use for the Halvard AU Leads ICP tiers)
python gmaps.py batch-profiles data/targets.json --out-dir data/batch_run --max 60
```

Only dependency is `requests`, pinned via the `uv` inline script header —
run with `uv run --script gmaps.py ...` and it self-installs.

## Pacing / staying under the radar

Batch mode paces 1.5–4s between requests with a rotating User-Agent, no
cookies or auth needed since the endpoint is public. At the volumes this
project needs (dozens to low hundreds of queries), this comfortably avoids
rate limiting. If a queue gets into the thousands of queries, add proxy
rotation to `fetch_page()` before running it — not needed yet.

## If this breaks

Google can change the internal response shape without notice, which is the
one real fragility of hitting an undocumented endpoint directly. If
`parse_page()` starts returning empty lists on a query that clearly has
results, the fix is: capture a fresh raw response, re-run the same
hand-indexing process documented in the `parse_page()` docstring to find
the new field positions, and update the indices. Fallback if the endpoint
gets blocked outright: swap to a Playwright-based approach (`gosom/
google-maps-scraper` on GitHub is the best-maintained one) — much slower,
but resilient to markup/response changes since it reads the rendered page.
