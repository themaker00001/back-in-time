#!/usr/bin/env python3
"""bit - Back in Time: a tiny ledger that makes agent work replayable and reversible.

A *timeline* is a plan (steps, each with do / undo / verify) plus an append-only
journal of what actually happened. Everything lives in `.back-in-time/<timeline>/`
next to the work, so an agent (or a person) days later can read it cold, revert it,
or replay it. Single file, Python 3.9+ standard library only.

    bit.py init "Title" --goal "..."     start a timeline
    bit.py step s1 --do ... --undo ...   add / edit a plan step
    bit.py check                         lint the plan (are undos defined? anything irreversible?)
    bit.py next | do | begin | done      work through the plan
    bit.py undo [STEP|--all] [--yes]     revert (dry run unless --yes); --merge keeps later edits; `reverted` logs a by-hand undo
    bit.py replay [--set k=v] [--auto]   run the whole plan again as a new run
    bit.py fork DIR [--set k=v]          same plan in another checkout/environment, fresh history
    bit.py verify                        is the world still as the trace says?
    bit.py status | list | use | set | note

Run `bit.py <command> --help` for details.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX only; without it, concurrent bit processes are simply not serialised
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

VERSION = "0.2.0"
DIRNAME = ".back-in-time"
MAX_DIFF_LINES = 1500
OUT_TAIL = 1500  # chars of command output kept in the journal
TIMEOUT = int(os.environ.get("BIT_TIMEOUT", "900"))
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SET_RE = re.compile(r"^BIT_SET[ \t]+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.M)
SAFE_SET_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./~-]{0,500}$")  # no whitespace, quotes, $, backticks, ; | & < > ( ) \
LIVE = ("applied", "failed", "running")  # step states whose effects may be in the world


def max_snap_bytes():
    """Per-path snapshot limit. Over it, `begin`/`do` refuse (fail closed) rather than record a step as reversible when it isn't."""
    return int(float(os.environ.get("BIT_MAX_SNAPSHOT_MB", "50")) * 1024 * 1024)


class BitError(Exception):
    def __init__(self, msg, code=2):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- utils

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].strip("-") or "timeline"


