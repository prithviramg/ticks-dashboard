#!/usr/bin/env bash
# Daily cron entry point: build pages for any newly archived days and publish the repo to GitHub Pages
# as a single commit (squash + force push), so the repo never accumulates history.
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=/home/prithvi/venv/bin/python3
MSG_PREFIX="NIFTY straddle dashboard (through"
cd "$REPO_DIR"

echo "=== $(date '+%F %T') publish start"

# Keep edits made on github.com. They arrive as commits on top of the last published commit, so the
# force push below would otherwise erase them. Our own earlier publishes (replaced by later squashes)
# are skipped. An edit that doesn't apply cleanly stops the run rather than being overwritten.
git fetch -q origin
for c in $(git rev-list --reverse HEAD..origin/main); do
    if [[ "$(git log -1 --format=%s "$c")" == "$MSG_PREFIX"* ]]; then
        continue
    fi
    echo "=== applying web edit: $(git log -1 --format='%h %s' "$c")"
    if ! git cherry-pick "$c" >/dev/null; then
        git cherry-pick --abort || true
        echo "=== ERROR: web edit $c conflicts with local changes; resolve by hand, nothing published" >&2
        exit 1
    fi
done

"$PYTHON" build_dashboard.py --missing

if [[ -z "$(git status --porcelain)" && "$(git rev-list --count HEAD)" == 1 \
      && "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]]; then
    echo "=== nothing changed"
    exit 0
fi

latest=$(ls docs/data | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.json$' | sort | tail -n 1 || true)
msg="$MSG_PREFIX ${latest%.json})"
git add -A
git reset -q --soft "$(git rev-list --max-parents=0 HEAD)"  # squash everything into the root commit
git commit -q --amend -m "$msg"
git push -q --force origin main
echo "=== published: $msg"
