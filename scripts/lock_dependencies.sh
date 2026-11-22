#!/usr/bin/env bash
# Regenerate constraints.txt, the pinned versions of every package that
# requirements.txt and requirements-dev.txt install, directly or transitively.
# Every pip install in the Pages and CI workflows passes
# --constraint constraints.txt, so a new upstream release never reaches the
# hourly refresh until this script is re-run and the result is reviewed.
#
# The resolution is universal: it carries environment markers where Python
# 3.10, 3.11 and 3.12 need different versions (numpy, pandas and others do), so
# one file serves CI's 3.10 and 3.12 jobs and the 3.12 Pages refresh.
#
# Needs uv (https://docs.astral.sh/uv/) and network access. Run it by hand
# after editing a requirements file; CI never runs it. tests/test_constraints.py
# checks offline that the result still matches the requirements files and that
# the command below matches the one recorded in constraints.txt's header.
#
# uv keeps the versions already in constraints.txt wherever they still satisfy
# the requirements, so re-running with no requirement change gives no diff.
# Pass --upgrade to drop the existing pins and resolve every package afresh.
#
# Usage: scripts/lock_dependencies.sh [--upgrade]
set -euo pipefail

upgrade=false
case "${1-}" in
  "") ;;
  --upgrade) upgrade=true ;;
  *)
    echo "Usage: $0 [--upgrade]" >&2
    exit 2
    ;;
esac

if ! command -v uv > /dev/null; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

if [[ $upgrade == true ]]; then
  rm -f constraints.txt
fi

# uv also prints the result; the file is what matters.
uv pip compile requirements.txt requirements-dev.txt --universal --python-version 3.10 -o constraints.txt > /dev/null
