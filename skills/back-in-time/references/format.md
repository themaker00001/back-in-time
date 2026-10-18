# File formats

Everything `bit.py` reads or writes. Use this to keep a timeline by hand when Python isn't available, to
write another tool against it, or to debug one. The format is deliberately plain JSON/JSONL/Markdown.

## Layout

```
<project>/.back-in-time/
  bit.py                    self-contained copy of the tool (refreshed by `init`)
  CURRENT                   slug of the default timeline
  README.md  .gitignore     (.gitignore excludes */snapshots/, */diffs/ and */.lock)
  <YYYY-MM-DD>-<slug>/
    plan.json               the steps (source of truth for intent)
    journal.jsonl           append-only events (source of truth for what happened)
    TIMELINE.md             generated handoff doc: never edit by hand
    .lock                   flock target: one mutating bit command per timeline at a time
    snapshots/r<run>-<step>/manifest.json + files/...    before-state            (gitignored)
    diffs/r<run>-<step>.diff  and  .hunks.json           what the step changed   (gitignored)
```

Current state is **derived**: `status`, `next`, and `TIMELINE.md` are a pure function of
`plan.json` + `journal.jsonl`. Delete `TIMELINE.md` and it comes back on the next command.

**What to commit:** `plan.json`, `journal.jsonl`, `TIMELINE.md`, `bit.py`. **What stays local:**
`snapshots/` and `diffs/` hold copies and diffs of your files (secrets, size). A clone without them can
read, replay and fork a timeline; `undo` there can only run explicit `undo` commands and says so when it
can't restore files.

## plan.json

```json
{
  "version": 1,
  "title": "Stage the demo app",
  "goal": "Bump version and add staging env",
  "created": "2026-10-18T08:00:00Z",
  "params": {"ver": "2.0.0"},
  "steps": [
    {
      "id": "s1",
      "title": "bump version",
      "do": "sed -i.bak 's/1.0.0/{{ver}}/' package.json && rm package.json.bak",
      "manual": "what to do by hand (instead of, or as well as, `do`)",
      "pre": "guard: must exit 0 before the step runs",
      "skip_if": "exit 0 means already done: skip (ignored if the step failed or was interrupted before)",
      "verify": "must exit 0 after the step",
      "undo": "shell command to revert",
      "undo_manual": "how to revert by hand ({{vars}} are filled in when shown)",
      "touches": ["package.json", "config/", ".env.{{env}}"],
      "reversible": "yes | partial | no | unknown",
      "notes": "for partial/no: what can't be undone, and the mitigation"
    }
  ]
}
```

Only `id` and one of `do`/`manual` are required. Ids must be unique ignoring case. `reversible` defaults to
`yes` if the step has `undo`, `touches` or `undo_manual`, else `unknown`. `touches` entries are relative to the
project root (or absolute, for files outside it) and may contain `{{vars}}`; the project root itself and
`.back-in-time/` are refused. `{{name}}` in `pre`, `skip_if`, `do`, `verify`, `undo` and `touches` is substituted
from `params`, `replay --set`, `set`, and `BIT_SET` lines; an undefined name stops the step before anything runs.

## journal.jsonl

One JSON object per line, appended and never rewritten. Every event has `ts` (UTC ISO-8601), `run` (an
integer, 1 for the original run, +1 per replay) and `event`. Non-UTF-8 bytes and a torn last line are
tolerated on read; the next append starts on a fresh line.

