#!/usr/bin/env bash
# Assemble the static GitHub Pages site into _site/.
# Used by both the Pages deploy and CI so the file list cannot drift.
# Fails if any file the site needs is missing.
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf _site
mkdir -p _site/data _site/assets
cp index.html styles.css app.js .nojekyll favicon.ico favicon.svg _site/
cp assets/apple-touch-icon.png _site/assets/
cp data/market.json _site/data/
