#!/usr/bin/env bash
#
# make_public_copy.sh — produce a pruned, public-ready copy of this repo.
#
# The public release method (see the owner's going-public checklist) is
# "fresh copy, never flip the switch": git history keeps every deleted file, so
# we never make the *existing* private repo public. Instead this script copies
# the working tree to a sibling folder, strips the internal-only files, and
# leaves it to a human to `git init` + review + push.
#
# It is deterministic and idempotent: run it as many times as you like, the
# destination is rebuilt from scratch each time and the pruned paths are always
# gone. It never touches this repo, never runs git, and never pushes anything.
#
# Usage:
#   bash scripts/make_public_copy.sh
#
set -euo pipefail

# Resolve the repo root from this script's location, so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="$(cd "$SRC_DIR/.." && pwd)/indratrace-python-sdk-public"

echo "==> Source:      $SRC_DIR"
echo "==> Destination: $DEST_DIR"

# Rebuild the destination from scratch every run — that's what makes this
# idempotent. Guard against absurd paths before an rm -rf.
case "$DEST_DIR" in
  */indratrace-python-sdk-public) ;;
  *) echo "refusing to delete unexpected path: $DEST_DIR" >&2; exit 1 ;;
esac
rm -rf "$DEST_DIR"
mkdir -p "$DEST_DIR"

# Copy the working tree, excluding .git (a fresh public history is created by
# the human, not carried over) and the local junk that isn't source: virtualenv,
# build artifacts, tool caches, harness data, OS cruft. These are all in
# .gitignore too, but excluding them here keeps the copy clean even if a stray
# one is present.
echo "==> Copying working tree (excluding .git and local junk)…"
rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'dist/' \
  --exclude 'build/' \
  --exclude '*.egg-info/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '.mypy_cache/' \
  --exclude 'htmlcov/' \
  --exclude '.coverage' \
  --exclude 'dev/clickhouse-data/' \
  --exclude '.DS_Store' \
  "$SRC_DIR"/ "$DEST_DIR"/

# Prune the internal-only files that must not appear in the public repo. These
# are the *automatic* deletions; anything needing judgment is flagged below for a
# human instead of being guessed at here.
echo "==> Pruning internal-only paths…"
prune() {
  # Delete a path under DEST if present; report what happened either way.
  local target="$DEST_DIR/$1"
  if [ -e "$target" ]; then
    rm -rf "$target"
    echo "    removed: $1"
  else
    echo "    (absent, ok): $1"
  fi
}

prune "docs/prompts"        # internal workflow prompts + roadmap language
prune "docs/PROGRESS.md"    # internal build journal
prune "docs/reference"      # internal platform spec/architecture exports
prune ".env"               # any local secrets (never public)
prune ".env.local"

# Belt-and-suspenders on caches, in case rsync's excludes were bypassed.
find "$DEST_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST_DIR" -type d \
  \( -name '.pytest_cache' -o -name '.ruff_cache' -o -name '.mypy_cache' \) \
  -prune -exec rm -rf {} + 2>/dev/null || true

echo ""
echo "==> Pruned copy ready at: $DEST_DIR"
echo ""
echo "===================================================================="
echo " MANUAL REVIEW BEFORE THE FIRST PUBLIC COMMIT (a second pair of eyes)"
echo "===================================================================="
echo ""
echo "  [ ] ADR pass — docs/adr/*: reword or drop any moat / commercialization"
echo "      framing. Look hardest at docs/adr/0002 (public-PyPI-package) — it"
echo "      carries the most business-strategy language. Engineering ADRs"
echo "      generally keep; review each one as if a stranger will read it."
echo ""
echo "  [ ] Product spec — docs/product-spec.md: strip internal product names"
echo "      (Compliance, EPM, …) and any unannounced roadmap."
echo ""
echo "  [ ] docs/reference/*: the architecture/spec HTML+PDF exports may name"
echo "      internal systems — confirm they're safe to publish or drop them."
echo ""
echo "  [ ] Internal endpoints: grep for otel.indrasol.com and any Azure /"
echo "      infra hostnames, team names, or private URLs; replace with"
echo "      neutral examples (localhost:4318)."
echo ""
echo "  [ ] README quickstart re-verified against the latest published wheel."
echo ""
echo "  This script does NOT git-init or push — that's the human's job:"
echo "    cd $DEST_DIR && git init && git add -A && git commit -m 'Initial public release'"
echo ""
