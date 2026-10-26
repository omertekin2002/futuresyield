#!/usr/bin/env bash
# Assemble the static GitHub Pages site into _site/.
# Used by both the Pages deploy and CI so the file list cannot drift.
# Fails if any file the site needs is missing.
#
# Archived end-of-day snapshots are copied from HISTORY_DIR (default: a local
# history/ directory, laid out like the data-history branch) into
# _site/data/history/. Without a history/index.json there, the site ships an
# empty manifest and the dashboard hides its history control.
set -euo pipefail

cd "$(dirname "$0")/.."
history_dir=${HISTORY_DIR:-history}

rm -rf _site
mkdir -p _site/data/history _site/assets
cp index.html styles.css app.js .nojekyll favicon.ico favicon.svg _site/
cp assets/apple-touch-icon.png _site/assets/
cp data/market.json _site/data/

if [[ -f "$history_dir/index.json" ]]; then
  find "$history_dir" -maxdepth 1 -type f \
    \( -name index.json -o -name '????-??-??.json' \) \
    -exec cp {} _site/data/history/ \;
  echo "Copied $(find _site/data/history -name '????-??-??.json' | wc -l) archived days from $history_dir"
else
  echo "No history manifest at $history_dir/index.json; building without history"
  printf '{"schema_version": 1, "days": []}\n' > _site/data/history/index.json
fi
