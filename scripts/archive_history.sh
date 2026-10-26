#!/usr/bin/env bash
# Archive an end-of-day snapshot onto origin's data-history branch.
# Used by the Pages workflow's archive job; run it from a checkout of main.
# Creates the branch as an orphan holding only history/ if it does not exist,
# commits only when something changed, and never force-pushes.
#
# Usage: scripts/archive_history.sh SNAPSHOT [WORKTREE]
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 SNAPSHOT [WORKTREE]" >&2
  exit 2
fi

branch=data-history
snapshot=$(realpath "$1")
worktree=$(realpath -m "${2:-$(mktemp -d)/$branch}")

cd "$(dirname "$0")/.."
script="$PWD/scripts/archive_snapshot.py"

# ls-remote exits 2 only when the branch is missing; anything else is an error.
remote_status=0
git ls-remote --exit-code --heads origin "$branch" > /dev/null || remote_status=$?
if [[ $remote_status -eq 0 ]]; then
  git fetch --no-tags --depth=1 origin "$branch"
  git worktree add --detach "$worktree" FETCH_HEAD
elif [[ $remote_status -eq 2 ]]; then
  echo "Creating orphan branch $branch"
  git worktree add --orphan -b "$branch" "$worktree"
else
  echo "Could not list origin's branches (git exit $remote_status)" >&2
  exit "$remote_status"
fi

python3 "$script" "$snapshot" "$worktree"

cd "$worktree"
git add --all -- history
if git diff --cached --quiet; then
  echo "No history changes to commit"
  exit 0
fi

day=$(git diff --cached --name-only --diff-filter=A -- 'history/????-??-??.json' \
  | sed -n 's|^history/\(.*\)\.json$|\1|p' | tail -n 1)
if [[ -n "$day" ]]; then
  message="data: archive end-of-day snapshot $day"
else
  message="data: update history manifest"
fi
git commit --quiet --message "$message"
git push origin "HEAD:refs/heads/$branch"
echo "Pushed \"$message\" to $branch"
