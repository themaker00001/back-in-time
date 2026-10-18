#!/usr/bin/env bash
# A one-minute tour of Back in Time, in a throwaway directory:
#   plan a change -> apply it -> revert it byte-for-byte -> replay it with a new parameter.
# Run:  bash examples/demo.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/skills/back-in-time/scripts/bit.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

say() { printf '\n\033[1m# %s\033[0m\n' "$*"; }
run() {
  printf '\033[2m$ bit %s\033[0m\n' "$(python3 -c 'import shlex,sys; print(shlex.join(sys.argv[1:]))' "$@")"
  python3 .back-in-time/bit.py "$@"
}

printf '{"name": "demo", "version": "1.0.0"}\n' > package.json
printf 'API_URL=http://localhost:3000\n' > .env

say "1. Start a timeline and plan the work (undo is designed up front)"
python3 "$SRC" init "Stage the demo app" --goal "Bump the version and add a staging env file" \
  --param ver=2.0.0 --param env=staging | head -1
run step s1 --title "Bump version" \
  --do "sed -i.bak 's/1.0.0/{{ver}}/' package.json && rm package.json.bak" \
  --touches package.json --verify "grep -q '{{ver}}' package.json"
run step s2 --title "Create {{env}} env file" \
  --do "sed 's/localhost:3000/{{env}}.example.com/' .env > .env.{{env}}" \
  --touches ".env.{{env}}" --verify "grep -q '{{env}}.example.com' .env.{{env}}"
run step s3 --title "Notify the team" --manual "Post the release note in #releases" \
  --reversible no --notes "a posted message can't be unsent"
run check || true

say "2. Do it. Every step is snapshotted, verified, and traced"
run do s1
run do s2
run next

say "3. Change your mind: undo is a dry run until you say --yes"
run undo --all
run undo --all --yes
echo "package.json is back to: $(cat package.json)"; ls -A | tr '\n' ' '; echo

say "4. A week later: replay the same plan against a different environment"
run replay --set env=production --set ver=2.1.0 --auto || true
cat .env.production

say "5. The handoff doc a cold reader (or another agent) starts from"
sed -n '1,22p' .back-in-time/*/TIMELINE.md