def write_atomic(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        try:  # mkstemp makes 0600 files; keep the target's mode, or the usual umask-based mode for a new file
            mode = stat.S_IMODE(os.stat(str(path)).st_mode)
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_KW = (r"(?:password|passwd|passphrase|secret[_-]?key|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|"
       r"signing[_-]?key|encryption[_-]?key|credentials?|"
       r"pgpass(?:word)?|(?<![A-Za-z])pass)")
_NOT_A_REF = r"(?!\[REDACTED|[\"']?\\?\$|[\"']?\{\{)"  # $VAR, \$VAR and {{var}} are references, not secrets
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED JWT]"),
    # scheme://user:password@host
    (re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:)(?!\$|\{\{|\\\$)[^\s/@]{3,}(@)"), r"\1[REDACTED]\2"),
    # Authorization: Basic|Bearer|Token|Digest <credential>, and a bare Bearer token
    (re.compile(r"(?i)\b(Authorization\s*[:=]\s*(?:Basic|Bearer|Token|Digest)\s+)[A-Za-z0-9._~+/=\-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._\-]{16,}"), r"\1 [REDACTED]"),
    # curl -u user:password
    (re.compile(r"(?i)((?:^|\s)-u\s+)(?!\$|\{\{|\\\$)[^\s:$]+:(?!\$|\{\{)\S{4,}"), r"\1[REDACTED]"),
    # --password VALUE / --token=VALUE style flags (--token-limit and --max-tokens are not matched)
    (re.compile(r"(?i)(--(?:password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)(?:=|\s+))"
                + _NOT_A_REF + r"(\"[^\"]{4,}\"|'[^']{4,}'|[^\s\"']{4,})"), r"\1[REDACTED]"),
    # name = value / "name": "value", where the keyword ENDS the identifier (token= and DB_PASS= match; max_tokens=,
    # token_limit=, password_policy= and bypass= are ordinary settings) and the literal value is 6+ characters
    (re.compile(r"(?i)(" + _KW + r"[\"']?\s*[=:]\s*)" + _NOT_A_REF + r"(\"[^\"]{6,}\"|'[^']{6,}'|[^\s\"']{6,})"), r"\1[REDACTED]"),
]


def redact(text):
    """Scrub obvious secrets before anything is written to the (committable) journal."""
    if not text:
        return text
    for rx, rep in _SECRETS:
        text = rx.sub(rep, text)
    return text


def has_secret(text):
    return bool(text) and redact(text) != text


def refuse_secret(what, text):
    """The ledger is meant to be committed and handed around: never store a secret in it, refuse instead.
    BIT_ALLOW_SECRET_LOOKING=1 is the deliberate override for a false positive (e.g. prose that mentions `token: ...`)."""
    if has_secret(text) and os.environ.get("BIT_ALLOW_SECRET_LOOKING") != "1":
        raise BitError("%s looks like it contains a secret, and the plan/journal/TIMELINE.md are meant to be committed. "
                       "Pass it through an environment variable instead (reference it as \"$NAME\" in a command) and "
                       "keep the value out of the ledger. (If this is a false positive, re-run with "
                       "BIT_ALLOW_SECRET_LOOKING=1.)" % what)


def tail(text, n=OUT_TAIL):
    text = redact((text or "").rstrip())
    return text if len(text) <= n else "..." + text[-n:]


def print_tail(text, lines):
    rows = (text or "").rstrip().splitlines()
    for row in rows[-lines:]:
        print("    | " + redact(row))


# --------------------------------------------------------------------------- locating things

def find_root(explicit=None, for_init=False):
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path(__file__).resolve().parent
    if here.name == DIRNAME:  # the self-contained copy lives inside the ledger
        return here.parent
    env = os.environ.get("BIT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cur = Path.cwd().resolve()
    if for_init:
        return cur
    for p in [cur] + list(cur.parents):
        if (p / DIRNAME).is_dir():
            return p
    raise BitError("no %s/ found here or in any parent directory. Run `bit.py init \"Title\"` "
                   "in the project root first." % DIRNAME)


def list_timelines(root):
    bd = root / DIRNAME
    if not bd.is_dir():
        return []
    return sorted(d.name for d in bd.iterdir() if (d / "plan.json").exists())


def pick_timeline(root, want=None):
    slugs = list_timelines(root)
    if want:
        if want in slugs:
            return want
        hits = [s for s in slugs if want in s]
        if len(hits) == 1:
            return hits[0]
        raise BitError("timeline %r not found (or ambiguous). Have: %s" % (want, ", ".join(slugs) or "none"))
    cur_file = root / DIRNAME / "CURRENT"
    if cur_file.exists():
        cur = cur_file.read_text().strip()
        if cur in slugs:
            return cur
    if len(slugs) == 1:
        return slugs[0]
    if slugs:
        raise BitError("several timelines exist (%s). Pass --tl SLUG or run `bit.py use SLUG`." % ", ".join(slugs))
    raise BitError("no timelines yet. Run `bit.py init \"Title\"`.")


def acquire_lock(tl):
    """One mutating bit command at a time per timeline (plan.json and the journal are read-modify-write).
    The flock is released when the process exits, so a crash can't leave a stale lock."""
    if fcntl is None or os.environ.get("BIT_LOCK_TL") == str(tl):
        return None  # a step that runs bit itself: the parent already holds the lock and is waiting for us
    fd = os.open(str(tl / ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("waiting for another bit command on this timeline to finish...", file=sys.stderr)
        fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class Ctx:
    def __init__(self, args, lock=False):
        self.root = find_root(getattr(args, "root", None))
        self.slug = pick_timeline(self.root, getattr(args, "tl", None))
        self.tl = self.root / DIRNAME / self.slug
        self.bd = self.root / DIRNAME
        self._lock_fd = acquire_lock(self.tl) if lock else None

    @property
    def tool_rel(self):
        """For documents (TIMELINE.md): relative to the project root."""
        return "python3 %s/bit.py --tl %s" % (DIRNAME, self.slug)

    @property
    def tool(self):
        """For hints printed right now: a path that works from the current directory."""
        try:
            at_root = Path.cwd().resolve() == self.root
        except OSError:
            at_root = False
        path = "%s/bit.py" % DIRNAME if at_root else shlex.quote(str(self.bd / "bit.py"))
        return "python3 %s --tl %s" % (path, self.slug)


# --------------------------------------------------------------------------- plan + journal

def load_plan(ctx):
    try:
        return json.loads((ctx.tl / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BitError("cannot read plan.json: %s" % e)


def save_plan(ctx, plan):
    write_atomic(ctx.tl / "plan.json", json.dumps(plan, indent=2, ensure_ascii=False) + "\n")


def read_events(ctx):
    path = ctx.tl / "journal.jsonl"
    events = []
    if path.exists():
        # split on "\n" only: str.splitlines() also splits on U+2028/2029/0085, which would silently drop events
        for line in path.read_bytes().decode("utf-8", "replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # a torn line must not make the whole ledger unreadable
    return events


def emit(ctx, run, event, **fields):
    rec = {"ts": now(), "run": run, "event": event}
    rec.update(fields)
    path = ctx.tl / "journal.jsonl"
    prefix = b""
    if path.exists() and path.stat().st_size > 0:
        with open(str(path), "rb") as f:  # a torn last line must not swallow the event we are about to write
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                prefix = b"\n"
    with open(str(path), "ab") as f:
        f.write(prefix + (json.dumps(rec, ensure_ascii=True) + "\n").encode("ascii"))
    return rec


def get_step(plan, sid):
    for s in plan["steps"]:
        if s["id"] == sid:
            return s
    raise BitError("no step %r in the plan. Steps: %s" % (sid, ", ".join(s["id"] for s in plan["steps"]) or "none"))


def eff_reversible(step):
    r = step.get("reversible")
    if r in ("yes", "partial", "no"):
        return r
    return "yes" if (step.get("undo") or step.get("touches") or step.get("undo_manual")) else "unknown"


def _fold_run(events, plan, target):
    st = {"run": target, "kind": "original", "steps": {}, "order": [], "vars": dict(plan.get("params", {})),
          "protected": set(plan.get("params", {})), "last_ts": None,
          "runs": [e for e in events if e.get("event") == "run_started"]}
    for s in plan["steps"]:
        st["steps"][s["id"]] = {"status": "pending", "begin": None, "done": None, "reverted": None,
                                "revert_failed": None, "blocked": None}
    for e in events:
        if e.get("run") != target:
            continue
        st["last_ts"] = e.get("ts")
        ev = e.get("event")
        sid = e.get("step")
        if ev == "run_started":
            st["kind"] = e.get("kind", "original")
            st["vars"].update(e.get("set") or {})
            st["protected"].update((e.get("set") or {}).keys())
        elif ev == "param_set":
            st["vars"][e["key"]] = e["value"]
            if e.get("source") == "cli":
                st["protected"].add(e["key"])
        elif sid in st["steps"]:
            s = st["steps"][sid]
            if ev == "step_begin":
                s["status"], s["begin"], s["reverted"], s["done"] = "running", e, None, None
                if sid in st["order"]:
                    st["order"].remove(sid)
                st["order"].append(sid)
            elif ev == "step_done":
                s["status"], s["done"] = e["status"], e
                if sid not in st["order"] and e["status"] != "skipped":
                    st["order"].append(sid)
            elif ev == "step_reverted":
                if e.get("ok"):
                    s["status"], s["reverted"], s["revert_failed"] = "reverted", e, None
                else:
                    s["revert_failed"] = e
            elif ev == "step_blocked":
                s["blocked"] = e
    return st


def orphans(events, plan):
    """(run, step, status) for steps with live effects in the journal that are no longer in plan.json."""
    ids = {s["id"] for s in plan["steps"]}
    state = {}
    for e in events:
        sid, ev = e.get("step"), e.get("event")
        if not sid or sid in ids:
            continue
        k = (e.get("run"), sid)
        if ev == "step_begin":
            state[k] = "running"
        elif ev == "step_done":
            state[k] = e.get("status")
        elif ev == "step_reverted" and e.get("ok"):
            state[k] = "reverted"
    return [(r, sid, status) for (r, sid), status in sorted(state.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1]))
            if status in LIVE]


def fold(events, plan, run=None):
    """Derive the state of one run (default: the latest) from the journal. `earlier` lists older runs that still have
    live (applied/failed/half-done) steps, because a replay starts a clean slate in the journal, not in the world."""
    runs_seen = sorted({e["run"] for e in events if isinstance(e.get("run"), int)})
    target = run if run is not None else (runs_seen[-1] if runs_seen else 1)
    st = _fold_run(events, plan, target)
    st["earlier"], st["earlier_status"] = {}, {}
    if run is None:
        for r in runs_seen:
            if r < target:
                o = _fold_run(events, plan, r)
                live = [x for x in o["order"] if o["steps"][x]["status"] in LIVE]
                if live:
                    st["earlier"][r] = live
                    st["earlier_status"][r] = {x: o["steps"][x]["status"] for x in live}
    st["orphans"] = orphans(events, plan)
    return st


def render_cmd(cmd, variables):
    def rep(m):
        k = m.group(1)
        if k not in variables:
            raise BitError("undefined variable {{%s}}. Define it with `bit.py set %s=...`, `replay --set %s=...`, "
                           "or have an earlier step print `BIT_SET %s=...`." % (k, k, k, k))
        return str(variables[k])
    return VAR_RE.sub(rep, cmd)


def stitle(step, variables):
    return soft_render(step.get("title", step["id"]), variables)


def soft_render(text, variables):
    if not text:
        return text
    return VAR_RE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)


def env_for(ctx, run, sid, variables):
    env = {"BIT_ROOT": str(ctx.root), "BIT_RUN": str(run), "BIT_STEP": sid or "", "BIT_LOCK_TL": str(ctx.tl)}
    for k, v in variables.items():
        env["BIT_VAR_%s" % k] = str(v)
    return env


def _kill_group(p):
    """p runs in its own session, so its pid is also its process-group id. Signal the group by that id rather than
    asking the OS for the leader's group: the leader (the shell) may already have exited while a background job it
    started still holds our output pipe. Returns whether a group was actually signalled."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except (OSError, AttributeError):
        try:
            p.kill()
        except OSError:
            pass
        return False


def run_shell(ctx, cmd, env_extra, timeout=None):
    """Run a shell command in its own process group. Normal exit leaves background jobs alone (recipes rely on
    that); a timeout or Ctrl-C kills the whole group so nothing keeps changing the world after bit has stopped
    recording it."""
    env = dict(os.environ)
    env.update(env_extra)
    t0 = time.time()
    p = subprocess.Popen(cmd, shell=True, cwd=str(ctx.root), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout or TIMEOUT)
        return p.returncode, out.decode("utf-8", "replace"), round(time.time() - t0, 2)
    except subprocess.TimeoutExpired:
        killed = _kill_group(p)
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b""
        text = out.decode("utf-8", "replace") + ("\n[bit: timed out after %ss; killed its process group]" if killed
                                                else "\n[bit: timed out after %ss]") % (timeout or TIMEOUT)
        return 124, text, round(time.time() - t0, 2)
    except BaseException:
        _kill_group(p)
        raise


# --------------------------------------------------------------------------- file state + snapshots

def sha_file(p):
    h = hashlib.sha256()
    with open(str(p), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_special(p):
    """A FIFO, socket or device. Opening a FIFO blocks forever, and none of them can be copied back faithfully, so
    they are neither hashed, snapshotted nor restored: bit leaves them alone."""
    try:
        m = os.lstat(str(p)).st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(m) or stat.S_ISDIR(m) or stat.S_ISLNK(m))


def ignore_special(dirpath, names):
    return [n for n in names if is_special(os.path.join(dirpath, n))]


def dir_map(p):
    out = {}
    for dp, dns, fns in os.walk(str(p)):
        dns.sort()
        for n in sorted(fns):
            full = os.path.join(dp, n)
            if is_special(full):
                continue
            rel = os.path.relpath(full, str(p)).replace(os.sep, "/")
            out[rel] = ("link:" + os.readlink(full)) if os.path.islink(full) else sha_file(full)[:12]
    return out


def dir_size(p):
    total = 0
    for dp, _, fns in os.walk(str(p)):
        for n in fns:
            try:
                m = os.lstat(os.path.join(dp, n))
                if stat.S_ISREG(m.st_mode):
                    total += m.st_size
            except OSError:
                pass
    return total


def state_of(p):
    p = str(p)
    if os.path.islink(p):
        return {"s": "symlink", "v": os.readlink(p)}
    if not os.path.exists(p):
        return {"s": "absent"}
    if os.path.isfile(p):
        return {"s": "file", "v": sha_file(p)[:12]}
    if os.path.isdir(p):
        h = hashlib.sha256()
        for k, v in sorted(dir_map(p).items()):
            h.update(("%s=%s\n" % (k, v)).encode())
        return {"s": "dir", "v": h.hexdigest()[:12]}
    return {"s": "other"}


def short(st):
    return st["s"] if "v" not in st else "%s:%s" % (st["s"], st["v"])


def resolve_touch(root, raw):
    ap = os.path.abspath(os.path.join(str(root), os.path.expanduser(raw)))
    rel = os.path.relpath(ap, str(root))
    if rel == ".":
        raise BitError("refusing to treat the whole project root as a touched path; list specific files/dirs")
    if rel == DIRNAME or rel.startswith(DIRNAME + os.sep):
        raise BitError("%s is the ledger itself and can't be listed in --touches" % DIRNAME)
    key = ap if rel.startswith("..") else rel.replace(os.sep, "/")
    return Path(ap), key


def abs_from_key(root, key):
    return Path(key) if os.path.isabs(key) else Path(os.path.join(str(root), key))


def snap_root(ctx, run, sid):
    return ctx.tl / "snapshots" / ("r%s-%s" % (run, sid))


def snap_file(ctx, run, sid, key):
    stored = ("__abs__/" + key.lstrip("/")) if os.path.isabs(key) else key
    return snap_root(ctx, run, sid) / "files" / stored


def expand_touches(root, touches):
    """(abs path, key) for every touched path. A symlink also pulls in what it points at: edits made *through* a
    link (dotfile managers!) change the target, not the link, so tracking only the link would miss them."""
    out, seen = [], set()
    for raw in touches:
        ap, key = resolve_touch(root, raw)
        if key not in seen:
            seen.add(key)
            out.append((ap, key))
        if os.path.islink(str(ap)):
            real = os.path.realpath(str(ap))
            if os.path.exists(real) and os.path.abspath(real) != str(ap):
                rap, rkey = resolve_touch(root, real)
                if rkey not in seen:
                    seen.add(rkey)
                    out.append((rap, rkey))
    return out


def check_snapshot_limit(ap, key):
    st = state_of(ap)
    if st["s"] not in ("file", "dir"):
        return
    size = dir_size(ap) if st["s"] == "dir" else os.path.getsize(str(ap))
    limit = max_snap_bytes()
    if size > limit:
        raise BitError("touched %s %s is %.1f MB, over the %g MB snapshot limit, so `undo` could not restore it. Nothing was "
                       "run. Narrow --touches to what the step really changes, raise BIT_MAX_SNAPSHOT_MB, or make the step "
                       "reversible another way (e.g. move it into a trash folder and `--undo` the move)."
                       % ("directory" if st["s"] == "dir" else "file", key, size / 1048576.0, limit / 1048576.0))


def snapshot_one(ctx, run, sid, ap, key, late):
    st = state_of(ap)
    entry = {"kind": st["s"], "state": short(st)}
    if late:
        entry["late"] = True
    dest = snap_file(ctx, run, sid, key)
    if st["s"] == "file":
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(ap), str(dest))
    elif st["s"] == "dir":
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(ap), str(dest), symlinks=True, ignore=ignore_special)
    elif st["s"] == "absent":
        missing, d = [], os.path.dirname(str(ap))
        while d and not os.path.lexists(d):  # parent directories this step may create: undo removes them again if empty
            missing.append(d)
            d = os.path.dirname(d)
        if missing:
            entry["missing_parents"] = missing  # deepest first
    return entry


def take_snapshot(ctx, run, sid, touches):
    """Copy the before-state of every touched path. Per (run, step) the first snapshot is the truth, so a retry after
    a half-finished attempt can't overwrite it; paths added to --touches since are snapshotted now (flagged `late`)."""
    if not touches:
        return {}, False
    keys = expand_touches(ctx.root, touches)
    sd = snap_root(ctx, run, sid)
    mf = sd / "manifest.json"
    manifest = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    reused = bool(manifest)
    todo = [(ap, key) for ap, key in keys if key not in manifest]
    for ap, key in todo:  # check every limit before copying anything, so a refusal leaves no half-made snapshot
        check_snapshot_limit(ap, key)
    for ap, key in todo:
        manifest[key] = snapshot_one(ctx, run, sid, ap, key, late=reused)
    if todo:
        sd.mkdir(parents=True, exist_ok=True)
        write_atomic(mf, json.dumps(manifest, indent=2) + "\n")
    return manifest, reused


def retire_snapshot(ctx, run, sid):
    """After a successful revert the before-state is spent: set it aside so a re-run of the step takes a fresh one."""
    sd = snap_root(ctx, run, sid)
    if not sd.exists():
        return
    n = 1
    while (sd.with_name("%s.reverted-%d" % (sd.name, n))).exists():
        n += 1
    os.replace(str(sd), str(sd.with_name("%s.reverted-%d" % (sd.name, n))))


def snapshot_problem(ctx, run, sid, manifest, expected):
    """Why `undo` can't trust the stored before-state, or None."""
    if manifest is None:
        return "its snapshot is missing" if expected else None
    for key in expected:
        if key not in manifest:
            return "the snapshot has no entry for %s" % key
    for key, e in manifest.items():
        if e.get("unsnapshotted"):
            return "the before-state of %s was too large to snapshot" % key
        src = snap_file(ctx, run, sid, key)
        if e["kind"] == "file" and not src.is_file():
            return "the stored copy of %s is missing" % key
        if e["kind"] == "dir" and not src.is_dir():
            return "the stored copy of %s is missing" % key
        if e["kind"] in ("file", "dir") and short(state_of(src)) != e["state"]:
            return "the stored copy of %s is corrupt or incomplete (it no longer matches the hash recorded when it was taken)" % key
    return None


def load_manifest(ctx, run, sid):
    mf = snap_root(ctx, run, sid) / "manifest.json"
    return json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else None


def remove_path(p):
    p = str(p)
    if os.path.islink(p) or os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p)


def remove_created_parents(entry):
    """The step created these directories (they did not exist at snapshot time): take them away again if empty."""
    for d in entry.get("missing_parents") or []:
        try:
            os.rmdir(d)
        except OSError:
            pass


def clear_dir_keep_special(dst):
    """Empty a directory of everything bit tracks, leaving FIFOs/sockets (and the directories holding them) alone."""
    for dp, dns, fns in os.walk(dst, topdown=False):
        for n in fns:
            full = os.path.join(dp, n)
            if not is_special(full):
                os.remove(full)
        for n in dns:
            full = os.path.join(dp, n)
            if os.path.islink(full):
                os.remove(full)
            else:
                try:
                    os.rmdir(full)
                except OSError:
                    pass  # not empty: it holds something we leave alone


def restore_path(ctx, run, sid, key, entry):
    ap = abs_from_key(ctx.root, key)
    kind = entry["kind"]
    src = snap_file(ctx, run, sid, key)
    if kind == "dir" and ap.is_dir() and not ap.is_symlink():
        clear_dir_keep_special(str(ap))
        shutil.copytree(str(src), str(ap), symlinks=True, dirs_exist_ok=True, ignore=ignore_special)
        return
    remove_path(ap)
    if kind == "absent":
        remove_created_parents(entry)
        return
    if kind == "file":
        ap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(ap))
    elif kind == "dir":
        ap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(ap), symlinks=True, ignore=ignore_special)
    elif kind == "symlink":
        ap.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(entry["state"].split(":", 1)[1], str(ap))


def read_small_text(p):
    try:
        if os.path.getsize(str(p)) > 200 * 1024:
            return None
        with open(str(p), "r", encoding="utf-8", newline="") as f:  # newline="": keep \r\n as it is
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def text_diff(a, b, fa, fb):
    out = []
    for line in difflib.unified_diff(a.splitlines(True), b.splitlines(True), fa, fb):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def make_hunks(a_text, b_text):
    """Reversible hunks: each one says 'this block of lines (with context) replaced that block', so a later edit that
    doesn't touch the block can be kept while the block is put back."""
    a, b = a_text.splitlines(True), b_text.splitlines(True)
    hunks = []
    for g in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(3):
        i1, i2, j1, j2 = g[0][1], g[-1][2], g[0][3], g[-1][4]
        hunks.append({"at": j1, "before": a[i1:i2], "after": b[j1:j2]})
    return hunks


def load_hunks(ctx, run, sid):
    p = ctx.tl / "diffs" / ("r%s-%s.hunks.json" % (run, sid))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_diff(ctx, run, sid, manifest):
    chunks = []
    hunks_doc = {}
    for key, entry in manifest.items():
        ap = abs_from_key(ctx.root, key)
        cur = state_of(ap)
        if entry["state"] == short(cur):
            continue
        if entry.get("unsnapshotted"):
            chunks.append("# %s: changed (before-state was too large to snapshot)" % key)
            continue
        bk, ck = entry["kind"], cur["s"]
        src = snap_file(ctx, run, sid, key)
        if bk in ("file", "absent") and ck in ("file", "absent"):
            a = read_small_text(src) if bk == "file" else ""
            b = read_small_text(ap) if ck == "file" else ""
            if a is None or b is None:
                chunks.append("# %s: binary or large file changed (%s -> %s)" % (key, entry["state"], short(cur)))
                continue
            d = text_diff(a, b, "a/" + key if bk == "file" else "/dev/null", "b/" + key if ck == "file" else "/dev/null")
            chunks.append(redact(d) if d else "# %s: content identical, metadata changed" % key)
            if d and ck == "file":
                hunks_doc[key] = {"created": bk == "absent", "hunks": make_hunks(a, b)}
        elif "dir" in (bk, ck):
            before = dir_map(src) if bk == "dir" and src.exists() else {}
            after = dir_map(ap) if ck == "dir" else {}
            rows = ["# %s/ (directory)" % key]
            rows += ["A %s" % f for f in sorted(set(after) - set(before))]
            rows += ["D %s" % f for f in sorted(set(before) - set(after))]
            rows += ["M %s" % f for f in sorted(f for f in set(before) & set(after) if before[f] != after[f])]
            chunks.append("\n".join(rows))
        else:
            chunks.append("# %s: %s -> %s" % (key, entry["state"], short(cur)))
    if not chunks:
        return None
    lines = "\n".join(chunks).splitlines()
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + ["# ... diff truncated at %d lines" % MAX_DIFF_LINES]
    (ctx.tl / "diffs").mkdir(exist_ok=True)
    rel = "diffs/r%s-%s.diff" % (run, sid)
    write_atomic(ctx.tl / rel, "\n".join(lines) + "\n")
    if hunks_doc:
        write_atomic(ctx.tl / ("diffs/r%s-%s.hunks.json" % (run, sid)), json.dumps(hunks_doc))
    return rel


def git_head(root):
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        return p.stdout.decode().strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_commits_between(root, a, b):
    try:
        p = subprocess.run(["git", "-C", str(root), "log", "--oneline", "%s..%s" % (a, b)], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        return [redact(line) for line in p.stdout.decode("utf-8", "replace").strip().splitlines()[:10]]
    except (OSError, subprocess.TimeoutExpired):
        return []


# --------------------------------------------------------------------------- step lifecycle

def gate(step, approved):
    if eff_reversible(step) == "no" and not approved:
        why = step.get("notes") or "no reason recorded"
        raise BitError("step %s is marked IRREVERSIBLE (%s). Get the user's explicit OK first, then re-run "
                       "with --yes (auto: --approve %s)." % (step["id"], why, step["id"]), 3)


def finalize(ctx, run, step, manifest, status, how, git0, **extra):
    sid = step["id"]
    after, changed = {}, []
    for key, entry in (manifest or {}).items():
        cur = short(state_of(abs_from_key(ctx.root, key)))
        after[key] = cur
        if cur != entry["state"]:
            changed.append({"path": key, "before": entry["state"], "after": cur})
    diff = build_diff(ctx, run, sid, manifest) if manifest else None
    git = None
    head1 = git_head(ctx.root)
    if git0 or head1:
        git = {"head_before": git0, "head_after": head1}
        if git0 and head1 and git0 != head1:
            git["commits"] = git_commits_between(ctx.root, git0, head1)
    warnings = None
    if manifest and not changed and status == "applied":
        warnings = ["none of the declared --touches changed (%s). Fine if this step was already applied and is "
                    "a no-op here; otherwise the path is probably wrong (typo? an unexpanded {{var}}?) or the file was "
                    "edited BEFORE `begin` (then the before-state is already gone). Either way `undo` would not restore "
                    "what you think" % ", ".join(manifest)]
    extra = {k: v for k, v in extra.items() if v is not None}
    return emit(ctx, run, "step_done", step=sid, status=status, how=how, changed=changed, after=after,
                diff=diff, git=git, warnings=warnings, **extra)


def begin_step(ctx, run, step, approved, variables):
    sid = step["id"]
    gate(step, approved)
    touches = [render_cmd(t, variables) for t in step.get("touches") or []]
    manifest, reused = take_snapshot(ctx, run, sid, touches)
    late = [k for k, e in manifest.items() if e.get("late")]
    if late:
        print("  note: %s was snapshotted late (this step already ran at least once), so its before-state may already "
              "include partial changes." % ", ".join(late))
    git0 = git_head(ctx.root)
    emit(ctx, run, "step_begin", step=sid, approved=bool(approved) or None, git_head=git0,
         snapshot={k: v["state"] for k, v in manifest.items()} or None, snapshot_reused=reused or None)
    return manifest, git0


def exec_step(ctx, sid, approved=False):
    plan = load_plan(ctx)
    step = get_step(plan, sid)
    st = fold(read_events(ctx), plan)
    run, ss, variables = st["run"], st["steps"][sid], st["vars"]
    if ss["status"] == "applied":
        raise BitError("%s is already applied in run %s. Undo it (`undo %s`) or start a new run (`replay`)."
                       % (sid, run, sid), 2)
    if not step.get("do"):
        print("MANUAL %s: %s" % (sid, step.get("manual") or step.get("title")))
        print("  This step has no `do` command. Run `%s begin %s`, do the work, then `%s done %s --note \"...\"`."
              % (ctx.tool, sid, ctx.tool, sid))
        return 3
    gate(step, approved)
    unfinished = ss["status"] in ("failed", "running") or any(
        o.get(sid) in ("failed", "running") for o in st["earlier_status"].values())
    if step.get("skip_if") and unfinished:
        print("  note: skip_if ignored for %s: it failed or was interrupted before (in this or an earlier run), and its own "
              "partial effects could make skip_if true. Running it again to completion." % sid)
    skip_cmd = render_cmd(step["skip_if"], variables) if step.get("skip_if") and not unfinished else None
    pre_cmd = render_cmd(step["pre"], variables) if step.get("pre") else None
    do_cmd = render_cmd(step["do"], variables)
    for t in step.get("touches") or []:  # render now: an undefined {{var}} must stop the step before anything has run
        render_cmd(t, variables)
    env = env_for(ctx, run, sid, variables)

    if skip_cmd:
        code, out, secs = run_shell(ctx, skip_cmd, env)
        if code == 0:
            emit(ctx, run, "step_done", step=sid, status="skipped", how="skip_if", cmd=redact(skip_cmd),
                 note="already satisfied (skip_if exited 0)")
            print("SKIP %s: already satisfied (skip_if)" % sid)
            return 0
    if pre_cmd:
        code, out, secs = run_shell(ctx, pre_cmd, env)
        if code != 0:
            emit(ctx, run, "step_blocked", step=sid, cmd=redact(pre_cmd), exit=code, output=tail(out),
                 reason="precondition failed")
            print("BLOCKED %s: precondition failed (exit %s): %s" % (sid, code, redact(pre_cmd)))
            print_tail(out, 15)
            print("  The world differs from what this plan assumed. Fix that, or edit the plan, before continuing.")
            return 1

    manifest, git0 = begin_step(ctx, run, step, approved, variables)
    code, out, secs = run_shell(ctx, do_cmd, env)
    accept_bit_sets(ctx, run, sid, st["protected"], variables, out)
    ok = code == 0
    verify = None
    if ok and step.get("verify"):
        try:
            vcmd = render_cmd(step["verify"], variables)
            vcode, vout, vsecs = run_shell(ctx, vcmd, env)
        except BitError as e:
            vcmd, vcode, vout, vsecs = step["verify"], 2, str(e), 0
        verify = {"cmd": redact(vcmd), "exit": vcode, "output": tail(vout, 600), "secs": vsecs}
        ok = vcode == 0
    ev = finalize(ctx, run, step, manifest, "applied" if ok else "failed", "do", git0, cmd=redact(do_cmd),
                  exit=code, secs=secs, output=tail(out), verify=verify)
    report_done(sid, step, ev, out, verify, variables)
    return 0 if ok else 1


def accept_bit_sets(ctx, run, sid, protected, variables, out):
    """`BIT_SET name=value` lines let a step announce what it discovered (an id, a SHA). Output can come from tools we
    don't control and values are pasted into later shell commands, so accept only plainly safe values and never let
    output override a declared parameter or something the user set."""
    for k, v in SET_RE.findall(out):
        v = v.strip()
        why = None
        if k in protected:
            why = "it would override a declared parameter or a value you set yourself"
        elif not SAFE_SET_VALUE.match(v):
            why = ("the value has characters unsafe to paste into a shell command (allowed: letters, digits and "
                   "_ @ %% + = : , . / ~ -, max 500). Set it deliberately with `bit.py set %s=...` if you mean it" % k)
        elif has_secret("%s=%s" % (k, v)):
            why = "it looks like a secret"
        if why:
            print("  WARNING: ignored `BIT_SET %s=...`: %s" % (k, why))
            emit(ctx, run, "note", step=sid, text="ignored BIT_SET %s: %s" % (k, why.split(". ")[0].split(" (")[0]))
            continue
        variables[k] = v
        emit(ctx, run, "param_set", key=k, value=v, source="step:%s" % sid)


def report_done(sid, step, ev, out, verify, variables):
    title = stitle(step, variables)
    if ev["status"] == "applied":
        print("OK   %s: %s (%.1fs)" % (sid, title, ev.get("secs") or 0))
        print_tail(out, 6)
    else:
        why = "verify failed" if verify and verify["exit"] != 0 else "exit %s" % ev.get("exit")
        print("FAIL %s: %s (%s)" % (sid, title, why))
        print_tail(out, 25)
        if verify and verify["exit"] != 0:
            print("  verify: %s" % verify["cmd"])
            print_tail(verify["output"], 10)
    for c in ev.get("changed") or []:
        print("  changed: %s  (%s -> %s)" % (c["path"], c["before"], c["after"]))
    if ev.get("diff"):
        print("  diff saved: .back-in-time/.../%s" % ev["diff"])
    for w in ev.get("warnings") or []:
        print("  WARNING: " + w)
    if ev["status"] == "failed":
        print("  Partial effects may exist. Fix and `do %s` again, or `undo %s` to revert it." % (sid, sid))


# --------------------------------------------------------------------------- docs (TIMELINE.md)

def next_step(plan, st):
    steps = plan["steps"]
    started = [s for s in steps if st["steps"][s["id"]]["status"] in ("applied", "running", "failed")]
    reverted = [s for s in steps if st["steps"][s["id"]]["status"] == "reverted"]
    if not started and reverted:
        return None, "all-reverted"
    for s in steps:
        status = st["steps"][s["id"]]["status"]
        if status in ("running", "failed"):
            return s, status
    for s in steps:
        status = st["steps"][s["id"]]["status"]
        if status in ("pending", "reverted"):
            return s, status
    return None, "complete"


def hazards(plan, st):
    out = []
    for r, sids in sorted((st.get("earlier") or {}).items()):
        out.append("run %d has steps that are still applied or half-done (%s): their effects are still in the world. "
                   "`undo --all --run %d` reverts them." % (r, ", ".join(sids), r))
    for r, sid, status in st.get("orphans") or []:
        out.append("step `%s` is no longer in the plan but is still %s in run %d: bit cannot revert it automatically. "
                   "Put it back in plan.json or undo its effects by hand." % (sid, status, r))
    for s in plan["steps"]:
        ss = st["steps"][s["id"]]
        status = ss["status"]
        if status == "failed":
            out.append("`%s` FAILED and may have partial effects: fix and re-run it, or undo it." % s["id"])
        if status == "running":
            out.append("`%s` was begun but never finished (crash?). Finish it with `done`, or undo it." % s["id"])
        if ss["revert_failed"]:
            out.append("`%s` undo FAILED earlier: the world may be half-reverted. Inspect before retrying." % s["id"])
        if status in ("applied", "running", "failed"):
            if eff_reversible(s) == "no":
                out.append("`%s` is IRREVERSIBLE and has been applied: %s" % (s["id"], s.get("notes") or "see plan"))
            elif eff_reversible(s) == "unknown":
                out.append("`%s` has no undo and no touched paths: it cannot be reverted automatically." % s["id"])
    return out


FIELDS = (("manual", "manual"), ("skip if", "skip_if"), ("precondition", "pre"), ("do", "do"),
          ("verify", "verify"), ("undo", "undo"), ("undo by hand", "undo_manual"))


def esc(s):
    return (s or "").replace("|", "\\|").replace("\n", " ")


def event_line(e):
    ev = e.get("event")
    sid = e.get("step", "")
    if ev == "run_started":
        return "run %s started (%s)" % (e["run"], e.get("kind"))
    if ev == "step_begin":
        return "%s begun" % sid
    if ev == "step_done":
        return "%s %s%s" % (sid, e.get("status"), (" (exit %s)" % e["exit"]) if "exit" in e else "")
    if ev == "step_reverted":
        return "%s %s" % (sid, "reverted" if e.get("ok") else "REVERT FAILED")
    if ev == "step_blocked":
        return "%s blocked: %s" % (sid, e.get("reason"))
    if ev == "param_set":
        return "set %s" % e.get("key")
    if ev == "plan_changed":
        return "plan %s %s" % (e.get("action"), sid)
    if ev == "note":
        return "note%s: %s" % ((" (%s)" % sid) if sid else "", (e.get("text") or "")[:100])
    if ev == "verify_check":
        return "verify %s %s" % (sid, "ok" if e.get("ok") else "FAILED")
    return ev or "?"


def render_timeline(ctx):
    plan = load_plan(ctx)
    events = read_events(ctx)
    st = fold(events, plan)
    tool = ctx.tool_rel
    nxt, why = next_step(plan, st)
    counts = {}
    for s in plan["steps"]:
        k = st["steps"][s["id"]]["status"]
        counts[k] = counts.get(k, 0) + 1
    L = []
    L.append("# %s" % plan["title"])
    L.append("")
    L.append("> **Goal:** %s" % (plan.get("goal") or "(none recorded)"))
    L.append("> **Timeline:** `%s` | created %s | run %s (%s) | last activity %s"
             % (ctx.slug, plan.get("created", "?"), st["run"], st["kind"], st["last_ts"] or "never"))
    if plan.get("forked_from"):
        L.append("> **Forked from:** `%s` in `%s` on %s" % (plan["forked_from"]["timeline"], plan["forked_from"]["root"], plan["forked_from"]["at"]))
    L.append("> **Tool:** `%s <command>` (self-contained, Python 3 stdlib only; run from the project root, or use the "
             "absolute path to `.back-in-time/bit.py`)" % tool)
    L.append("> **Keep this folder** after a revert: it is the record of what happened and what `replay` needs.")
    L.append("> **Trust:** anything that executes a step (`do`, `auto`, `replay`, `fork`, `verify`, `undo`) runs the shell "
             "commands in plan.json with your privileges. Read the plan before using a ledger you did not write.")
    L.append("")
    L.append("## Where things stand")
    L.append("")
    L.append("Run %s: %s." % (st["run"], ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())) or "no steps yet"))
    if why == "complete":
        L.append("**Next:** nothing. Every step is applied or skipped.")
    elif why == "all-reverted":
        L.append("**Next:** everything in this run is reverted. `%s replay` starts a fresh run." % tool)
    else:
        L.append("**Next:** `%s` (%s): %s. Run `%s next`." % (nxt["id"], why, stitle(nxt, st["vars"]), tool))
    hz = hazards(plan, st)
    if hz:
        L.append("")
        L.append("**Hazards**")
        L += ["- " + h for h in hz]
    L.append("")
    L.append("## Steps")
    L.append("")
    L.append("| id | step | reversible | status | last result |")
    L.append("|----|------|------------|--------|-------------|")
    for s in plan["steps"]:
        ss = st["steps"][s["id"]]
        d = ss["done"]
        res = ""
        if d:
            res = d.get("how", "")
            if d.get("changed"):
                res += ", %d path(s) changed" % len(d["changed"])
            if "exit" in d:
                res += ", exit %s" % d["exit"]
        L.append("| %s | %s | %s | %s | %s |" % (s["id"], esc(stitle(s, st["vars"])), eff_reversible(s), ss["status"], esc(res)))
    L.append("")
    L.append("## Step details")
    for s in plan["steps"]:
        ss = st["steps"][s["id"]]
        L.append("")
        L.append("### %s: %s" % (s["id"], stitle(s, st["vars"])))
        for label, key in FIELDS:
            if s.get(key):
                L.append("- **%s:** %s" % (label, redact(s[key]) if key in ("manual", "undo_manual") else "`%s`" % redact(s[key])))
        if s.get("touches"):
            L.append("- **touches:** " + ", ".join("`%s`" % t for t in s["touches"]))
        L.append("- **reversible:** %s%s" % (eff_reversible(s), (" (%s)" % s["notes"]) if s.get("notes") else ""))
        d = ss["done"]
        if d:
            L.append("- **last result (run %s):** %s at %s%s" % (st["run"], d["status"], d["ts"],
                                                                  (", exit %s, %ss" % (d.get("exit"), d.get("secs"))) if "exit" in d else ""))
            for c in d.get("changed") or []:
                L.append("  - changed `%s` (%s -> %s)" % (c["path"], c["before"], c["after"]))
            if d.get("diff"):
                L.append("  - diff (local only, gitignored): `%s/%s`" % (DIRNAME + "/" + ctx.slug, d["diff"]))
            if d.get("git") and d["git"].get("commits"):
                L.append("  - commits made: " + "; ".join(d["git"]["commits"]))
        if ss["reverted"]:
            L.append("- **reverted** at %s via %s" % (ss["reverted"]["ts"], ss["reverted"].get("method")))
    L.append("")
    L.append("## Operating this timeline")
    L.append("")
    L.append("```bash")
    L.append("%s next                  # what to do now" % tool)
    L.append("%s undo                  # dry run: revert the last applied step" % tool)
    L.append("%s undo <step>           # dry run: rewind to just before <step>" % tool)
    L.append("%s undo --all --yes      # revert everything this run applied" % tool)
    L.append("%s undo --all --merge   # dry run: if files were edited since, remove only what the steps added" % tool)
    L.append("%s replay --auto        # run the whole plan again as a new run (add --set k=v for new parameters)" % tool)
    L.append("%s fork <other-dir>     # same plan in another checkout/environment, fresh history" % tool)
    L.append("%s verify              # is the world still as the trace says?" % tool)
    L.append("```")
    L.append("")
    L.append("`undo` is a dry run until you add `--yes`: show the user the plan first. "
             "Steps marked irreversible need the user's explicit OK (`--yes` / `--approve <step>`).")
    L.append("")
    if st["vars"]:
        L.append("## Parameters (this run)")
        L.append("")
        for k, v in st["vars"].items():
            L.append("- `%s` = `%s`" % (k, redact("%s=%s" % (k, v))[len(k) + 1:]))
        L.append("")
    L.append("## Run history")
    L.append("")
    for r in st["runs"]:
        sets = (" set " + ", ".join("%s=%s" % kv for kv in r["set"].items())) if r.get("set") else ""
        L.append("- run %s: %s, started %s%s" % (r["run"], r.get("kind"), r["ts"], sets))
    L.append("")
    L.append("## Latest journal entries")
    L.append("")
    for e in events[-12:]:
        L.append("- %s (run %s) %s" % (e["ts"], e.get("run"), event_line(e)))
    L.append("")
    L.append("_Generated by bit.py v%s. Do not edit by hand: it is regenerated from plan.json + journal.jsonl._" % VERSION)
    write_atomic(ctx.tl / "TIMELINE.md", "\n".join(L) + "\n")


