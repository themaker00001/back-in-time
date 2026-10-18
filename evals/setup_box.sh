#!/usr/bin/env bash
# Usage: setup_box.sh <dest-dir>
# Materialises the dev-box fixture: a fake home directory (home/), a project (project/) and a
# send-notice tool whose effect (an entry in outbox.log) cannot be undone. Deliberately NOT a git repo:
# there is no commit to fall back on, so the only ground truth is whatever record the earlier agent left.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/fixtures/dev-box" && pwd)"
DEST="$1"
mkdir -p "$DEST"
cp -R "$SRC/." "$DEST/"
mv "$DEST/home/dot-zshrc" "$DEST/home/.zshrc"
mv "$DEST/home/dot-gitconfig" "$DEST/home/.gitconfig"
