#!/usr/bin/env bash
# Check out origin's data-history branch read-only into a detached worktree.
# Used by the Pages workflow's build job and by CI's smoke and archive jobs,
# so the rule below is written once. Run it from a checkout of the repository.
#
# When the branch exists, prints the worktree's history directory (DEST/history)
# on stdout. When origin has no data-history branch (git ls-remote exits 2), it
# says so on stderr, prints nothing and succeeds, so callers build without
# history. Any other failure to list or fetch origin (network, auth, rate
# limit) exits non-zero: a missing archive must never be mistaken for an empty
# one. Progress and git output go to stderr, keeping stdout to the path alone.
#
# Usage: scripts/fetch_history.sh DEST
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 DEST" >&2
  exit 2
fi

branch=data-history
dest=$(realpath -m "$1")

cd "$(dirname "$0")/.."

# ls-remote exits 2 only when the branch is missing; anything else is an error.
remote_status=0
git ls-remote --exit-code --heads origin "$branch" > /dev/null || remote_status=$?
if [[ $remote_status -eq 2 ]]; then
  echo "No $branch branch on origin; continuing without history" >&2
  exit 0
elif [[ $remote_status -ne 0 ]]; then
  echo "Could not list origin's branches (git exit $remote_status)" >&2
  exit "$remote_status"
fi

git fetch --no-tags --depth=1 origin "$branch" >&2
git worktree add --detach "$dest" FETCH_HEAD >&2
echo "Checked out $branch at $dest" >&2
echo "$dest/history"