def refresh_docs(ctx):
    try:
        render_timeline(ctx)
    except Exception as e:  # docs must never break the actual work
        print("warning: could not regenerate TIMELINE.md: %s" % e, file=sys.stderr)


def show_next(ctx):
    plan = load_plan(ctx)
    st = fold(read_events(ctx), plan)
    nxt, why = next_step(plan, st)
    if why == "complete":
        print("Run %s is complete: every step is applied or skipped." % st["run"])
        return 0
    if why == "all-reverted":
        print("Everything in run %s is reverted. `%s replay` starts a fresh run." % (st["run"], ctx.tool))
        return 0
    s = nxt
    print("NEXT %s (%s): %s" % (s["id"], why, stitle(s, st["vars"])))
    for label, key in FIELDS:
        if s.get(key):
            print("  %-13s %s" % (label + ":", redact(s[key])))
    if s.get("touches"):
        print("  touches: %s" % ", ".join(s["touches"]))
    print("  reversible: %s%s" % (eff_reversible(s), (" - " + s["notes"]) if s.get("notes") else ""))
    if why == "running":
        print("  -> started but not finished. Finish with: %s done %s" % (ctx.tool, s["id"]))
    elif why == "failed":
        print("  -> failed earlier. Fix then `%s do %s`, or `%s undo %s`." % (ctx.tool, s["id"], ctx.tool, s["id"]))
    elif s.get("do"):
        flag = " --yes" if eff_reversible(s) == "no" else ""
        print("  -> run: %s do %s%s" % (ctx.tool, s["id"], flag))
    else:
        flag = " --yes" if eff_reversible(s) == "no" else ""
        print("  -> manual: %s begin %s%s ; do the work ; %s done %s --note \"...\"" % (ctx.tool, s["id"], flag, ctx.tool, s["id"]))
    if eff_reversible(s) == "no":
        print("  !! IRREVERSIBLE: get the user's explicit OK before running this (the --yes above is only for after they say yes).")
    return 0


