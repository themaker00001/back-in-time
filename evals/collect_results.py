#!/usr/bin/env python3
"""Collect graded eval runs from a workspace into a markdown table (and optionally JSON).

    python3 evals/collect_results.py back-in-time-workspace [--json results.json]

Reads  <workspace>/iteration-N/eval-*/<config>/run-*/{grading.json,timing.json}  and prints one row per
eval with the skill on the left and the no-skill baseline on the right. Nothing is hand-copied: the numbers
are the graders' and the harness's. Missing tokens/time show as n/a (never as 0), and a wall-clock that was
inflated by rate limiting is marked and left out of the comparison.
"""
import argparse
import json
import sys
from pathlib import Path

SKILL = ("with_skill", "with_skill_available")


def load(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def collect(ws):
    rows = {}
    for g in sorted(ws.glob("iteration-*/eval-*/*/run-*/grading.json")):
        run, cfg, ev, it = g.parent, g.parent.parent.name, g.parent.parent.parent.name, g.parent.parent.parent.parent.name
        grade, timing = load(g), load(run / "timing.json") or {}
        if not grade:
            continue
        side = "skill" if cfg in SKILL else "baseline"
        s = grade["summary"]
        rows.setdefault((it, ev), {})[side] = {
            "config": cfg, "passed": s["passed"], "total": s["total"],
            "failed": [e["text"] for e in grade["expectations"] if not e["passed"]],
            "tokens": timing.get("total_tokens"), "tool_uses": timing.get("tool_uses"),
            "seconds": None if timing.get("duration_unreliable") else timing.get("total_duration_seconds"),
            "duration_unreliable": bool(timing.get("duration_unreliable")),
        }
    return rows


def fmt(side):
    if not side:
        return "n/a | n/a | n/a"
    k = lambda v: "n/a" if v is None else ("{:,}".format(v) if isinstance(v, int) else "%.0f s" % v)
    return "%d/%d | %s | %s" % (side["passed"], side["total"], k(side["tokens"]), k(side["tool_uses"]))


def table(rows):
    out = ["| run | eval | skill: pass | tokens | tool calls | baseline: pass | tokens | tool calls |",
           "|---|---|---|---|---|---|---|---|"]
    for (it, ev), sides in sorted(rows.items()):
        out.append("| %s | %s | %s | %s |" % (it.replace("iteration-", "it. "), ev.replace("eval-", "").replace("-", " "),
                                           fmt(sides.get("skill")), fmt(sides.get("baseline"))))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = collect(Path(a.workspace))
    if not rows:
        print("no graded runs found under %s" % a.workspace, file=sys.stderr)
        return 1
    print(table(rows))
    if a.json:
        Path(a.json).write_text(json.dumps({"%s/%s" % k: v for k, v in sorted(rows.items())}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
