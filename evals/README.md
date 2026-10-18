# Evals

Does the skill make an agent's work easier to **revert** and **repeat** for someone who wasn't there? The
evals test that claim directly, with a no-skill baseline, and grade by comparing files, not by judging prose.

## Design

Each scenario runs in three acts:

1. **Work.** An agent does a realistic multi-file task, once with the skill and once without (same prompt,
   including "I may want to roll this back, and I'll want to do it again", so the baseline is invited to leave
   records too).
2. **Cold revert.** A *fresh* agent, with no skill and no memory of act 1, is told to undo the earlier session's
   work using whatever it finds in the folder.
3. **Cold replay** (scenario 1). Another fresh agent is told to do the same job for a different environment.

Cold agents get only the folder the earlier agent left behind: that is the point. The folders are passed under
neutral names (`A`/`B`), so nothing in a path says which run had the skill.

| scenario | fixture | what makes it hard |
|---|---|---|
| 1. `acme-api` staging (evals 1-3) | a git repo; `.env*` is git-ignored | changes git can't see (`.env.staging`); a rename; an unrequested but necessary `server.js` edit |
| 2. `dev-box` machine setup (evals 4-5) | a fake home dir, **not** a git repo | no commit to fall back on; files outside the project; an irreversible "send notice" step; and a **human edits two of the touched files after the work**, so a naive restore destroys their edits |

## Graders

`grade.py` is the only judge, so both configurations are scored identically. Each expectation is a file
comparison (byte-for-byte restore, a value in a config, a file's absence); the one text check (does the cold
agent's report admit the sent notice can't be undone?) is a keyword test. The graders were validated first
against hand-built correct and incorrect solutions.

```bash
bash evals/setup_fixture.sh <dir>                  # scenario 1 project
python3 evals/grade.py work   <dir> --env staging  # eval 1
python3 evals/grade.py revert <dir>                # eval 2
python3 evals/grade.py work   <dir> --env production --forbid staging   # eval 3

bash evals/setup_box.sh <dir>                      # scenario 2 box
python3 evals/grade.py box-work   <dir>            # eval 4
python3 evals/grade.py box-revert <dir> --response <agent-report.txt>   # eval 5
```

`evals.json` holds the prompts in the skill-creator format; `trigger-queries.json` is a starter set of 20
realistic prompts (10 that should trigger the skill, 10 near-misses that shouldn't) for tuning the skill's
`description`. It has not been run through an automated optimiser.

## Results

See [results.md](results.md).