# --------------------------------------------------------------------------- commands

def parse_kv(items, what):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise BitError("%s must look like key=value, got %r" % (what, it))
        k, v = it.split("=", 1)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            raise BitError("bad name %r in %s" % (k, what))
        refuse_secret("%s %s=..." % (what, k), "%s=%s" % (k, v))
        out[k] = v
    return out


def scaffold(bd, slug):
    """The parts of `.back-in-time/` that aren't a timeline: the tool copy, ignore rules, a pointer README."""
    me = Path(__file__).resolve()
    dst = bd / "bit.py"
    if not (dst.exists() and dst.resolve() == me):  # running the ledger's own copy: nothing to refresh
        shutil.copy2(str(me), str(dst))
        os.chmod(str(dst), 0o755)
    gi = bd / ".gitignore"
    have = gi.read_text(encoding="utf-8") if gi.exists() else ""
    want = ["*/snapshots/", "*/diffs/", "*/.lock"]
    missing = [w for w in want if w not in have.splitlines()]
    if missing:
        head = "" if have else ("# snapshots/ and diffs/ hold copies and diffs of your files (secrets, size): keep them local.\n"
                                "# plan.json, journal.jsonl and TIMELINE.md are scrubbed of obvious secrets and safe to commit.\n")
        write_atomic(gi, have + ("" if not have or have.endswith("\n") else "\n") + head + "\n".join(missing) + "\n")
    if not (bd / "README.md").exists():
        write_atomic(bd / "README.md",
                     "# .back-in-time\n\nLedgers kept by the *back-in-time* skill: each folder here is a timeline "
                     "(plan + journal) that can be reverted or replayed.\n\n- Start with `<timeline>/TIMELINE.md`.\n"
                     "- Operate with `python3 .back-in-time/bit.py status | next | undo | replay | verify | fork`.\n"
                     "- Keep this folder after reverting: it is the record of what happened, and `replay` needs it.\n"
                     "- `bit.py` is a self-contained copy (stdlib only), so this folder works without the skill installed.\n"
                     "- Trust: `do`, `auto`, `replay`, `fork`, `verify` and `undo` run the shell commands in `plan.json` with your "
                     "privileges. Read the plan before using a ledger you did not write.\n"
                     "- `snapshots/` and `diffs/` are gitignored (they hold copies of your files). Without them, a clone can read, "
                     "replay and fork the timeline, but `undo` can only run explicit `undo` commands, not restore files.\n")
    write_atomic(bd / "CURRENT", slug + "\n")


