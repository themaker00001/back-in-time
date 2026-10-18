# Back in Time

**A skill that makes AI-agent work replayable and reversible.**

It is the opposite of *Back to the Future*: you never rewrite the past. You keep an exact record of it,
so the future can **repeat** it or **reverse** it.

An agent that finishes a task keeps nothing. Days later, nobody (including the same agent, in a new
session) remembers which commands ran, what the files looked like before, or how to put things back.
Back in Time makes the agent write that memory down as it works: a **plan** with an undo for every step, and
a **trace** of what actually happened. Both live in a `.back-in-time/` folder next to the work, so
they can be committed, handed to another agent, and read cold.

```
 plan first                  act through the tool                 days later
┌────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────────┐
│ step: do / undo /  │      │ snapshot before-state    │      │ read TIMELINE.md             │
│  verify / guards   │ ───▶ │ run, verify, record diff │ ───▶ │ undo --all      (dry run 1st)│
│ check: undo defined?│      │ append to journal.jsonl  │      │ replay --set env=prod --auto │
│  anything one-way? │      │ regenerate TIMELINE.md   │      │ fork ../other-checkout       │
└────────────────────┘      └──────────────────────────┘      └──────────────────────────────┘
```

## What you get

- **A skill** ([`skills/back-in-time/SKILL.md`](skills/back-in-time/SKILL.md)) that teaches an agent when to
  keep a timeline and how to write steps that survive a replay: idempotent `do`, honest `undo`, `verify`
  that proves something, guards against the wrong environment, and an explicit "this one can't be
  undone" class that needs the user's yes.
- **A tiny tool** ([`bit.py`](skills/back-in-time/scripts/bit.py), one file, Python standard library only)
  that does the bookkeeping agents are bad at: before-state snapshots, diffs, an append-only journal,
  drift detection, reverse-order undo, replay with parameters. It copies itself into `.back-in-time/`, so a
  repo that contains a ledger can be replayed on a machine that never had the skill.

## A one-minute tour

`bash examples/demo.sh` runs this for real in a throwaway directory. Output below is abridged.

```text
# plan the work; undo is designed up front
$ bit step s1 --title "Bump version" --do "sed -i.bak 's/1.0.0/{{ver}}/' package.json && rm package.json.bak" \
      --touches package.json --verify "grep -q '{{ver}}' package.json"
$ bit step s2 --title "Create {{env}} env file" --do "sed 's/localhost:3000/{{env}}.example.com/' .env > .env.{{env}}" \
      --touches ".env.{{env}}" --verify "grep -q '{{env}}.example.com' .env.{{env}}"
$ bit step s3 --title "Notify the team" --manual "Post the release note in #releases" \
      --reversible no --notes "a posted message can't be unsent"
$ bit check
0 error(s), 0 warning(s) in 3 step(s).

# do it: every step is snapshotted, verified and traced
$ bit do s2
OK   s2: Create staging env file (0.0s)
  changed: .env.staging  (absent -> file:864b6f474ad9)
  diff saved: .back-in-time/.../diffs/r1-s2.diff

# change your mind: undo is a dry run until you say --yes
$ bit undo --all
REVERT PLAN (everything run 1 applied), in this order:
  revert  s2  Create staging env file
          restore files: .env.staging
  revert  s1  Bump version
          restore files: package.json
DRY RUN. Nothing was changed. Show this to the user, then re-run with --yes to execute.
$ bit undo --all --yes          # package.json is byte-for-byte what it was; .env.staging is gone

# a week later: the same plan, a different environment
$ bit replay --set env=production --set ver=2.1.0 --auto
OK   s1: Bump version
OK   s2: Create production env file
MANUAL s3: Post the release note in #releases
PAUSED at s3 (needs a human or the agent).
```

Run it first, read the diff second: `.back-in-time/<timeline>/TIMELINE.md` is the handoff document a cold
reader starts from. It is regenerated after every command: where things stand, what's next, hazards,
every step's do/undo/verify, and the exact commands to revert or replay.

## Install

**Claude Code** (personal skill, available in every project):

```bash
git clone https://github.com/themaker00001/back-in-time.git
mkdir -p ~/.claude/skills
cp -R back-in-time/skills/back-in-time ~/.claude/skills/
```

For one project only, copy it to `<project>/.claude/skills/` instead. Claude picks it up from the skill's
description, so you don't need to name it: asking for something repeatable or reversible is enough.

**Claude.ai / Claude desktop:** zip the `skills/back-in-time` folder and upload it in the Skills settings.

