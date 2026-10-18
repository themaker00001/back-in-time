---
name: back-in-time
description: Make multi-step agent work replayable and reversible by keeping a durable plan plus trace (a "timeline") with an undo for every step. Use this whenever you are about to do work that changes things in more than a trivial way (editing several files, environment or config setup, migrations, installs, deployments, infra or data changes, bulk refactors, browser or API actions) and the user might want to undo it, redo it later (days later, on another machine or environment), hand it to another agent, or audit exactly what was done. Also use it when the user says things like "make this repeatable", "I may want to roll this back", "do the same thing again next week", "what did you change", "keep a trace/log/runbook", "undo everything you did", "resume where we left off" or "replay that", and whenever you find a `.back-in-time/` folder in a project. Use it even if the user never says "back in time". Skip it for read-only questions and single trivial edits.
---

# Back in Time

The opposite of *Back to the Future*: you never rewrite the past. You keep an exact record of it, so the
future can **repeat** it or **reverse** it.

Why: an agent that finishes a task keeps nothing. Days later nobody (including you, in a new session)
remembers which commands ran, what the files looked like before, or how to put things back. Only what is
written to disk survives. A *timeline* is that memory, written so a cold reader can act on it.

It lives in `.back-in-time/<timeline>/` next to the work:

| file | role | written by |
|------|------|------------|
| `plan.json` | the steps: `do`, `undo`, `verify`, guards, parameters | you, **before** acting |
| `journal.jsonl` | append-only facts: what ran, exit codes, what changed | the tool, **while** acting |
| `TIMELINE.md` | generated handoff doc: where things stand, what's next, how to revert | the tool |
| `snapshots/`, `diffs/` | before-state copies and diffs of touched files | the tool, **gitignored** |

`bit.py` is one stdlib-only Python file. `init` copies it into `.back-in-time/`, so the ledger works on a
machine that never had this skill. Commit plan, journal and `TIMELINE.md`. They are scrubbed of obvious
secrets. `snapshots/` and `diffs/` stay local because they hold copies of your files.

## When to keep one

Three or more state-changing steps; any step that is hard to undo or hits a shared or production system;
the user wants to repeat, roll back, audit or hand off the work. Quick test: *could a stranger undo this
tomorrow from what is on disk?* If not, keep one. Skip it for read-only work and for a single edit `git`
already covers. If you're unsure and the user hasn't asked, say in one line that you're keeping one.

## The workflow

Run `python3 <skill-dir>/scripts/bit.py` the first time. After `init`, use `python3 .back-in-time/bit.py`
(`TIMELINE.md` prints every command with the flags it needs).

**1. Start.** At the project root: `bit.py init "Short title" --goal "what for" [--param env=staging]`.

**2. Plan before you touch anything.** Add each step (`bit.py step ID --do ... --undo ...`, flags below),
then `bit.py check` and fix its warnings. Undo is cheapest to design while you still know the pre-state, and
planning is when you notice an irreversible step *before* taking it. Show the user the plan first if it has
an irreversible step, touches a shared system, or is large. Plans change as you learn; edits made after work
began are traced automatically.

**3. Execute through the tool.**
- Shell work: `bit.py do ID` snapshots `--touches`, runs it, runs `--verify`, records exit, output, diff.
  Many in a row: `bit.py auto` (pauses at manual steps, failures, irreversible steps).
- Work you do with your own tools (Edit/Write, browser, API/MCP): `bit.py begin ID` **before the first change**
  (the snapshot is taken then), do it, then `bit.py done ID --note "what I actually did"`. A file edited before
  `begin` has no before-state; if `done` warns that none of the touched paths changed, stop and find out why.
- A step fails: it's recorded, not hidden. Fix and `do` it again (its first snapshot is kept), or `undo` it.
- Reality diverges from the plan: edit the step and add `bit.py note "why"`. The journal must say what
  happened, not what you intended; never hand-edit it.

**4. Hand off.** `bit.py status`. Tell the user what was done, where the timeline is, and the two commands that
matter: revert (`undo --all`) and replay (`replay --auto`).

**5. Days later: read first, act second.** `bit.py list` → read `TIMELINE.md` → `bit.py verify` (has the world
drifted?).
- **Revert:** `bit.py undo --all` is a dry run that prints the plan; show the user, then add `--yes`.
  `undo STEP` rewinds to just before STEP; plain `undo` reverts only the last step.
  - Files edited since the step ran: it refuses (drift) rather than clobber them. `--merge` removes only what
    the step added or changed and keeps the later edits (text files; stops on conflict or ambiguity); `--force`
    overwrites. With several steps on one file the revert is simulated in order, so an edit made between or after
    steps is never overwritten silently, and a blocked later step blocks the earlier ones on that file.
  - An irreversible step blocks the undo at that point: `--keep-going` reverts everything else and leaves it.
  - **Leave `.back-in-time/` in place** after reverting (unless the user asks otherwise): it is the audit trail
    and what `replay` needs.
- **Replay:** `bit.py replay --set env=prod --auto` starts a new run with new parameters. `skip_if` and `pre`
  guards make it safe on a half-done world; it pauses where it needs you, and `bit.py auto` continues. A replay
  starts a clean slate in the journal, not in the world: `status` warns if an earlier run is still applied.