def cmd_fork(args):
    ctx = Ctx(args)
    plan = load_plan(ctx)
    target = Path(args.target).expanduser().resolve()
    if target == ctx.root:
        raise BitError("fork needs a different project directory. To run the same plan again here, use `replay`.")
    bd = target / DIRNAME
    bd.mkdir(parents=True, exist_ok=True)
    slug, n = ctx.slug, 2
    while (bd / slug).exists():
        slug, n = "%s-%d" % (ctx.slug, n), n + 1
    (bd / slug).mkdir()
    forked = dict(plan)
    forked["params"] = dict(plan.get("params", {}))
    forked["params"].update(parse_kv(args.set, "--set"))
    forked["created"] = now()
    forked["forked_from"] = {"timeline": ctx.slug, "root": str(ctx.root), "at": now()}
    write_atomic(bd / slug / "plan.json", json.dumps(forked, indent=2, ensure_ascii=False) + "\n")
    (bd / slug / "journal.jsonl").write_text("", encoding="utf-8")
    scaffold(bd, slug)
    ctx2 = Ctx(argparse.Namespace(root=str(target), tl=slug))
    emit(ctx2, 1, "run_started", kind="fork", set={})
    render_timeline(ctx2)
    print("Forked %s -> %s/%s (same plan, fresh history; params: %s)"
          % (ctx.slug, target, DIRNAME + "/" + slug, ", ".join("%s=%s" % kv for kv in forked["params"].items()) or "none"))
    if args.auto:
        return run_auto(ctx2, None, set(args.approve or []))
    return show_next(ctx2)