| event | extra fields |
|-------|--------------|
| `run_started` | `kind` (`original`/`replay`/`fork`), `set` (param overrides), `from` |
| `param_set` | `key`, `value`, `source` (`cli` or `step:<id>`) |
| `step_begin` | `step`, `git_head`, `snapshot` (`{path: state}`), `snapshot_reused`, `approved` |
| `step_done` | `step`, `status` (`applied`/`failed`/`skipped`), `how` (`do`/`manual`/`skip_if`/`replay --from`), `changed` (`[{path,before,after}]`), `after` (`{path: state}`), `diff`, `git` (`head_before`,`head_after`,`commits`), `cmd`, `exit`, `secs`, `output` (redacted tail), `verify` (`{cmd,exit,output,secs}`), `warnings`, `note` |
| `step_blocked` | `step`, `reason`, `cmd`, `exit`, `output` (a `pre` guard failed; nothing ran) |
| `step_reverted` | `step`, `ok`, `method` (`restore`/`command`/`restore+command`/`manual`), `restored`, `merged`, `cmd`, `exit`, `output`, `detail`, `warnings`, `manual_remainder`, `note` |
| `verify_check` | `step`, `ok`, `problems` |
| `plan_changed` | `step`, `action` (`added`/`updated`/`removed`), `fields` (only recorded once execution has begun) |
| `note` | `text`, `step` (also written when a `BIT_SET` line is ignored) |

A **state** string describes a path: `absent`, `file:<12 hex of sha256>`, `dir:<12 hex of tree hash>`,
`symlink:<target>`, or `other`.

### Folding events into status

For one run (default: the latest), a step starts `pending`, then, in journal order:
`step_begin` → `running`; `step_done` → its `status`; `step_reverted` with `ok` → `reverted` (a failed
revert leaves the status and sets a hazard). `step_blocked` changes nothing. Reverse-chronological *begin*
order, not plan order, decides what `undo` reverts first. Variables for a run are `params`, overlaid with that
run's `set`, then each `param_set` in order. `BIT_SET` may not overwrite a name that is a plan param, in a
`run_started.set`, or set with `source: cli`.

Two things are derived beyond the run itself: **earlier runs** that still have applied/failed/half-done steps
(a replay is a clean slate in the journal, not in the world), and **orphans**, steps with live effects in the
journal that are no longer in `plan.json` (`check` errors on them; `undo` reports them and cannot revert them).

## Snapshots

`manifest.json` maps each touched path to `{"kind": <absent|file|dir|symlink>, "state": <state>}` (plus
`"late": true` for a path added to `--touches` after the step already ran once). Copies live under
`files/<path>` (paths outside the project under `files/__abs__/<path>`). A symlink in `--touches` also tracks
what it points at. Per `(run, step)` the first snapshot is kept, so retries after a half-finished attempt keep
the true before-state. After a successful revert the folder is renamed `r<run>-<step>.reverted-N`, so a re-run of
the step takes a fresh before-state instead of restoring a stale one. A path over `BIT_MAX_SNAPSHOT_MB` (default
50) makes `do`/`begin` refuse before anything runs. `undo` validates that every stored copy exists and
refuses (never half-restores) if one doesn't.

A step's `after` map (in `step_done`) is what `undo` and `verify` compare against to detect drift. When several
steps touched one path only the latest is checked, and a path already back at its before-state doesn't count as
drift (a retry after a failed undo command).

`diffs/r<run>-<step>.hunks.json` holds, per changed plain-text file, the line blocks (with 3 lines of context) the
step replaced. `undo --merge` swaps each block back if the block still appears in the file as the step left it
(with several matches, the one nearest where the step put it), and reports a conflict, changing nothing, if the
surrounding lines were edited since.

## Redaction and secrets

Anything written to the journal, `TIMELINE.md`, or a diff is scrubbed for private keys, `sk-`/`ghp_`/
`github_pat_`/`AKIA`/`xox*` tokens, `Bearer` tokens, and `password|secret|token|api_key ...= value` assignments
(values that are `$VARS` or `{{vars}}`, quoted or not, are left alone). It's a safety net, not a guarantee, so the
tool also **refuses** to store secret-looking text in the first place: step fields, `--param`, `--set`, `set`,
`init` title and goal, and `BIT_SET` values. `snapshots/` and `diffs/` are gitignored because file copies and
diffs are not reliably scrubbable.

## Exit codes

`0` success · `1` a step or verify failed (or a revert failed) · `2` bad input, bad plan, undefined variable, a
snapshot over the limit, or a refused secret · `3` paused for a decision (manual step, irreversible step awaiting
approval, undo blocked by an irreversible step, drift, a merge conflict, a missing snapshot, or an orphaned step).
