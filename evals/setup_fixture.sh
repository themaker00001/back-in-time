#!/usr/bin/env bash
# Usage: setup_fixture.sh <dest-dir>
# Copies the acme-api fixture to <dest-dir> and makes it a git repo (with .env* git-ignored),
# like a real project whose environment files are not under version control.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/fixtures/acme-api" && pwd)"
DEST="$1"
mkdir -p "$DEST"
cp -R "$SRC/." "$DEST/"
mv "$DEST/dot-env" "$DEST/.env"
mv "$DEST/dot-gitignore" "$DEST/.gitignore"
cd "$DEST"
git init -q -b main
git -c user.name=fixture -c user.email=fixture@example.com add -A
git -c user.name=fixture -c user.email=fixture@example.com commit -qm "acme-api 2.0.3"