def cmd_init(args):
    refuse_secret("the title", args.title)
    refuse_secret("--goal", args.goal)
    here = Path(__file__).resolve().parent
    root = Path(args.root).expanduser().resolve() if args.root else (here.parent if here.name == DIRNAME else Path.cwd().resolve())
    bd = root / DIRNAME
    bd.mkdir(exist_ok=True)
    base = "%s-%s" % (datetime.now().strftime("%Y-%m-%d"), slugify(args.title))
    slug, n = base, 2
    while (bd / slug).exists():
        slug, n = "%s-%d" % (base, n), n + 1
    tl = bd / slug
    tl.mkdir()
    plan = {"version": 1, "title": args.title, "goal": args.goal or "", "created": now(),
            "params": parse_kv(args.param, "--param"), "steps": []}
    write_atomic(tl / "plan.json", json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    (tl / "journal.jsonl").write_text("", encoding="utf-8")
    scaffold(bd, slug)
    ctx = Ctx(argparse.Namespace(root=str(root), tl=slug))
    emit(ctx, 1, "run_started", kind="original", set={})
    render_timeline(ctx)
    print("Timeline created: %s/%s" % (DIRNAME, slug))
    print("Next: add steps, e.g.")
    print("  %s step s1 --title \"...\" --do \"...\" --undo \"...\" --verify \"...\"" % ctx.tool)
    print("then `%s check`, then `%s next`." % (ctx.tool, ctx.tool))
    return 0


def cmd_step(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    sid = args.id
    if not ID_RE.match(sid):
        raise BitError("step id must match %s" % ID_RE.pattern)
    began = any(e.get("event") == "step_begin" for e in read_events(ctx))
    run = fold(read_events(ctx), plan)["run"]
    if args.remove:
        get_step(plan, sid)
        events = read_events(ctx)
        live_in = [r for r in sorted({e["run"] for e in events if isinstance(e.get("run"), int)})
                   if fold(events, plan, r)["steps"][sid]["status"] in LIVE]
        if live_in:
            raise BitError("step %s is still applied (or half-done) in run %s. Undo it first (`undo %s --run %s`), then "
                           "remove it: removing it now would orphan its history and `undo` could no longer revert it."
                           % (sid, ", ".join(map(str, live_in)), sid, live_in[-1]))
        plan["steps"] = [s for s in plan["steps"] if s["id"] != sid]
        save_plan(ctx, plan)
        if began:
            emit(ctx, run, "plan_changed", step=sid, action="removed")
        print("Removed step %s." % sid)
        refresh_docs(ctx)
        return 0
    fields = {"title": args.title, "do": args.do, "manual": args.manual, "pre": args.pre, "skip_if": args.skip_if,
              "verify": args.verify, "undo": args.undo, "undo_manual": args.undo_manual,
              "reversible": args.reversible, "notes": args.notes}
    if args.touches:
        for t in args.touches:
            resolve_touch(ctx.root, t)  # validate early
        fields["touches"] = args.touches
    fields = {k: v for k, v in fields.items() if v is not None}
    for k, v in fields.items():
        if isinstance(v, str):
            refuse_secret("--%s" % k.replace("_", "-"), v)
    existing = next((s for s in plan["steps"] if s["id"] == sid), None)
    if existing is None:
        clash = [x["id"] for x in plan["steps"] if x["id"].lower() == sid.lower()]
        if clash:
            raise BitError("step id %r differs from the existing %r only by case. Ids must be unique ignoring case "
                           "(snapshot folders would collide on case-insensitive filesystems like macOS's default)." % (sid, clash[0]))
        if not (fields.get("do") or fields.get("manual")):
            raise BitError("a new step needs --do \"<shell command>\" and/or --manual \"<what to do by hand>\"")
        step = {"id": sid}
        step.update(fields)
        step.setdefault("title", sid)
        if args.after:
            idx = [s["id"] for s in plan["steps"]].index(get_step(plan, args.after)["id"]) + 1
            plan["steps"].insert(idx, step)
        else:
            plan["steps"].append(step)
        action = "added"
    else:
        existing.update(fields)
        action = "updated"
    save_plan(ctx, plan)
    if began:
        emit(ctx, run, "plan_changed", step=sid, action=action, fields=sorted(fields))
    print("%s step %s." % (action.capitalize(), sid))
    refresh_docs(ctx)
    return 0


def cmd_check(args):
    ctx = Ctx(args)
    plan = load_plan(ctx)
    errors, warns = [], []
    if not plan["steps"]:
        errors.append("the plan has no steps yet")
    ids = [s["id"] for s in plan["steps"]]
    if len({i.lower() for i in ids}) != len(ids):
        errors.append("step ids must be unique ignoring case (snapshot folders collide on case-insensitive filesystems)")
    for r, osid, ostatus in fold(read_events(ctx), plan)["orphans"]:
        errors.append("the journal has step %s still %s in run %d but it is no longer in the plan: bit cannot revert it" % (osid, ostatus, r))
    known = set(plan.get("params", {}))
    for s in plan["steps"]:
        for m in re.finditer(r"BIT_SET[ \t]+([A-Za-z_][A-Za-z0-9_]*)=", s.get("do") or ""):
            known.add(m.group(1))
    for s in plan["steps"]:
        sid = s["id"]
        if not (s.get("do") or s.get("manual")):
            errors.append("%s: no `do` command and no `manual` text" % sid)
        r = eff_reversible(s)
        if r == "unknown":
            warns.append("%s: no undo and no touched paths. Add `--undo`, `--touches`, or `--undo-manual`, or mark `--reversible no` with a reason" % sid)
        if r == "no" and not s.get("notes"):
            warns.append("%s: marked irreversible but `--notes` doesn't say why or what the mitigation is" % sid)
        if r == "partial" and not s.get("notes"):
            warns.append("%s: marked partially reversible but `--notes` doesn't say what can't be undone" % sid)
        if not s.get("verify") and r != "no":
            warns.append("%s: no `--verify`: a replay can't tell whether this step really worked" % sid)
        if not s.get("do") and s.get("manual") and eff_reversible(s) == "unknown":
            warns.append("%s: manual step with no undo/touches/undo-manual: nothing to revert it with" % sid)
        for key in ("pre", "skip_if", "do", "verify", "undo", "undo_manual", "manual", "notes"):
            text = s.get(key) or ""
            for name in (VAR_RE.findall(text) if key in ("pre", "skip_if", "do", "verify", "undo") else []):
                if name not in known:
                    warns.append("%s: {{%s}} in `%s` is not a param and no step prints `BIT_SET %s=...` (fine if you will "
                                 "`set %s=...` or `replay --set %s=...` first; otherwise the step stops with an error)"
                                 % (sid, name, key, name, name, name))
            if key == "do":
                for t in s.get("touches") or []:
                    if "{{" not in t:
                        try:
                            tap, tkey = resolve_touch(ctx.root, t)
                            if tap.is_dir() and not tap.is_symlink() and dir_size(tap) > max_snap_bytes():
                                warns.append("%s: --touches %s is over the %g MB snapshot limit: `do` will refuse. Narrow it or "
                                             "raise BIT_MAX_SNAPSHOT_MB" % (sid, t, max_snap_bytes() / 1048576.0))
                        except BitError:
                            pass
                    for name in VAR_RE.findall(t):
                        if name not in known:
                            warns.append("%s: {{%s}} in --touches %s is not a param and no step prints `BIT_SET %s=...`" % (sid, name, t, name))
            if redact(text) != text:
                warns.append("%s: `%s` looks like it contains a secret. Read it from an env var instead of inlining it" % (sid, key))
    for e in errors:
        print("ERROR  " + e)
    for w in warns:
        print("WARN   " + w)
    print("%d error(s), %d warning(s) in %d step(s)." % (len(errors), len(warns), len(plan["steps"])))
    if not errors and not warns:
        print("Plan looks replayable and reversible.")
    return 2 if errors else 0


def cmd_next(args):
    ctx = Ctx(args)
    return show_next(ctx)


def cmd_do(args):
    ctx = Ctx(args, lock=True)
    code = exec_step(ctx, args.id, args.yes)
    refresh_docs(ctx)
    return code


def cmd_begin(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    step = get_step(plan, args.id)
    st = fold(read_events(ctx), plan)
    ss = st["steps"][args.id]
    if ss["status"] == "applied":
        raise BitError("%s is already applied in run %s." % (args.id, st["run"]))
    manifest, _ = begin_step(ctx, st["run"], step, args.yes, st["vars"])
    print("BEGIN %s: %s" % (args.id, stitle(step, st["vars"])))
    if manifest:
        print("  before-state saved for: %s" % ", ".join(manifest))
    else:
        print("  no --touches declared, so nothing was snapshotted: undo must come from an `undo` command.")
    if step.get("manual"):
        print("  to do: %s" % step["manual"])
    print("  When finished: %s done %s --note \"what you actually did\"   (or --failed)" % (ctx.tool, args.id))
    refresh_docs(ctx)
    return 0


def cmd_done(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    step = get_step(plan, args.id)
    st = fold(read_events(ctx), plan)
    run, ss = st["run"], st["steps"][args.id]
    if ss["status"] == "applied":
        raise BitError("%s is already applied in run %s." % (args.id, run))
    manifest, git0 = None, None
    if ss["status"] == "running":
        manifest = load_manifest(ctx, run, args.id)
        git0 = (ss["begin"] or {}).get("git_head")
    else:
        print("note: no `begin %s` in this run, so no before-state was captured; snapshot-based undo is unavailable "
              "for it." % args.id, file=sys.stderr)
    ok = not args.failed
    verify = None
    if ok and step.get("verify") and not args.no_verify:
        env = env_for(ctx, run, args.id, st["vars"])
        try:
            vcmd = render_cmd(step["verify"], st["vars"])
            vcode, vout, vsecs = run_shell(ctx, vcmd, env)
        except BitError as e:
            vcmd, vcode, vout, vsecs = step["verify"], 2, str(e), 0
        verify = {"cmd": redact(vcmd), "exit": vcode, "output": tail(vout, 600), "secs": vsecs}
        ok = vcode == 0
    ev = finalize(ctx, run, step, manifest, "applied" if ok else "failed", "manual", git0,
                  note=redact(args.note) if args.note else None, verify=verify)
    report_done(args.id, step, ev, "", verify, st["vars"])
    refresh_docs(ctx)
    return 0 if ok else 1


class MergeConflict(Exception):
    pass


def reverse_merge(ctx, run, sid, key, text):
    """Put back what this step changed in `text` (the file as it will be when this step is reverted, which may include
    later human edits): find each hunk's 'after' block, with its context, exactly once and swap the 'before' block
    back in. Strict: no match, or more than one, is a conflict, never a guess.
    -> ("text", new_text) or ("delete", None)."""
    doc = load_hunks(ctx, run, sid).get(key)
    if not doc:
        raise MergeConflict("no text diff was recorded for it (binary, large, not a plain file, or the step deleted it)")
    if text is None:
        raise MergeConflict("it is no longer a readable text file")
    lines = text.splitlines(True)
    for i, h in sorted(enumerate(doc["hunks"]), key=lambda t: -t[1]["at"]):  # bottom first: earlier indexes stay valid
        block = h["after"]
        if not block:
            raise MergeConflict("hunk %d has nothing to anchor on" % (i + 1))
        n = len(block)
        hits = [j for j in range(len(lines) - n + 1) if lines[j:j + n] == block]
        if not hits:
            raise MergeConflict("hunk %d no longer matches: the lines around this step's change were edited later" % (i + 1))
        if len(hits) > 1:
            raise MergeConflict("hunk %d is ambiguous: its lines appear in %d places, so there is no safe way to tell which "
                                "one this step changed" % (i + 1, len(hits)))
        j, repl = hits[0], list(h["before"])
        if repl and not repl[-1].endswith("\n") and j + n < len(lines):
            repl[-1] += "\n"  # the original ended without a newline; a later line must not be glued onto it
        lines[j:j + n] = repl
    new = "".join(lines)
    if doc.get("created") and new == "":
        return "delete", None
    return "text", new


def revert_items(ctx, plan, st, targets, force, merge=False):
    """Describe how each target would be reverted, and what blocks it.

    Order inside one step: restore the snapshotted files first, then run the undo command on top
    (so e.g. `touches package.json package-lock.json` + `undo npm ci` restores the lockfile, then reinstalls).

    `targets` is reverse-chronological. To judge drift correctly when several steps touched one path, the whole revert
    is simulated in order and each path carries a *virtual* state: what the file will look like by the time the next
    (earlier) step is reached. A later step that is blocked blocks every earlier step on that path (reverting under
    it would overwrite its work); a later step that was merged leaves the human's edits in the text the earlier step is
    merged against; a later plain restore leaves the file in that step's before-state."""
    items = []
    virtual = {}  # key -> ("snap", state, snapshot_path) | ("merged", text) | ("blocked",)
    for sid in targets:
        step = get_step(plan, sid)
        ss = st["steps"][sid]
        it = {"sid": sid, "step": step, "restore": None, "merge": {}, "skip": set(), "cmd": None,
              "manual": soft_render(step.get("undo_manual"), st["vars"]), "blocked": None,
              "title": stitle(step, st["vars"]), "notes": []}
        manifest = load_manifest(ctx, st["run"], sid)
        expected = set(((ss["begin"] or {}).get("snapshot")) or {})
        after = (ss["done"] or {}).get("after") or {}
        problem = snapshot_problem(ctx, st["run"], sid, manifest, expected)
        if eff_reversible(step) == "no":
            it["blocked"] = "irreversible: %s" % (step.get("notes") or "no mitigation recorded")
        else:
            if step.get("undo"):
                try:
                    it["cmd"] = render_cmd(step["undo"], st["vars"])
                except BitError as e:
                    it["blocked"] = str(e)
            if problem and not force and not it["blocked"]:
                it["blocked"] = ("Snapshot problem: %s. snapshots/ is gitignored, so it is absent in a clone or after a cleanup: "
                                 "the files this step changed can't be restored automatically. (--force runs only its undo "
                                 "command, if it has one.)" % problem)
            elif problem and force:
                it["notes"].append("snapshot problem ignored because of --force: %s" % problem)
            elif manifest:
                it["restore"] = manifest
            if not it["blocked"] and not it["cmd"] and not it["restore"]:
                if it["manual"]:
                    it["blocked"] = ("MANUAL UNDO needed: %s  -> do it, then run `%s reverted %s --run %d --note \"...\"`"
                                     % (it["manual"], ctx.tool, sid, st["run"]))
                else:
                    it["blocked"] = "no undo command and no snapshot (it was done without `begin`/--touches)"
        if it["restore"] and not it["blocked"] and not force:
            for k in it["restore"]:
                if virtual.get(k, ("",))[0] == "blocked":
                    it["blocked"] = ("%s is also changed by a later step that will NOT be reverted (it is blocked above), so "
                                     "restoring this earlier step would overwrite that step's work. Resolve the later step "
                                     "first, or use --force." % k)
                    break
        if it["restore"] and not it["blocked"] and not force:
            done_states = (ss["revert_failed"] or {}).get("restored_states") or {}
            drift = []
            for k, e in it["restore"].items():
                v = virtual.get(k)
                if v is None:
                    cur = short(state_of(abs_from_key(ctx.root, k)))
                    if cur == done_states.get(k):  # an earlier attempt already restored it (then its undo command failed)
                        it["skip"].add(k)
                        it["notes"].append("%s was already restored by an earlier attempt of this undo" % k)
                        continue
                elif v[0] == "snap":
                    cur = v[1]
                else:  # "merged": by construction it differs from what this step left, by the human's edits
                    cur = None
                # drifted = neither how this step left it nor how it was before it ran
                if k in after and (cur is None or (cur != after[k] and cur != e["state"])):
                    drift.append(k)
            if drift and merge:
                conflicts = []
                for k in drift:
                    v = virtual.get(k)
                    path = abs_from_key(ctx.root, k)
                    if v is None:
                        base = read_small_text(path) if path.is_file() and not path.is_symlink() else None
                    elif v[0] == "merged":
                        base = v[1]
                    else:  # a later plain restore: the file will be that step's stored before-copy
                        base = read_small_text(v[2]) if v[2] and Path(v[2]).is_file() else ("" if v[1] == "absent" else None)
                    try:
                        it["merge"][k] = reverse_merge(ctx, st["run"], sid, k, base)
                    except MergeConflict as c:
                        conflicts.append("%s: %s" % (k, c))
                if conflicts:
                    it["blocked"] = ("MERGE CONFLICT, nothing changed for this step: %s. Resolve by hand then `reverted`, "
                                     "or --force to overwrite with the snapshot." % "; ".join(conflicts))
            elif drift:
                it["blocked"] = ("DRIFT: %s changed since this step ran (edited later). Restoring the snapshot would overwrite "
                                 "that work. Options: --merge (remove just what this step added or changed and keep the later "
                                 "edits; text files, stops on conflict) or --force (overwrite with the snapshot)."
                                 % ", ".join(drift))
        for k, e in (manifest or {}).items():
            if e.get("late"):
                it["notes"].append("%s was snapshotted late (the step had already run once), so its restored state may "
                                   "include that earlier partial change" % k)
        # what the files will look like once this item has been handled, for the earlier steps still to come
        for k, e in (manifest or {}).items():
            if it["blocked"]:
                virtual[k] = ("blocked",)
            elif k in it["skip"]:
                virtual.pop(k, None)
            elif k in it["merge"]:
                kind, text = it["merge"][k]
                virtual[k] = ("merged", text) if kind == "text" else ("snap", "absent", None)
            elif it["restore"]:
                virtual[k] = ("snap", e["state"], str(snap_file(ctx, st["run"], sid, k)) if e["kind"] == "file" else None)
        items.append(it)
    return items


def cmd_undo(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    events = read_events(ctx)
    st = fold(events, plan, args.run)
    orphan_lines = ["step %s is no longer in the plan but is still %s in run %d: bit cannot revert it, so it stays applied "
                    "(put it back in plan.json, or undo its effects by hand)" % (sid, status, r)
                    for r, sid, status in st["orphans"] if args.run in (None, r)]
    live = [s for s in st["order"] if st["steps"][s]["status"] in LIVE]
    if not live:
        print("Nothing to undo in run %s: no applied, failed or half-done steps." % st["run"])
        for r, sids in sorted(st["earlier"].items()):
            print("Run %d still has applied steps (%s): `%s undo --all --run %d` reverts them." % (r, ", ".join(sids), ctx.tool, r))
        for line in orphan_lines:
            print("WARNING: " + line)
        return 3 if orphan_lines and args.yes else 0
    if args.all:
        targets = list(reversed(live))
        scope = "everything run %s applied" % st["run"]
    elif args.step:
        if args.step not in live:
            raise BitError("%s is not applied in run %s (applied: %s)" % (args.step, st["run"], ", ".join(live)))
        targets = list(reversed(live[live.index(args.step):]))
        scope = "rewind to just before %s" % args.step
    else:
        targets = [live[-1]]
        scope = "the last applied step only"
    items = revert_items(ctx, plan, st, targets, args.force, args.merge)
    for line in orphan_lines:
        print("WARNING: " + line)
    print("REVERT PLAN (%s), in this order:" % scope)
    stopped = False
    for it in items:
        tag = "   (not reached: undo stops at the blocked step above)" if stopped and not args.keep_going else ""
        print("  %-7s %s  %s%s" % ("BLOCKED" if it["blocked"] else "revert", it["sid"], it["title"], tag))
        if it["blocked"]:
            print("          %s" % it["blocked"])
            stopped = True
            continue
        plain = [k for k in (it["restore"] or {}) if k not in it["merge"] and k not in it["skip"]]
        if plain:
            print("          restore files: %s" % ", ".join(plain))
        for k, (kind, _) in it["merge"].items():
            print("          merge: %s (%s, keeping later edits)" % (k, "remove the file it created" if kind == "delete" else "reverse-apply this step's change"))
        if it["cmd"]:
            print("          run: %s" % it["cmd"])
        if it["manual"]:
            print("          ALSO by hand afterwards: %s" % it["manual"])
        for n in it["notes"]:
            print("          note: %s" % n)
    if any(it["blocked"] for it in items) and not args.keep_going:
        print("\nA blocked step stops the undo and leaves it, and everything listed after it, applied. Add --keep-going to "
              "revert every step that can be reverted and leave only the blocked ones.")
    if not args.yes:
        print("\nDRY RUN. Nothing was changed. Show this to the user, then re-run with --yes to execute.")
        return 0
    done_ids, code = [], 0
    for it in items:
        sid = it["sid"]
        if it["blocked"]:
            print("%s %s: %s" % ("SKIPPED" if args.keep_going else "STOP at", sid, it["blocked"]))
            code = 3
            if not args.keep_going:
                break
            continue
        ok, extra = revert_one(ctx, st, it)
        emit(ctx, st["run"], "step_reverted", step=sid, ok=ok, method=extra.pop("method", "?"), **extra)
        if ok:
            retire_snapshot(ctx, st["run"], sid)
            done_ids.append(sid)
            print("REVERTED %s" % sid)
            for w in extra.get("warnings") or []:
                print("  warning: " + w)
        else:
            print("REVERT FAILED %s: %s" % (sid, extra.get("detail")))
            code = 1
            if not args.keep_going:
                break
    left = [s for s in live if s not in done_ids]
    print("\nReverted: %s." % (", ".join(done_ids) or "nothing"))
    if left:
        print("Still applied: %s." % ", ".join(left))
    if orphan_lines:
        print("Not reverted (no longer in the plan): %s." % ", ".join(sorted({sid for _, sid, _ in st["orphans"]})))
        code = code or 3
    refresh_docs(ctx)
    return code


def revert_one(ctx, st, it):
    sid, run = it["sid"], st["run"]
    extra, warnings, methods = {}, [], []
    if it["restore"]:
        methods.append("restore")
        restored = []
        try:
            for k, e in it["restore"].items():
                if k in it["skip"]:
                    continue
                path = abs_from_key(ctx.root, k)
                if k in it["merge"]:
                    kind, text = it["merge"][k]
                    if kind == "delete":
                        remove_path(path)
                        remove_created_parents(e)
                    else:
                        with open(str(path), "w", encoding="utf-8", newline="") as f:
                            f.write(text)
                    warnings.append("merged %s: removed this step's changes and kept the later edits" % k)
                else:
                    restore_path(ctx, run, sid, k, e)
                restored.append(k)
        except OSError as e:
            extra.update(method="+".join(methods), detail="restore failed: %s" % e, restored=restored,
                         restored_states={k: short(state_of(abs_from_key(ctx.root, k))) for k in restored})
            return False, extra
        extra["restored"] = restored
        for k, e in it["restore"].items():
            if k not in it["merge"] and k not in it["skip"] and short(state_of(abs_from_key(ctx.root, k))) != e["state"]:
                warnings.append("%s does not match its snapshot after restoring" % k)
        if it["merge"]:
            extra["merged"] = sorted(it["merge"])
    warnings += [n for n in it["notes"] if "snapshotted late" in n]
    if it["cmd"]:
        methods.append("command")
        code, out, secs = run_shell(ctx, it["cmd"], env_for(ctx, run, sid, st["vars"]))
        extra.update(cmd=redact(it["cmd"]), exit=code, secs=secs, output=tail(out))
        if code != 0:
            # remember what the restore left behind, so a retry sees 'already restored' instead of 'edited later'
            extra.update(method="+".join(methods), detail="undo command exited %s" % code,
                         restored_states={k: short(state_of(abs_from_key(ctx.root, k))) for k in (it["restore"] or {})})
            print_tail(out, 15)
            return False, extra
    if it["manual"]:
        warnings.append("also do by hand: %s" % it["manual"])
        extra["manual_remainder"] = redact(it["manual"])
    extra["method"] = "+".join(methods) or "none"
    if warnings:
        extra["warnings"] = warnings
    return True, extra


def cmd_reverted(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    get_step(plan, args.id)
    st = fold(read_events(ctx), plan, args.run)
    if st["steps"][args.id]["status"] not in LIVE:
        raise BitError("%s is %s in run %s: nothing to mark as reverted." % (args.id, st["steps"][args.id]["status"], st["run"]))
    emit(ctx, st["run"], "step_reverted", step=args.id, ok=True, method="manual",
         note=redact(args.note) if args.note else None)
    retire_snapshot(ctx, st["run"], args.id)
    print("REVERTED %s (recorded: you undid it by hand)" % args.id)
    refresh_docs(ctx)
    return 0


def cmd_replay(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    events = read_events(ctx)
    sets = parse_kv(args.set, "--set")
    if args.from_step:
        get_step(plan, args.from_step)  # validate BEFORE committing a new run to the journal
    run = max([e["run"] for e in events if isinstance(e.get("run"), int)] or [1]) + 1
    emit(ctx, run, "run_started", kind="replay", set=sets, **({"from": args.from_step} if args.from_step else {}))
    if args.from_step:
        for s in plan["steps"]:
            if s["id"] == args.from_step:
                break
            emit(ctx, run, "step_done", step=s["id"], status="skipped", how="replay --from",
                 note="before --from %s" % args.from_step)
    print("Run %s started (replay). Steps are pending again; the world is whatever it is now, so `skip_if` and "
          "`pre` guards decide what actually runs." % run)
    for r, sids in sorted(fold(read_events(ctx), plan)["earlier"].items()):
        print("note: run %d still has applied steps (%s). They are still in the world; `undo --all --run %d` reverts "
              "them, or let skip_if guards absorb them." % (r, ", ".join(sids), r))
    refresh_docs(ctx)
    if args.auto:
        return run_auto(ctx, None, set(args.approve or []))
    return show_next(ctx)


def run_auto(ctx, from_step, approve):
    plan = load_plan(ctx)
    if from_step:
        get_step(plan, from_step)
    started = from_step is None
    ran = 0
    while True:
        plan = load_plan(ctx)
        st = fold(read_events(ctx), plan)
        pick = None
        seen = started
        for s in plan["steps"]:
            if not seen and s["id"] == from_step:
                seen = True
            if not seen:
                continue
            if st["steps"][s["id"]]["status"] not in ("applied", "skipped"):
                pick = s
                break
        if pick is None:
            print("Run %s complete: every step is applied or skipped (%d executed now)." % (st["run"], ran))
            refresh_docs(ctx)
            return 0
        try:
            code = exec_step(ctx, pick["id"], pick["id"] in approve)
        except BitError as e:
            print("PAUSED at %s: %s" % (pick["id"], e))
            refresh_docs(ctx)
            return e.code
        ran += 1
        if code != 0:
            print("PAUSED at %s (%s)." % (pick["id"], "needs a human or the agent" if code == 3 else "failed"))
            refresh_docs(ctx)
            return code


def cmd_auto(args):
    ctx = Ctx(args, lock=True)
    return run_auto(ctx, args.from_step, set(args.approve or []))


def cmd_verify(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    st = fold(read_events(ctx), plan)
    bad = 0
    ids = [args.id] if args.id else [s["id"] for s in plan["steps"]]
    owner = {}  # path -> the latest applied step that touched it: earlier steps' after-states are superseded
    for osid in st["order"]:
        oss = st["steps"][osid]
        if oss["status"] == "applied":
            for k in (oss["done"] or {}).get("after") or {}:
                owner[k] = osid
    for sid in ids:
        step, ss = get_step(plan, sid), st["steps"][sid]
        if ss["status"] != "applied":
            continue
        after = (ss["done"] or {}).get("after") or {}
        problems = []
        for k, want in after.items():
            if owner.get(k) != sid:
                continue
            if short(state_of(abs_from_key(ctx.root, k))) != want:
                problems.append("DRIFT %s changed since the step ran" % k)
        if step.get("verify"):
            try:
                code, out, _ = run_shell(ctx, render_cmd(step["verify"], st["vars"]), env_for(ctx, st["run"], sid, st["vars"]))
                if code != 0:
                    problems.append("verify command exited %s" % code)
            except BitError as e:
                problems.append(str(e))
        elif not after:
            print("SKIP %s: nothing to verify (no verify command, no touched paths)" % sid)
            continue
        emit(ctx, st["run"], "verify_check", step=sid, ok=not problems, problems=problems or None)
        print("%s %s%s" % ("FAIL" if problems else "OK  ", sid, ("  " + "; ".join(problems)) if problems else ""))
        bad += bool(problems)
    refresh_docs(ctx)
    return 1 if bad else 0


def cmd_status(args):
    ctx = Ctx(args)
    plan = load_plan(ctx)
    st = fold(read_events(ctx), plan)
    print("%s  [%s]  run %s (%s)" % (plan["title"], ctx.slug, st["run"], st["kind"]))
    if plan.get("goal"):
        print("goal: %s" % plan["goal"])
    for s in plan["steps"]:
        ss = st["steps"][s["id"]]
        print("  %-8s %-9s %-8s %s" % (s["id"], ss["status"], "rev:" + eff_reversible(s), stitle(s, st["vars"])))
    if not plan["steps"]:
        print("  (no steps yet)")
    for h in hazards(plan, st):
        print("  ! " + h.replace("`", ""))
    nxt, why = next_step(plan, st)
    if nxt:
        print("next: %s (%s)" % (nxt["id"], why))
    refresh_docs(ctx)
    print("handoff doc: %s" % (ctx.tl / "TIMELINE.md"))
    return 0


def cmd_list(args):
    root = find_root(args.root)
    slugs = list_timelines(root)
    if not slugs:
        print("No timelines in %s" % root)
        return 0
    cur_file = root / DIRNAME / "CURRENT"
    cur = cur_file.read_text().strip() if cur_file.exists() else None
    for slug in slugs:
        ctx = Ctx(argparse.Namespace(root=str(root), tl=slug))
        plan = load_plan(ctx)
        st = fold(read_events(ctx), plan)
        counts = {}
        for s in plan["steps"]:
            counts[st["steps"][s["id"]]["status"]] = counts.get(st["steps"][s["id"]]["status"], 0) + 1
        print("%s %s  run %s  %s  %s" % ("*" if slug == cur else " ", slug, st["run"],
                                         ", ".join("%d %s" % (n, k) for k, n in sorted(counts.items())) or "empty", plan["title"]))
    return 0


def cmd_use(args):
    root = find_root(args.root)
    slug = pick_timeline(root, args.slug)
    write_atomic(root / DIRNAME / "CURRENT", slug + "\n")
    print("Current timeline: %s" % slug)
    return 0


def cmd_set(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    run = fold(read_events(ctx), plan)["run"]
    for k, v in parse_kv(args.pairs, "argument").items():
        emit(ctx, run, "param_set", key=k, value=v, source="cli")
        print("set %s" % k)
    refresh_docs(ctx)
    return 0


def cmd_note(args):
    ctx = Ctx(args, lock=True)
    plan = load_plan(ctx)
    run = fold(read_events(ctx), plan)["run"]
    if args.step:
        get_step(plan, args.step)
    emit(ctx, run, "note", text=redact(args.text), **({"step": args.step} if args.step else {}))
    print("noted")
    refresh_docs(ctx)
    return 0


# --------------------------------------------------------------------------- CLI

def build_parser():
    # The same two flags on the main parser and on every subcommand (SUPPRESS so a subcommand's absent flag doesn't
    # overwrite the one given before it): `bit.py --tl X status` and `bit.py status --tl X` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tl", default=argparse.SUPPRESS, help="timeline slug (default: the current one)")
    common.add_argument("--root", default=argparse.SUPPRESS, help="project root (default: auto-detected)")
    p = argparse.ArgumentParser(prog="bit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="bit %s" % VERSION)
    p.add_argument("--tl", help="timeline slug (default: the current one)")
    p.add_argument("--root", help="project root (default: auto-detected)")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        sp.set_defaults(func=fn)
        return sp

    sp = add("init", cmd_init, "start a new timeline in the project root")
    sp.add_argument("title")
    sp.add_argument("--goal", help="one sentence: what is this work for?")
    sp.add_argument("--param", action="append", metavar="K=V", help="plan parameter, usable as {{K}} (repeatable)")

    sp = add("step", cmd_step, "add or edit a plan step (only the flags you pass change)")
    sp.add_argument("id")
    sp.add_argument("--title")
    sp.add_argument("--do", help="shell command that performs the step (omit for manual steps)")
    sp.add_argument("--manual", help="what to do by hand (edits, browser actions, API calls made with the agent's own tools)")
    sp.add_argument("--pre", help="must exit 0 before the step runs, otherwise the plan's assumptions no longer hold")
    sp.add_argument("--skip-if", dest="skip_if", help="exit 0 means 'already done': skip (makes replay idempotent)")
    sp.add_argument("--verify", help="must exit 0 after the step, proving it worked")
    sp.add_argument("--undo", help="shell command that reverts the step ({{vars}} allowed)")
    sp.add_argument("--undo-manual", dest="undo_manual",
                    help="how to undo by hand when no command can (API/UI/MCP effects); `undo` pauses there until you run `reverted`")
    sp.add_argument("--touches", nargs="+", action="extend", metavar="PATH",
                    help="files/dirs the step changes; their before-state is snapshotted so undo can restore them")
    sp.add_argument("--reversible", choices=["yes", "partial", "no", "unknown"])
    sp.add_argument("--notes", help="for partial/no: what can't be undone and the mitigation")
    sp.add_argument("--after", help="insert after this step id")
    sp.add_argument("--remove", action="store_true")

    add("check", cmd_check, "lint the plan: undo defined? irreversible steps explained? verifiable?")
    add("next", cmd_next, "show the next step to execute")

    sp = add("do", cmd_do, "execute a step's shell command, with snapshot, verify and trace")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true", help="the user explicitly approved this irreversible step")

    sp = add("begin", cmd_begin, "start a step you will perform yourself: snapshots its --touches first")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true", help="the user explicitly approved this irreversible step")

    sp = add("done", cmd_done, "finish a step you performed yourself: records what changed")
    sp.add_argument("id")
    sp.add_argument("--note", help="what you actually did (including deviations from the plan)")
    sp.add_argument("--failed", action="store_true")
    sp.add_argument("--no-verify", action="store_true")

    sp = add("undo", cmd_undo, "revert applied steps in reverse order (dry run unless --yes)")
    sp.add_argument("step", nargs="?", help="rewind to just before this step (reverts it and everything after)")
    sp.add_argument("--all", action="store_true", help="revert everything this run applied")
    sp.add_argument("--run", type=int, help="act on this run instead of the latest")
    sp.add_argument("--yes", action="store_true", help="actually do it")
    sp.add_argument("--merge", action="store_true",
                    help="where files were edited since the step ran: remove only what the step added/changed and keep the later edits (text files; stops on conflict)")
    sp.add_argument("--force", action="store_true", help="restore snapshots even if the files drifted since")
    sp.add_argument("--keep-going", action="store_true", help="continue past blocked/failed reverts")

    sp = add("reverted", cmd_reverted, "record that you undid a step by hand (after `undo` paused on its --undo-manual)")
    sp.add_argument("id")
    sp.add_argument("--run", type=int, help="the run the step belongs to (default: the latest)")
    sp.add_argument("--note", help="what you did")

    sp = add("replay", cmd_replay, "start a new run of the same plan (optionally with new parameters)")
    sp.add_argument("--set", action="append", metavar="K=V", help="override a parameter for this run")
    sp.add_argument("--from", dest="from_step", metavar="STEP", help="treat earlier steps as already done")
    sp.add_argument("--auto", action="store_true", help="execute steps until one needs a human/agent")
    sp.add_argument("--approve", action="append", metavar="STEP", help="pre-approved irreversible step (repeatable)")

    sp = add("auto", cmd_auto, "execute pending steps in order until one needs a human/agent or fails")
    sp.add_argument("--from", dest="from_step", metavar="STEP")
    sp.add_argument("--approve", action="append", metavar="STEP", help="pre-approved irreversible step (repeatable)")

    sp = add("fork", cmd_fork, "copy this plan into another project directory/checkout with fresh history (new env, new machine)")
    sp.add_argument("target", help="the other project's root directory")
    sp.add_argument("--set", action="append", metavar="K=V", help="parameter value for the fork (kept as its default)")
    sp.add_argument("--auto", action="store_true", help="execute steps there until one needs a human/agent")
    sp.add_argument("--approve", action="append", metavar="STEP", help="pre-approved irreversible step (repeatable)")

    sp = add("verify", cmd_verify, "re-check applied steps: verify commands pass and touched files haven't drifted")
    sp.add_argument("id", nargs="?")

    add("status", cmd_status, "summary of the current run (also regenerates TIMELINE.md)")
    add("list", cmd_list, "list timelines in this project")
    sp = add("use", cmd_use, "make a timeline the default")
    sp.add_argument("slug")
    sp = add("set", cmd_set, "set run variables ({{name}} in commands)")
    sp.add_argument("pairs", nargs="+", metavar="K=V")
    sp = add("note", cmd_note, "add a free-form entry to the journal (deviations, decisions, findings)")
    sp.add_argument("text")
    sp.add_argument("--step")
    return p


def _on_signal(signum, frame):
    raise KeyboardInterrupt()  # same path as Ctrl-C: run_shell kills the step's process group, then we exit


def main(argv=None):
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _on_signal)
    args = build_parser().parse_args(argv)
    try:
        code = args.func(args) or 0
    except BitError as e:
        print("error: %s" % e, file=sys.stderr)
        code = e.code
    except KeyboardInterrupt:
        print("interrupted: a step that was running stays 'running' in the journal; finish it with `done` or revert it "
              "with `undo`.", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