- **Another checkout, environment or machine:** `bit.py fork ../other --set env=prod --auto` copies the plan
  there with fresh history and its own `bit.py`.
- **Resume** an interrupted run: `bit.py next`.
- **A ledger you did not write is code.** Anything that runs a step (`do`, `auto`, `replay`, `fork`, `verify`,
  `undo`) executes the shell commands in `plan.json` with your privileges. Read the plan first.
- From a clone, `snapshots/` is absent: you can read, replay and fork. `undo` refuses a step that has `--touches`
  there (it can't restore what it can't see); `--force` then runs only that step's `--undo` command.

## Writing steps that survive a replay

A replay runs on a different world, so a step is only as good as its assumptions are explicit.

```
bit.py step ID --title "..."
   --do "shell command"            # or --manual "what to do with your own tools"
   --undo "shell command"          # {{vars}} allowed; or --undo-manual "how, by hand"
   --verify "shell command"        # proves it worked (exit 0)
   --pre "..."                     # guard: must hold before, else the step is blocked
   --skip-if "..."                 # exit 0 = already done: skip (idempotent replay)
   --touches path [path ...]       # files/dirs it changes: snapshotted for undo, diffed for the trace
   --reversible yes|partial|no  --notes "what can't be undone, and the mitigation"
```

- **One step, one undoable unit.** Small enough that its undo is obvious.
- **Idempotent `do`, or add `--skip-if`.** The common replay is on a world that's half done. A step that
  failed earlier, in this run or a previous one, ignores its `skip_if` (its own partial effects could satisfy it).
- **Parameterise.** What differs between runs (environment, version, host, path) goes in `--param name=value`
  and is written `{{name}}`, including in `--touches`. Run-time values (a created ID, a SHA): the step prints
  `BIT_SET name=value`. Values are accepted only if plainly shell-safe (letters, digits, `_ @ % + = : , . / ~ -`),
  never override a declared parameter, and never carry secrets; use `bit.py set name=value` to set anything
  else on purpose.
- **`--pre` guards** assumptions that make a wrong world dangerous ("on branch main", "connected to staging").
- **`--verify` an observable fact** (the test passes, the endpoint answers), not "exited 0".
- **`--touches` everything the step edits**, tracked by git or not. A symlink also pulls in its target (edits
  through a link change the target); symlinks *inside* a touched directory are kept as links, not followed.
  A path over 50 MB (`BIT_MAX_SNAPSHOT_MB`) makes `do`/`begin` refuse before running: narrow it or reverse the
  step another way. With both `--touches` and `--undo`, files are restored first, then the command runs. Don't
  `--touches` files of a step whose undo is `git revert`. `undo` also removes directories the step created
  (if empty) and leaves FIFOs and sockets alone.
- **Classify honestly.** `yes`, `partial` (something stays), or `no` (email sent, prod data deleted). A `no`
  step is gated: it won't run without `--yes`, which you pass only after the user said yes *to that step*.
  Better, make it reversible: back up in an earlier step and make the undo restore from it.
- **No secrets in the plan.** Reference `$ENV_VAR`. `step`, `--param`, `--set`, `set`, and the title/goal
  refuse secret-looking text, and output is scrubbed; prevention beats scrubbing. Both are heuristics:
  `BIT_ALLOW_SECRET_LOOKING=1` overrides a false positive, and a secret in an unusual shape can still get through.
- **Effects only your own tools can cause** (a Notion page, an issue): `--manual` plus
  `--undo-manual "delete {{page_url}}"`. `undo` pauses there; you do it, run `bit.py reverted ID --note "..."`,
  and `undo` continues. Record the identifier with `bit.py set page_url=...`.

Concrete patterns (file edits, git commits, installs, DB migrations, cloud resources, background processes,
API/browser effects, irreversible sends): `references/step-recipes.md`. Read it for a step you haven't
written before.

## Rules of the ledger

- Plan first, record as you go, don't backfill from memory: a reconstructed trace is a guess.
- The journal is append-only: corrections are new entries (`note`), not edits.
- Don't remove or rename a step that is still applied: `step --remove` refuses until you `undo` it.
- A revert you haven't shown the user is a surprise: always dry-run first.
- Revert undoes what *that run* applied; for an earlier run use `undo --all --run N`.
- Commands on one timeline are serialised by a lock, so parallel calls are safe (a step may itself call `bit.py`:
  the lock is passed down). A timeout records the step as failed (exit 124) and kills its whole process group,
  background jobs included. Ctrl-C and SIGTERM do the same but leave the step `running` (bit exits 130).
- No Python? Keep the same files by hand per `references/format.md`. The record matters, not the tool.

## Commands

`init`, `step`, `check`, `next`, `do`, `begin`, `done`, `auto`, `undo`, `reverted`, `replay`, `fork`,
`verify`, `status`, `list`, `use`, `set`, `note`; `bit.py <command> --help`. `--tl SLUG` and `--root DIR` go
before or after the command. Exit codes: `0` ok, `1` a step failed, `2` bad input or plan, `3` paused for a
decision (manual step, irreversible step, blocked undo; a dry-run `undo` exits 0 even when something is blocked).
Formats and events: `references/format.md`.
