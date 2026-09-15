#!/bin/bash
# Sequential ICP grid-scrape queue: runs each remaining Tier-1 profile through
# au_grid.py, then ingests + exports after each one finishes, so the dashboard
# grows incrementally without needing anyone to babysit each run.
export PATH="/opt/homebrew/bin:$PATH"
cd ~/Downloads/google-maps-scraper || exit 1

run_one() {
  local keyword="$1" slug="$2" profile="$3"
  echo "=== [$(date '+%H:%M:%S')] Starting: $keyword ($profile) ===" | tee -a queue.log
  mkdir -p "data/grid_${slug}"
  uv run --script au_grid.py "$keyword" \
    --out-dir "data/grid_${slug}" \
    --max-per-point 120 \
    --pace 1.3 \
    --profile "$profile" \
    --tier-num 1 >> "data/grid_${slug}/run.log" 2>&1
  echo "=== [$(date '+%H:%M:%S')] Finished: $keyword ===" | tee -a queue.log

  # ingest + export after each run so progress is visible incrementally
  full_file="data/grid_${slug}/${keyword// /_}_full.json"
  if [ -f "$full_file" ]; then
    uv run --script pipeline.py ingest "$full_file" >> queue.log 2>&1
    uv run --script pipeline.py export >> queue.log 2>&1
    echo "=== [$(date '+%H:%M:%S')] Ingested + exported after $keyword ===" | tee -a queue.log
  else
    echo "=== WARNING: no output file found for $keyword at $full_file ===" | tee -a queue.log
  fi
}

run_one "painters"                    "painters"      "Painters & decorators"
run_one "joiners cabinet makers"      "joiners"       "Joiners/cabinet makers"
run_one "landscapers"                 "landscapers"   "Landscapers/garden designers"
run_one "signage sign makers"         "signage"       "Signage & print shops"
run_one "plumbers"                    "plumbers"      "Plumbers"
run_one "renovation builders"         "renovation"    "Boutique renovation/building contractors"
run_one "roofing contractors"         "roofing"       "Roofing contractors"
run_one "pool builders"               "pool_builders" "Pool builders/installers"
run_one "air conditioning installers" "hvac"          "HVAC/air-con install & service"

echo "=== [$(date '+%H:%M:%S')] QUEUE COMPLETE — all 9 Tier 1 ICPs done ===" | tee -a queue.log
