# Step recipes

Patterns for writing steps whose `undo` is real and whose replay is safe. Each was chosen because the
obvious way to write the step has a trap. `bit` below means `python3 .back-in-time/bit.py`.

Contents: [edit with your own tools](#edit-files-with-your-own-tools) ·
[edit with a command](#edit-files-with-a-command) · [delete things](#delete-things) ·
[git commits](#git-commits) · [installs](#installs-and-lockfiles) · [database migrations](#database-migrations) ·
[cloud resources](#cloud-resources) · [background processes](#background-processes) ·
[API / MCP / browser effects](#effects-only-your-own-tools-can-cause) · [irreversible actions](#irreversible-actions) ·
[parameters and discovered values](#parameters-and-discovered-values)

## Edit files with your own tools

The snapshot is taken at `begin`, so edit **after** it.

```bash
bit step s1 --title "Enable debug logging" \
  --manual "Set LOG_LEVEL: debug in config/app.yaml" \
  --touches config/app.yaml \
  --verify "grep -q 'LOG_LEVEL: debug' config/app.yaml"
bit begin s1                     # before-state saved here
# ... use the Edit tool on config/app.yaml ...
bit done s1 --note "changed line 12, nothing else"   # runs --verify, saves the diff
```

`undo` restores the file byte-for-byte, with no `--undo` needed. If someone edits the file afterwards,
`undo` refuses (drift) rather than destroying their work. Then `undo --merge` removes only what the step added
or changed and keeps their edits (it stops with a conflict if they edited the same lines); `--force` overwrites.
Editing the file *before* `begin` leaves no before-state, so `begin` first, always. A dotfile that is a symlink
into a dotfiles repo is fine: listing the link also tracks the file it points at.

## Edit files with a command

```bash
bit step s2 --title "Bump version" \
  --do "sed -i.bak 's/{{old}}/{{new}}/' package.json && rm package.json.bak" \
  --touches package.json \
  --verify "grep -q '\"version\": \"{{new}}\"' package.json"
```

Use `sed -i.bak ... && rm *.bak`: it works on both GNU and BSD/macOS `sed`, where bare `sed -i` doesn't.

## Delete things

Declare the thing in `--touches`, and `undo` restores it from the snapshot:

```bash
bit step d1 --title "Remove stale build output" --do "rm -rf build" --touches build --verify "! test -e build"
```

A path over 50 MB (`BIT_MAX_SNAPSHOT_MB`) makes `do` refuse before running anything, because it could not be
restored. Move instead of delete, so the undo is a move back, and leave it out of `--touches`:
`--do "mkdir -p .trash && mv big-dir .trash/big-dir-{{stamp}}" --undo "mv .trash/big-dir-{{stamp}} big-dir"`
(set `stamp` with `--param` or `BIT_SET`).

## Git commits

The SHA doesn't exist until the commit does, so the step announces it with `BIT_SET`:

```bash
bit step c1 --title "Commit the config change" \
  --pre 'test "$(git branch --show-current)" = main' \
  --do 'git add config/app.yaml && git commit -qm "enable debug" && echo "BIT_SET sha=$(git rev-parse HEAD)"' \
  --undo 'git revert --no-edit {{sha}}' \
  --verify 'git log -1 --format=%s | grep -q "enable debug"'
```

Don't also put the committed files in `--touches` here: files are restored first, so `git revert` would
find nothing left to revert. Revert, not reset, keeps history linear and safe on shared branches. The
journal also records `head_before`/`head_after` and the commit list for every step, whatever it does.

## Installs and lockfiles

Snapshot the manifest files, and make the undo re-sync from them. Restore runs first, then the command:

```bash
bit step i1 --title "Add left-pad" \
  --do "npm install left-pad@1.3.0" \
  --touches package.json package-lock.json \
  --undo "npm ci" \
  --verify "node -e \"require('left-pad')\""
```

## Database migrations

Make an irreversible step reversible by taking a backup in the same step. Be honest in `--reversible`
about what the restore costs:

```bash
bit step m1 --title "Run migration 0042" \
  --pre 'test "$APP_ENV" = staging' \
  --do 'mkdir -p backups && pg_dump "$DATABASE_URL" -Fc -f backups/pre-0042.dump && ./migrate up 0042' \
  --undo 'pg_restore --clean --if-exists -d "$DATABASE_URL" backups/pre-0042.dump' \
  --reversible partial --notes "writes made after the dump are lost on restore" \
  --verify './migrate status | grep -q "0042 applied"'
```

Add `backups/` to `.gitignore`. The connection string stays in `$DATABASE_URL`, never in the plan.

## Cloud resources

Guard the account/region (the classic replay disaster is the right command against the wrong target), skip
if it exists, and undo by name:

```bash
bit init "Provision bucket" --param bucket=acme-reports-stg --param account=123456789012
bit step b1 --title "Create bucket" \
  --pre 'aws sts get-caller-identity --query Account --output text | grep -qx {{account}}' \
  --skip-if 'aws s3api head-bucket --bucket {{bucket}}' \
  --do 'aws s3 mb s3://{{bucket}}' \
  --undo 'aws s3 rb s3://{{bucket}}' \
  --verify 'aws s3api head-bucket --bucket {{bucket}}'
```

Replay in prod later is `bit replay --set bucket=acme-reports-prod --set account=210987654321 --auto`.
A `skip_if` step is not undone by default (it wasn't this run's doing), which protects a bucket that
existed before you.

## Background processes

Redirect the process's output. `do` waits for the output pipe to close, so a background job that inherits
it makes the step hang until the timeout (default 900 s, `BIT_TIMEOUT` overrides; a timeout kills the step's
whole process group, background job included):

```bash
bit step p1 --title "Start dev server" \
  --do 'nohup npm run dev > .dev-server.log 2>&1 & echo "BIT_SET pid=$!"' \
  --undo 'kill {{pid}}' --verify 'kill -0 {{pid}}'
```

## Effects only your own tools can cause

A Notion page, an issue, a calendar event, a click in a browser: no shell command undoes these, but they
still belong in the timeline. Record what you'll need to undo them, concretely:

```bash
bit step n1 --title "Create runbook page in Notion" \
  --manual "Create page 'Runbook: staging deploy' under Engineering" \
  --undo-manual "Delete the page whose URL is in {{page_url}}"
bit begin n1
# ... create the page with the MCP tool ...
bit set page_url=https://www.notion.so/...
bit done n1 --note "created via Notion MCP"
```

`undo` prints `MANUAL UNDO needed: ...` and pauses there. After you delete the page, run
`bit reverted n1 --note "deleted via MCP"`, and `undo` carries on with the earlier steps. The `--undo-manual`
text is shown with `{{vars}}` filled in from what you `set`, so record the concrete value, as above.

For browser work, `--manual` should say *which* page and *what* you changed (`Account > Billing >
turn off auto-renew`), because "changed a setting" can't be replayed.

## Irreversible actions

Sending an email, publishing, charging a card, deleting without a backup, force-pushing. Two moves:

1. Split **prepare** (reversible, repeatable) from **commit** (irreversible), so the user approves exactly
   the one step that can't be taken back, after seeing what it will do.
2. Mark the commit step `--reversible no` with a `--notes` that says what can't be undone and any
   mitigation.

```bash
bit step e1 --title "Draft announcement" --do "./render-announcement > out/announce.eml" \
  --touches out/announce.eml --verify "test -s out/announce.eml"
bit step e2 --title "Send announcement to all-staff" --do "sendmail -t < out/announce.eml" \
  --reversible no --notes "email can't be recalled; recipients: all-staff@"
```

`bit do e2` and `bit auto` stop at `e2`. After the user says yes to that step, run `bit do e2 --yes`
(or `bit auto --approve e2`). A replay days later stops there again, which is the point: yesterday's
approval doesn't cover today's send.

## Parameters and discovered values

- **Fixed per run, may differ between runs** (env, version, host): `--param` at `init`, used as `{{name}}`,
  overridden per replay with `--set name=value`.
- **Only known once a step ran** (IDs, SHAs, ports, temp paths): the step prints `BIT_SET name=value` on its
  own line; later steps, `verify` and `undo` can use `{{name}}`. The shell also sees every variable as
  `$BIT_VAR_name`.
- Values are substituted verbatim into the shell command, so quote them in the command where spaces
  are possible (`--do 'mkdir "{{dir}}"'`).
- `BIT_SET` values are pasted into later commands, and command output can come from tools you don't control, so
  only plainly safe values are accepted (letters, digits, `_ @ % + = : , . / ~ -`, up to 500 characters). Anything
  else, or a name that is already a declared parameter or something you `set`, is ignored with a warning and a
  journal note; if you do mean it, `bit set name=value` it yourself. Secret-looking values are never stored.
- An undefined `{{name}}` stops the step before anything runs. That's deliberate: a command with a hole
  in it is how replays do damage.