**Without any agent:** the tool is useful on its own.
`python3 skills/back-in-time/scripts/bit.py init "My change" --goal "..."`, then `bit.py --help`.

## How it works

| file in `.back-in-time/<timeline>/` | role |
|---|---|
| `plan.json` | the steps: `do`, `undo`, `verify`, `pre` guard, `skip_if`, `touches`, `reversible`, parameters |
| `journal.jsonl` | append-only facts: what ran, exit codes, redacted output, what changed, git commits made |
| `TIMELINE.md` | generated handoff doc (never hand-edited) |
| `snapshots/`, `diffs/` | before-state of touched files (gitignored) and what each step changed |

Current state is *derived* from `plan.json` + `journal.jsonl`, so there is no second copy to drift. Full
schema: [`references/format.md`](skills/back-in-time/references/format.md).

**Commands:** `init` · `step` · `check` · `next` · `do` · `begin`/`done` (for work the agent does with its own
tools) · `auto` · `undo` · `reverted` · `replay` · `fork` · `verify` · `status` · `list` · `use` · `set` ·
`note`. Exit codes: `0` ok, `1` a step failed, `2` bad input, `3` paused for a decision.

### Safety properties

- **Undo is shown before it's done.** `undo` is a dry run unless you add `--yes`.
- **It won't clobber later work.** If a file changed after the step that touched it, restoring is refused
  (drift). `undo --merge` then removes only what the step added or changed and keeps the later edits (text
  files; stops on a conflict); `--force` overwrites.
- **One-way steps are gated.** A step marked `--reversible no` won't run without `--yes`, and `auto` and
  `replay` stop in front of it every time, so yesterday's approval never covers today's send. `undo` stops
  at one instead of guessing.
- **Wrong paths are called out.** If a step declares `--touches` and none of them changed, it says so; an
  undefined `{{variable}}` stops the step before anything runs; a path too big to snapshot (50 MB by default)
  makes the step refuse instead of pretending to be reversible; a symlink's target is tracked too.
- **Reverting can't use a stale or missing snapshot.** A re-run after a revert takes a fresh before-state, and
  `undo` refuses if a stored copy is missing instead of half-restoring.
- **The trace is honest.** Failures are recorded, retries keep the original snapshot, plan edits made
  mid-run are logged, and the journal is append-only.
- **Secrets stay out.** `step`, `--param`, `--set`, `set`, and the title/goal refuse secret-looking text;
  output is scrubbed before it reaches the journal or `TIMELINE.md`; `snapshots/` and `diffs/` (copies and
  diffs of your files) are gitignored; `BIT_SET` values from command output are accepted only if plainly
  shell-safe and can't override a declared parameter.
- **One writer at a time.** Commands on a timeline take a lock, so parallel calls can't lose each other's
  steps; a timeout or Ctrl-C kills the step's whole process group.

### What it deliberately isn't

- **Not a sandbox.** `do`, `undo` and `verify` run shell commands with your privileges. Review the plan the
  way you'd review a script.
- **Not magic undo.** Snapshots cover files and directories up to 50 MB (`BIT_MAX_SNAPSHOT_MB`). A database, a cloud resource or an
  API call is undone by the `undo` command or `--undo-manual` text you write, so it is exactly as good as
  that, and `verify` is how you find out. [`references/step-recipes.md`](skills/back-in-time/references/step-recipes.md)
  has worked patterns (migrations with a backup, cloud resources with account guards, git commits,
  irreversible sends).
- **Not a replacement for git.** Use git for code; this covers everything around it (ignored env files,
  installs, services, other systems) and ties it into one revertible, replayable sequence.
- **Not safe to run on a ledger you didn't write.** `do`, `auto`, `replay`, `fork`, `verify` and `undo` execute
  the shell commands in `plan.json` with your privileges. Read the plan first.
- Clone-only is partial: `snapshots/` and `diffs/` are local, so from a fresh clone you can read, replay and fork a
  timeline, but `undo` can only run explicit `undo` commands (it says so rather than guessing).
- Symlinks *inside* a touched directory are kept as links, not followed. POSIX shell (macOS, Linux, WSL); secret
  scrubbing is a safety net, not a guarantee.

<!-- RESULTS -->

## Development

```bash
python3 -m unittest discover -s tests -v    # black-box tests: every test drives the real CLI
bash examples/demo.sh                        # the tour above
```

Evals (`evals/`): one realistic scenario in three acts. An agent does the work with or without the skill, then
two *cold* agents (no skill, no memory) try to revert it and to repeat it for production from nothing but
the folder, graded by file comparison. See [`evals/README.md`](evals/README.md).

## License

MIT, see [LICENSE](LICENSE).
