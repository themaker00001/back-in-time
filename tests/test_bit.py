"""Black-box tests for bit.py: every test drives the real CLI in a throwaway project.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BIT_SRC = Path(__file__).resolve().parent.parent / "skills" / "back-in-time" / "scripts" / "bit.py"


class Sandbox:
    """A temp project with `bit` initialised in it."""

    def __init__(self, title="Test run", goal="g", params=()):
        self.dir = Path(tempfile.mkdtemp(prefix="bit-test-")).resolve()
        args = ["init", title, "--goal", goal]
        for p in params:
            args += ["--param", p]
        r = subprocess.run([sys.executable, str(BIT_SRC)] + args + ["--root", str(self.dir)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        self.bit = self.dir / ".back-in-time" / "bit.py"
        self.tl = next(d for d in (self.dir / ".back-in-time").iterdir() if (d / "plan.json").exists())

    def run(self, *args, check=None):
        r = subprocess.run([sys.executable, str(self.bit)] + list(args), cwd=str(self.dir),
                           capture_output=True, text=True)
        if check is not None:
            assert r.returncode == check, "exit %s (wanted %s)\n%s\n%s" % (r.returncode, check, r.stdout, r.stderr)
        r.all = r.stdout + r.stderr
        return r

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def read(self, rel):
        return (self.dir / rel).read_text()

    def exists(self, rel):
        return os.path.lexists(str(self.dir / rel))

    def events(self):
        return [json.loads(l) for l in (self.tl / "journal.jsonl").read_text().splitlines() if l.strip()]

    def plan(self):
        return json.loads((self.tl / "plan.json").read_text())

    def cleanup(self):
        shutil.rmtree(str(self.dir), ignore_errors=True)


class BitTest(unittest.TestCase):
    def sandbox(self, **kw):
        sb = Sandbox(**kw)
        self.addCleanup(sb.cleanup)
        return sb


class TestBasics(BitTest):
    def test_init_makes_self_contained_ledger(self):
        sb = self.sandbox()
        self.assertTrue(sb.bit.exists())
        self.assertTrue((sb.dir / ".back-in-time" / ".gitignore").exists())
        self.assertIn("snapshots", (sb.dir / ".back-in-time" / ".gitignore").read_text())
        self.assertTrue((sb.tl / "TIMELINE.md").exists())
        self.assertEqual(sb.events()[0]["event"], "run_started")

    def test_do_then_undo_restores_file_byte_for_byte(self):
        sb = self.sandbox()
        sb.write("a.txt", "original\n")
        sb.run("step", "s1", "--do", "echo changed > a.txt", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=0)
        self.assertEqual(sb.read("a.txt"), "changed\n")
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("a.txt"), "original\n")

    def test_undo_deletes_files_the_step_created(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo hi > new.txt", "--touches", "new.txt", check=0)
        sb.run("do", "s1", check=0)
        self.assertTrue(sb.exists("new.txt"))
        sb.run("undo", "--yes", check=0)
        self.assertFalse(sb.exists("new.txt"))

    def test_directory_snapshot_restores_added_removed_and_modified(self):
        sb = self.sandbox()
        sb.write("cfg/a.txt", "A\n")
        sb.write("cfg/b.txt", "B\n")
        sb.run("step", "s1", "--do", "rm cfg/a.txt && echo B2 > cfg/b.txt && echo C > cfg/c.txt",
               "--touches", "cfg", check=0)
        sb.run("do", "s1", check=0)
        diff = (sb.tl / "diffs" / "r1-s1.diff").read_text()
        self.assertIn("A c.txt", diff)
        self.assertIn("D a.txt", diff)
        self.assertIn("M b.txt", diff)
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("cfg/a.txt"), "A\n")
        self.assertEqual(sb.read("cfg/b.txt"), "B\n")
        self.assertFalse(sb.exists("cfg/c.txt"))

    def test_dry_run_changes_nothing(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo new > a.txt", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=0)
        r = sb.run("undo", "--all", check=0)
        self.assertIn("DRY RUN", r.all)
        self.assertEqual(sb.read("a.txt"), "new\n")

    def test_undo_runs_in_reverse_chronological_order(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "echo a >> order.log", "--undo", "echo undo-a >> order.log", check=0)
        sb.run("step", "b", "--do", "echo b >> order.log", "--undo", "echo undo-b >> order.log", check=0)
        sb.run("step", "c", "--do", "echo c >> order.log", "--undo", "echo undo-c >> order.log", check=0)
        sb.run("auto", check=0)
        sb.run("undo", "--all", "--yes", check=0)
        self.assertEqual(sb.read("order.log").split(), ["a", "b", "c", "undo-c", "undo-b", "undo-a"])

    def test_rewind_to_step_reverts_it_and_everything_after(self):
        sb = self.sandbox()
        for s in "abc":
            sb.run("step", s, "--do", "touch %s.f" % s, "--undo", "rm -f %s.f" % s, check=0)
        sb.run("auto", check=0)
        sb.run("undo", "b", "--yes", check=0)
        self.assertTrue(sb.exists("a.f"))
        self.assertFalse(sb.exists("b.f"))
        self.assertFalse(sb.exists("c.f"))

    def test_plain_undo_only_reverts_the_last_step(self):
        sb = self.sandbox()
        for s in "ab":
            sb.run("step", s, "--do", "touch %s.f" % s, "--undo", "rm -f %s.f" % s, check=0)
        sb.run("auto", check=0)
        sb.run("undo", "--yes", check=0)
        self.assertTrue(sb.exists("a.f"))
        self.assertFalse(sb.exists("b.f"))


class TestSafety(BitTest):
    def test_irreversible_step_is_gated_until_approved(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo sent > sent.txt", "--reversible", "no", "--notes", "email can't be unsent", check=0)
        r = sb.run("do", "s1", check=3)
        self.assertIn("IRREVERSIBLE", r.all)
        self.assertFalse(sb.exists("sent.txt"))
        sb.run("do", "s1", "--yes", check=0)
        self.assertTrue(sb.exists("sent.txt"))

    def test_auto_stops_before_irreversible_step_unless_approved(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("step", "b", "--do", "touch b.f", "--reversible", "no", "--notes", "x", check=0)
        sb.run("step", "c", "--do", "touch c.f", "--undo", "rm -f c.f", check=0)
        r = sb.run("auto", check=3)
        self.assertTrue(sb.exists("a.f"))
        self.assertFalse(sb.exists("b.f"))
        self.assertIn("PAUSED at b", r.all)
        sb.run("auto", "--approve", "b", check=0)
        self.assertTrue(sb.exists("c.f"))

    def test_undo_stops_at_irreversible_step_and_leaves_earlier_ones_applied(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("step", "b", "--do", "touch b.f", "--reversible", "no", "--notes", "cannot recall", check=0)
        sb.run("do", "a", check=0)
        sb.run("do", "b", "--yes", check=0)
        r = sb.run("undo", "--all", "--yes", check=3)
        self.assertIn("STOP at b", r.all)
        self.assertTrue(sb.exists("a.f"))  # we never reached a: reverse order stopped at b

    def test_drift_blocks_snapshot_restore_unless_forced(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo agent > a.txt", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=0)
        sb.write("a.txt", "a human edited this later\n")
        r = sb.run("undo", "--yes", check=3)
        self.assertIn("DRIFT", r.all)
        self.assertEqual(sb.read("a.txt"), "a human edited this later\n")
        sb.run("undo", "--yes", "--force", check=0)
        self.assertEqual(sb.read("a.txt"), "orig\n")

    def test_step_without_undo_or_snapshot_blocks_revert(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo hi", check=0)
        sb.run("do", "s1", check=0)
        r = sb.run("undo", "--yes", check=3)
        self.assertIn("no undo command and no snapshot", r.all)

    def test_secrets_in_runtime_output_never_reach_the_journal_or_timeline(self):
        sb = self.sandbox()
        sb.write("creds.txt", "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 password=hunter2\nsk-abcdefghijklmnopqrstuv\n")
        sb.run("step", "s1", "--do", "cat creds.txt", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        text = (sb.tl / "journal.jsonl").read_text() + (sb.tl / "TIMELINE.md").read_text()
        for leaked in ("ghp_abcdefghijklmnop", "hunter2", "sk-abcdefghijklmnop"):
            self.assertNotIn(leaked, text)
        self.assertIn("[REDACTED]", text)

    def test_step_refuses_to_store_a_secret_in_the_plan(self):
        sb = self.sandbox()
        r = sb.run("step", "s1", "--do", "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz123' x", check=2)
        self.assertIn("secret", r.all)
        self.assertEqual(sb.plan()["steps"], [])

    def test_env_var_references_are_not_mistaken_for_secrets(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo password=$DB_PASS token={{tok}}", "--undo", "true", check=0)

    def test_check_still_warns_if_a_secret_was_hand_edited_into_plan_json(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        plan = sb.plan()
        plan["steps"][0]["do"] = "echo deploy --key sk-abcdefghijklmnopqrstuvwxyz"
        (sb.tl / "plan.json").write_text(json.dumps(plan))
        self.assertIn("secret", sb.run("check").all)
        sb.run("do", "s1", check=0)
        self.assertNotIn("sk-abcdefghijklmnop", (sb.tl / "TIMELINE.md").read_text() + (sb.tl / "journal.jsonl").read_text())

    def test_touching_ledger_or_project_root_is_refused(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--touches", ".back-in-time", check=2)
        sb.run("step", "s1", "--do", "true", "--touches", ".", check=2)


class TestFailuresAndGuards(BitTest):
    def test_failed_step_is_traced_and_still_revertible(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo half > a.txt; exit 7", "--touches", "a.txt", check=0)
        r = sb.run("do", "s1", check=1)
        self.assertIn("FAIL s1", r.all)
        done = [e for e in sb.events() if e["event"] == "step_done"][0]
        self.assertEqual((done["status"], done["exit"]), ("failed", 7))
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("a.txt"), "orig\n")

    def test_verify_failure_marks_step_failed(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--verify", "test -f nope.txt", "--undo", "true", check=0)
        r = sb.run("do", "s1", check=1)
        self.assertIn("verify failed", r.all)

    def test_precondition_failure_blocks_without_running_do(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--pre", "test -f needed.txt", "--do", "touch ran.txt", "--undo", "true", check=0)
        r = sb.run("do", "s1", check=1)
        self.assertIn("BLOCKED s1", r.all)
        self.assertFalse(sb.exists("ran.txt"))
        sb.write("needed.txt", "x")
        sb.run("do", "s1", check=0)
        self.assertTrue(sb.exists("ran.txt"))

    def test_retry_after_failure_keeps_the_original_snapshot(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo partial >> a.txt; test -f ok.flag", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=1)
        sb.write("ok.flag", "")
        sb.run("do", "s1", check=0)  # a.txt now has two 'partial' lines
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("a.txt"), "orig\n")

    def test_double_apply_is_refused(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        sb.run("do", "s1", check=2)

    def test_undefined_variable_fails_closed_before_running(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "touch {{nope}}.f", "--undo", "true", check=0)
        r = sb.run("do", "s1", check=2)
        self.assertIn("undefined variable", r.all)
        self.assertFalse(sb.exists(".f"))

    def test_check_flags_unknown_variable_and_missing_undo(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo {{ghost}}", check=0)
        r = sb.run("check", check=0)  # warnings, not errors: `set ghost=...` may come later
        self.assertIn("ghost", r.all)
        self.assertIn("no undo", r.all)
        sb.run("do", "s1", check=2)  # but it can never run with a hole in it
        sb.run("set", "ghost=boo", check=0)
        self.assertIn("boo", sb.run("do", "s1", check=0).all)

    def test_undo_manual_shows_the_values_set_during_the_run(self):
        sb = self.sandbox()
        sb.run("step", "n", "--manual", "make page", "--undo-manual", "delete the page at {{page_url}}", check=0)
        self.assertNotIn("page_url", sb.run("check", check=0).all)  # runtime-set values aren't lint noise here
        sb.run("begin", "n", check=0)
        sb.run("set", "page_url=https://example.test/p/1", check=0)
        sb.run("done", "n", check=0)
        self.assertIn("https://example.test/p/1", sb.run("undo", "--yes", check=3).all)

    def test_timeout_is_recorded_as_failure(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "sleep 5", "--undo", "true", check=0)
        env = dict(os.environ, BIT_TIMEOUT="1")
        r = subprocess.run([sys.executable, str(sb.bit), "do", "s1"], cwd=str(sb.dir), capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out", r.stdout)


class TestUndoComposition(BitTest):
    def test_snapshot_is_restored_first_then_the_undo_command_runs_on_top(self):
        sb = self.sandbox()
        sb.write("lock.json", "v1\n")
        sb.run("step", "s1", "--do", "echo v2 > lock.json", "--touches", "lock.json",
               "--undo", "echo \"reinstalled from $(cat lock.json)\" > installed.txt", check=0)
        sb.run("do", "s1", check=0)
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("lock.json"), "v1\n")
        self.assertEqual(sb.read("installed.txt"), "reinstalled from v1\n")

    def test_manual_undo_pauses_then_resumes_after_reverted_is_recorded(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("step", "b", "--manual", "create the wiki page", "--undo-manual", "delete the wiki page 'Runbook'", check=0)
        sb.run("do", "a", check=0)
        sb.run("begin", "b", check=0)
        sb.run("done", "b", "--note", "created page", check=0)
        r = sb.run("undo", "--all", "--yes", check=3)
        self.assertIn("MANUAL UNDO", r.all)
        self.assertIn("delete the wiki page", r.all)
        self.assertTrue(sb.exists("a.f"))
        sb.run("reverted", "b", "--note", "deleted via MCP", check=0)
        sb.run("undo", "--all", "--yes", check=0)
        self.assertFalse(sb.exists("a.f"))
        evs = [e for e in sb.events() if e["event"] == "step_reverted"]
        self.assertEqual([(e["step"], e["method"]) for e in evs], [("b", "manual"), ("a", "command")])

    def test_reverted_refuses_a_step_that_was_never_applied(self):
        sb = self.sandbox()
        sb.run("step", "a", "--manual", "x", "--undo-manual", "y", check=0)
        sb.run("reverted", "a", check=2)

    def test_manual_undo_counts_as_reversible_for_check(self):
        sb = self.sandbox()
        sb.run("step", "a", "--manual", "x", "--undo-manual", "y", "--verify", "true", check=0)
        self.assertNotIn("no undo", sb.run("check", check=0).all)


class TestParameterisedPaths(BitTest):
    def test_touches_expand_run_variables_so_undo_removes_the_real_file(self):
        sb = self.sandbox(params=["env=staging"])
        sb.run("step", "s1", "--title", "make {{env}} file", "--do", "echo x > .env.{{env}}", "--touches", ".env.{{env}}", check=0)
        r = sb.run("do", "s1", check=0)
        self.assertIn("make staging file", r.all)  # titles show the run's values too
        self.assertTrue(sb.exists(".env.staging"))
        sb.run("undo", "--yes", check=0)
        self.assertFalse(sb.exists(".env.staging"))

    def test_replay_with_new_param_snapshots_the_new_path(self):
        sb = self.sandbox(params=["env=staging"])
        sb.run("step", "s1", "--do", "echo x > .env.{{env}}", "--touches", ".env.{{env}}", check=0)
        sb.run("auto", check=0)
        sb.run("replay", "--set", "env=prod", "--auto", check=0)
        self.assertTrue(sb.exists(".env.prod"))
        sb.run("undo", "--all", "--yes", check=0)
        self.assertFalse(sb.exists(".env.prod"))
        self.assertTrue(sb.exists(".env.staging"))  # run 1's effects are untouched by run 2's undo
        sb.run("undo", "--all", "--run", "1", "--yes", check=0)
        self.assertFalse(sb.exists(".env.staging"))

    def test_wrong_touches_path_is_called_out_loudly(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo x > real.txt", "--touches", "typo.txt", check=0)
        r = sb.run("do", "s1", check=0)
        self.assertIn("none of the declared --touches changed", r.all)

    def test_no_warning_when_touches_did_change(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo x > real.txt", "--touches", "real.txt", check=0)
        self.assertNotIn("WARNING", sb.run("do", "s1", check=0).all)

    def test_check_flags_unknown_variable_in_touches(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--touches", "x.{{nope}}", check=0)
        self.assertIn("nope", sb.run("check", check=0).all)


class TestManualSteps(BitTest):
    def test_begin_done_snapshots_work_the_agent_did_itself(self):
        sb = self.sandbox()
        sb.write("conf.ini", "debug=0\n")
        sb.run("step", "s1", "--manual", "flip debug on in conf.ini", "--touches", "conf.ini", check=0)
        sb.run("begin", "s1", check=0)
        sb.write("conf.ini", "debug=1\n")  # the agent's own Edit tool
        sb.run("done", "s1", "--note", "set debug=1", check=0)
        done = [e for e in sb.events() if e["event"] == "step_done"][0]
        self.assertEqual(done["how"], "manual")
        self.assertEqual(done["changed"][0]["path"], "conf.ini")
        self.assertIn("-debug=0", (sb.tl / "diffs" / "r1-s1.diff").read_text())
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("conf.ini"), "debug=0\n")

    def test_do_on_a_manual_step_points_at_begin_and_done(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--manual", "click the thing", "--undo", "true", check=0)
        r = sb.run("do", "s1", check=3)
        self.assertIn("MANUAL s1", r.all)

    def test_done_without_begin_warns_and_blocks_snapshot_undo(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--manual", "x", "--touches", "a.txt", check=0)
        r = sb.run("done", "s1", check=0)
        self.assertIn("no `begin", r.all)
        self.assertIn("no undo command and no snapshot", sb.run("undo", "--yes", check=3).all)

    def test_done_failed_records_failure(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--manual", "x", "--undo", "true", check=0)
        sb.run("begin", "s1", check=0)
        sb.run("done", "s1", "--failed", "--note", "could not log in", check=1)
        self.assertEqual([e for e in sb.events() if e["event"] == "step_done"][0]["status"], "failed")

    def test_path_outside_project_root_is_snapshotted_and_restored(self):
        sb = self.sandbox()
        outside = Path(tempfile.mkdtemp(prefix="bit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, str(outside), True)
        f = outside / "global.cfg"
        f.write_text("v1\n")
        sb.run("step", "s1", "--do", "echo v2 > %s" % f, "--touches", str(f), check=0)
        sb.run("do", "s1", check=0)
        self.assertEqual(f.read_text(), "v2\n")
        sb.run("undo", "--yes", check=0)
        self.assertEqual(f.read_text(), "v1\n")


class TestReplay(BitTest):
    def build(self, sb):
        sb.run("step", "s1", "--title", "make dir", "--do", "mkdir -p out", "--undo", "rmdir out",
               "--skip-if", "test -d out", check=0)
        sb.run("step", "s2", "--title", "write file", "--do", "echo {{who}} > out/hello.txt",
               "--touches", "out/hello.txt", "--verify", "grep -q {{who}} out/hello.txt", check=0)

    def test_replay_after_full_revert_reproduces_the_result(self):
        sb = self.sandbox(params=["who=world"])
        self.build(sb)
        sb.run("auto", check=0)
        self.assertEqual(sb.read("out/hello.txt"), "world\n")
        sb.run("undo", "--all", "--yes", check=0)
        self.assertFalse(sb.exists("out"))
        r = sb.run("replay", "--auto", check=0)
        self.assertIn("Run 2 started", r.all)
        self.assertEqual(sb.read("out/hello.txt"), "world\n")
        runs = {e["run"] for e in sb.events()}
        self.assertEqual(runs, {1, 2})

    def test_replay_with_new_parameter_changes_the_outcome(self):
        sb = self.sandbox(params=["who=world"])
        self.build(sb)
        sb.run("auto", check=0)
        sb.run("undo", "--all", "--yes", check=0)
        sb.run("replay", "--set", "who=staging", "--auto", check=0)
        self.assertEqual(sb.read("out/hello.txt"), "staging\n")

    def test_skip_if_makes_replay_on_an_already_applied_world_safe(self):
        sb = self.sandbox(params=["who=world"])
        self.build(sb)
        sb.run("auto", check=0)
        r = sb.run("replay", "--auto", check=0)  # nothing was undone
        self.assertIn("SKIP s1", r.all)
        self.assertEqual(sb.read("out/hello.txt"), "world\n")

    def test_replay_from_treats_earlier_steps_as_already_done(self):
        sb = self.sandbox(params=["who=w"])
        self.build(sb)
        sb.write("out/seed", "")  # the world already has what s1 would have made
        sb.run("replay", "--from", "s2", "--auto", check=0)
        self.assertEqual(sb.read("out/hello.txt"), "w\n")
        run2 = [e for e in sb.events() if e["run"] == 2 and e.get("step") == "s1"]
        self.assertEqual([e["event"] for e in run2], ["step_done"])  # recorded as skipped, never executed
        self.assertEqual(run2[0]["status"], "skipped")

    def test_bit_set_values_flow_into_later_steps_and_undo(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo BIT_SET id=abc123", "--undo", "echo removed-{{id}} > undone.txt", check=0)
        sb.run("step", "s2", "--do", "echo created-{{id}} > made.txt", "--undo", "rm -f made.txt", check=0)
        sb.run("auto", check=0)
        self.assertEqual(sb.read("made.txt"), "created-abc123\n")
        sb.run("undo", "--all", "--yes", check=0)
        self.assertEqual(sb.read("undone.txt"), "removed-abc123\n")

    def test_auto_pauses_at_manual_step_and_resumes_after_done(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("step", "m", "--manual", "do something by hand", "--undo", "true", check=0)
        sb.run("step", "c", "--do", "touch c.f", "--undo", "rm -f c.f", check=0)
        r = sb.run("auto", check=3)
        self.assertIn("MANUAL m", r.all)
        self.assertFalse(sb.exists("c.f"))
        sb.run("begin", "m", check=0)
        sb.run("done", "m", check=0)
        sb.run("auto", check=0)
        self.assertTrue(sb.exists("c.f"))

    def test_undo_run_one_after_a_replay_started(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("auto", check=0)
        sb.run("replay", check=0)
        self.assertIn("Nothing to undo", sb.run("undo", "--all", "--yes", check=0).all)
        sb.run("undo", "--all", "--run", "1", "--yes", check=0)
        self.assertFalse(sb.exists("a.f"))

    def test_verify_detects_drift_and_failed_checks(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "echo x > f.txt", "--touches", "f.txt", "--verify", "test -f f.txt", check=0)
        sb.run("do", "s1", check=0)
        sb.run("verify", check=0)
        sb.write("f.txt", "tampered\n")
        self.assertIn("DRIFT", sb.run("verify", check=1).all)


class TestFork(BitTest):
    def test_fork_carries_the_plan_to_another_checkout_with_fresh_history(self):
        sb = self.sandbox(params=["env=staging"])
        sb.run("step", "s1", "--do", "echo {{env}} > deployed.txt", "--touches", "deployed.txt", check=0)
        sb.run("do", "s1", check=0)
        other = Path(tempfile.mkdtemp(prefix="bit-fork-")).resolve()
        self.addCleanup(shutil.rmtree, str(other), True)
        r = sb.run("fork", str(other), "--set", "env=prod", "--auto", check=0)
        self.assertIn("Forked", r.all)
        self.assertEqual((other / "deployed.txt").read_text(), "prod\n")
        self.assertEqual(sb.read("deployed.txt"), "staging\n")  # the original checkout is untouched
        self.assertTrue((other / ".back-in-time" / "bit.py").exists())
        slug = next(d for d in (other / ".back-in-time").iterdir() if (d / "plan.json").exists())
        events = [json.loads(l) for l in (slug / "journal.jsonl").read_text().splitlines()]
        self.assertEqual(events[0]["kind"], "fork")
        self.assertEqual({e["run"] for e in events}, {1})  # fresh history, not run N+1 of the old one
        self.assertIn("Forked from", (slug / "TIMELINE.md").read_text())
        # and the fork is a fully working ledger of its own
        out = subprocess.run([sys.executable, str(other / ".back-in-time" / "bit.py"), "undo", "--all", "--yes"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse((other / "deployed.txt").exists())

    def test_fork_into_the_same_directory_is_refused(self):
        sb = self.sandbox()
        sb.run("fork", str(sb.dir), check=2)


class TestDocsAndState(BitTest):
    def test_timeline_md_tells_a_cold_reader_what_to_do(self):
        sb = self.sandbox(title="Rotate keys", goal="Rotate the staging keys")
        sb.run("step", "s1", "--title", "gen", "--do", "true", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        md = (sb.tl / "TIMELINE.md").read_text()
        for needle in ("# Rotate keys", "Rotate the staging keys", "bit.py", "undo --all --yes", "replay --auto", "s1"):
            self.assertIn(needle, md)

    def test_plan_edits_after_execution_started_are_traced(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        sb.run("step", "s2", "--do", "true", "--undo", "true", check=0)
        self.assertIn("plan_changed", [e["event"] for e in sb.events()])

    def test_next_after_full_revert_does_not_push_you_to_reapply(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        sb.run("undo", "--all", "--yes", check=0)
        self.assertIn("reverted", sb.run("next", check=0).all)

    def test_multiple_timelines_need_an_explicit_choice(self):
        sb = self.sandbox(title="one")
        sb.run("init", "two", check=0)
        slugs = sorted(d.name for d in (sb.dir / ".back-in-time").iterdir() if (d / "plan.json").exists())
        self.assertEqual(len(slugs), 2)
        self.assertIn("two", sb.run("list", check=0).all)
        sb.run("--version", check=0)
        self.assertIn(slugs[0], sb.run("use", slugs[0], check=0).all)

    def test_survives_a_torn_journal_line(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        with open(str(sb.tl / "journal.jsonl"), "a") as f:
            f.write('{"ts": "2026-01-01T00:00:00Z", "run": 1, "ev')  # half-written
        self.assertIn("applied", sb.run("status", check=0).all)

    def test_runs_from_any_directory_via_the_ledger_copy(self):
        sb = self.sandbox()
        sub = sb.dir / "deep" / "er"
        sub.mkdir(parents=True)
        r = subprocess.run([sys.executable, str(sb.bit), "status"], cwd=str(sub), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestReviewRegressions(BitTest):
    """One test per defect found by the adversarial review (each failed against v0.1.0)."""

    def raw(self, sb, *args, env=None, cwd=None):
        e = dict(os.environ)
        e.update(env or {})
        r = subprocess.run([sys.executable, str(sb.bit)] + list(args), cwd=str(cwd or sb.dir), capture_output=True, text=True, env=e)
        r.all = r.stdout + r.stderr
        return r

    # --- every command the tool prints must be runnable as printed
    def test_global_flags_work_before_and_after_the_subcommand(self):
        sb = self.sandbox()
        slug = sb.tl.name
        self.assertEqual(self.raw(sb, "--tl", slug, "status").returncode, 0)
        self.assertEqual(self.raw(sb, "status", "--tl", slug).returncode, 0)
        r = self.raw(sb, "--tl", slug, "step", "s1", "--do", "true", "--undo", "true")
        self.assertEqual(r.returncode, 0, r.all)

    def test_every_command_printed_in_timeline_md_runs(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("do", "s1", check=0)
        md = (sb.tl / "TIMELINE.md").read_text()
        block = md.split("```bash")[1].split("```")[0]
        ran = 0
        for line in block.splitlines():
            line = line.split("  #")[0].strip()
            if not line or "<" in line or "--yes" in line or "replay" in line:
                continue
            argv = shlex.split(line)
            self.assertEqual(argv[0], "python3")
            r = subprocess.run([sys.executable] + argv[1:], cwd=str(sb.dir), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, "%r -> %s%s" % (line, r.stdout, r.stderr))
            ran += 1
        self.assertGreaterEqual(ran, 3)

    def test_hints_use_a_path_that_works_from_other_directories(self):
        sb = self.sandbox()
        sub = sb.dir / "deep"
        sub.mkdir()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        r = self.raw(sb, "next", cwd=sub)
        self.assertIn(str(sb.bit), r.all)  # absolute, since the relative path would not resolve from here

    # --- snapshots
    def test_redo_after_undo_takes_a_fresh_snapshot(self):
        sb = self.sandbox()
        sb.write("app.cfg", "base\n")
        sb.run("step", "s1", "--do", "touch s1.f", "--undo", "rm -f s1.f", check=0)
        sb.run("step", "s2", "--do", "echo '#s2' >> app.cfg", "--touches", "app.cfg", check=0)
        sb.run("auto", check=0)
        sb.run("undo", "s2", "--yes", check=0)
        sb.write("app.cfg", "base\nuser-line-added-after-rewind\n")
        sb.run("auto", check=0)
        sb.run("undo", "--all", "--yes", check=0)
        self.assertEqual(sb.read("app.cfg"), "base\nuser-line-added-after-rewind\n")

    def test_widened_touches_on_a_retry_are_snapshotted(self):
        sb = self.sandbox()
        sb.write("a.txt", "a0\n")
        sb.write("b.txt", "b0\n")
        sb.run("step", "s1", "--do", "echo a1 > a.txt; test -f ok.flag && echo b1 > b.txt", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=1)
        sb.run("step", "s1", "--touches", "a.txt", "b.txt", check=0)
        sb.write("ok.flag", "")
        sb.run("do", "s1", check=0)
        sb.run("undo", "--yes", check=0)
        self.assertEqual((sb.read("a.txt"), sb.read("b.txt")), ("a0\n", "b0\n"))

    def test_touching_a_symlink_also_tracks_and_restores_its_target(self):
        sb = self.sandbox()
        sb.write("dotfiles/zshrc", "orig\n")
        os.symlink("dotfiles/zshrc", str(sb.dir / ".zshrc"))
        sb.run("step", "s1", "--do", "echo added >> .zshrc", "--touches", ".zshrc", check=0)
        sb.run("do", "s1", check=0)
        self.assertEqual(sb.read("dotfiles/zshrc"), "orig\nadded\n")
        sb.run("undo", "--yes", check=0)
        self.assertEqual(sb.read("dotfiles/zshrc"), "orig\n")
        self.assertTrue(os.path.islink(str(sb.dir / ".zshrc")))

    def test_undo_refuses_when_the_snapshot_store_is_missing(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo n > a.txt", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=0)
        shutil.rmtree(str(sb.tl / "snapshots"))
        r = sb.run("undo", "--yes", check=3)
        self.assertIn("snapshot", r.all.lower())
        self.assertEqual(sb.read("a.txt"), "n\n")

    def test_undo_refuses_when_a_stored_copy_is_missing_instead_of_deleting_the_live_file(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo n > a.txt", "--touches", "a.txt", check=0)
        sb.run("do", "s1", check=0)
        shutil.rmtree(str(sb.tl / "snapshots" / "r1-s1" / "files"))
        sb.run("undo", "--yes", check=3)
        self.assertEqual(sb.read("a.txt"), "n\n")

    def test_directory_over_the_snapshot_limit_fails_closed_before_anything_runs(self):
        sb = self.sandbox()
        sb.write("big/a.bin", "x" * 5000)
        sb.run("step", "s1", "--do", "rm -rf big", "--touches", "big", check=0)
        r = self.raw(sb, "do", "s1", env={"BIT_MAX_SNAPSHOT_MB": "0.001"})
        self.assertEqual(r.returncode, 2, r.all)
        self.assertIn("snapshot", r.all.lower())
        self.assertTrue(sb.exists("big/a.bin"))

    # --- state machine
    def test_skip_if_does_not_hide_a_failed_step(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "touch half; test -f ok.flag", "--skip-if", "test -f half", "--undo", "rm -f half", check=0)
        sb.run("do", "s1", check=1)
        sb.write("ok.flag", "")
        r = sb.run("do", "s1", check=0)
        self.assertNotIn("SKIP", r.all)
        done = [e for e in sb.events() if e["event"] == "step_done"]
        self.assertEqual(done[-1]["status"], "applied")

    def test_removing_an_applied_step_is_refused_until_it_is_undone(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("do", "s1", check=0)
        r = sb.run("step", "s1", "--remove", check=2)
        self.assertIn("undo", r.all)
        self.assertEqual(len(sb.plan()["steps"]), 1)
        sb.run("undo", "--yes", check=0)
        sb.run("step", "s1", "--remove", check=0)

    def test_hand_edit_that_drops_an_applied_step_is_called_out(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("step", "s2", "--do", "touch b.f", "--undo", "rm -f b.f", check=0)
        sb.run("auto", check=0)
        plan = sb.plan()
        plan["steps"] = [s for s in plan["steps"] if s["id"] != "s1"]
        (sb.tl / "plan.json").write_text(json.dumps(plan))
        self.assertIn("no longer in the plan", sb.run("check", check=2).all)
        r = sb.run("undo", "--all", "--yes", check=3)
        self.assertIn("no longer in the plan", r.all)
        self.assertTrue(sb.exists("a.f"))  # nothing was claimed as reverted for it

    def test_blocked_undo_explains_keep_going_and_keep_going_reverts_the_rest(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("step", "b", "--do", "touch b.f", "--reversible", "no", "--notes", "sent", check=0)
        sb.run("do", "a", check=0)
        sb.run("do", "b", "--yes", check=0)
        r = sb.run("undo", "--all", check=0)
        self.assertIn("--keep-going", r.all)
        sb.run("undo", "--all", "--yes", "--keep-going", check=3)
        self.assertFalse(sb.exists("a.f"))
        self.assertTrue(sb.exists("b.f"))

    def test_status_and_undo_point_at_an_earlier_run_that_is_still_applied(self):
        sb = self.sandbox()
        sb.run("step", "a", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("do", "a", check=0)
        sb.run("replay", check=0)
        r = sb.run("status", check=0)
        self.assertIn("run 1", r.all)
        self.assertIn("still applied", r.all.lower())
        self.assertIn("--run 1", sb.run("undo", "--all", check=0).all)

    def test_two_steps_touching_one_file_undo_cleanly_and_verify_has_no_false_drift(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo 1 >> a.txt", "--touches", "a.txt", check=0)
        sb.run("step", "s2", "--do", "echo 2 >> a.txt", "--touches", "a.txt", check=0)
        sb.run("auto", check=0)
        sb.run("verify", check=0)
        sb.run("undo", "--all", "--yes", check=0)
        self.assertEqual(sb.read("a.txt"), "orig\n")

    def test_retry_after_a_failed_undo_command_is_not_blocked_by_its_own_restore(self):
        sb = self.sandbox()
        sb.write("a.txt", "orig\n")
        sb.run("step", "s1", "--do", "echo n > a.txt", "--touches", "a.txt", "--undo", "test -f undo-ok.flag", check=0)
        sb.run("do", "s1", check=0)
        sb.run("undo", "--yes", check=1)
        self.assertEqual(sb.read("a.txt"), "orig\n")
        sb.write("undo-ok.flag", "")
        r = sb.run("undo", "--yes", check=0)
        self.assertNotIn("DRIFT", r.all)

    def test_replay_from_an_unknown_step_does_not_start_a_run(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "touch a.f", "--undo", "rm -f a.f", check=0)
        sb.run("do", "s1", check=0)
        sb.run("replay", "--from", "nope", check=2)
        self.assertEqual({e["run"] for e in sb.events()}, {1})
        self.assertNotIn("Nothing to undo", sb.run("undo", "--all", check=0).all)

    def test_step_ids_differing_only_by_case_are_refused(self):
        sb = self.sandbox()
        sb.run("step", "Build", "--do", "true", "--undo", "true", check=0)
        sb.run("step", "build", "--do", "true", "--undo", "true", check=2)

    # --- journal robustness and concurrency
    def test_torn_multibyte_journal_tail_neither_crashes_nor_swallows_the_next_event(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "true", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        with open(str(sb.tl / "journal.jsonl"), "ab") as f:
            f.write('{"ts": "x", "run": 1, "note": "caf'.encode("utf-8") + b"\xc3")
        sb.run("status", check=0)
        sb.run("note", "after-the-tear", check=0)
        last = (sb.tl / "journal.jsonl").read_bytes().splitlines()[-1]
        self.assertEqual(json.loads(last)["text"], "after-the-tear")

    def test_parallel_step_calls_do_not_lose_steps(self):
        sb = self.sandbox()
        procs = [subprocess.Popen([sys.executable, str(sb.bit), "step", "s%d" % i, "--do", "true", "--undo", "true"],
                                  cwd=str(sb.dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(12)]
        for p in procs:
            p.communicate()
        self.assertEqual(len({s["id"] for s in sb.plan()["steps"]}), 12)

    def test_timeout_kills_the_whole_process_group_including_background_children(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "sleep 30 & echo $! > child.pid; wait", "--undo", "true", check=0)
        r = self.raw(sb, "do", "s1", env={"BIT_TIMEOUT": "1"})
        self.assertEqual(r.returncode, 1, r.all)
        pid = int(sb.read("child.pid"))
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    # --- secrets and trust
    def test_diffs_directory_is_gitignored_because_it_holds_file_contents(self):
        sb = self.sandbox()
        self.assertIn("diffs", (sb.dir / ".back-in-time" / ".gitignore").read_text())

    def test_secret_looking_parameters_are_refused_everywhere_they_can_be_stored(self):
        sb = self.sandbox()
        tok = "sk-abcdefghijklmnopqrstuvwxyz"
        sb.run("set", "api_token=" + tok, check=2)
        sb.run("set", "ver=2.1.0", "token=$FROM_ENV", check=0)  # variable references are fine
        sb.run("replay", "--set", "db_password=hunter22222", check=2)
        r = subprocess.run([sys.executable, str(sb.bit), "init", "x", "--param", "key=" + tok, "--root", str(sb.dir)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        sb.run("init", "x2", "--goal", "rotate; password=hunter2 is old", check=2)
        text = (sb.tl / "journal.jsonl").read_text() + (sb.tl / "TIMELINE.md").read_text()
        self.assertNotIn(tok, text)
        self.assertNotIn("hunter2", text)

    def test_bit_set_rejects_shell_metacharacters_and_cannot_override_declared_params(self):
        sb = self.sandbox(params=["ver=1.0"])
        sb.run("step", "s1", "--do",
               'echo "BIT_SET ver=9.9"; echo "BIT_SET evil=\\$(touch pwned)"; echo "BIT_SET good=abc-123_x.y/z"',
               "--undo", "true", check=0)
        sb.run("step", "s2", "--do", "echo {{ver}} {{good}} > out.txt", "--undo", "true", check=0)
        sb.run("step", "s3", "--do", "echo {{evil}}", "--undo", "true", check=0)
        sb.run("do", "s1", check=0)
        sb.run("do", "s2", check=0)
        self.assertEqual(sb.read("out.txt"), "1.0 abc-123_x.y/z\n")
        sb.run("do", "s3", check=2)
        self.assertFalse(sb.exists("pwned"))

    def test_quoted_env_var_references_are_not_mistaken_for_secrets(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "curl -H \"Authorization: Bearer $TOKEN\" --data 'password=\"$DB_PASS\"' x",
               "--undo", "true", check=0)

    def test_plausible_config_names_are_not_mistaken_for_secrets(self):
        sb = self.sandbox()
        for text in ("python train.py --max_tokens=100 --token_limit=5000", "password_policy=strict pwd=/home/me keyboard=us",
                     "echo token=$FROM_ENV client_secret=\"$CS\""):
            sb.run("step", "s_%d" % abs(hash(text) % 1000), "--do", text, "--undo", "true", check=0)
        sb.run("set", "max_tokens=100", "password_policy=strict", check=0)
        r = subprocess.run([sys.executable, str(sb.bit), "init", "x", "--param", "max_tokens=100", "--root", str(sb.dir)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_real_secret_assignments_are_still_caught(self):
        sb = self.sandbox()
        for text in ("db_password=hunter22222", "password=hunter2", "client_secret: abcdefgh12", "mytoken=abcdef1234",
                     "api_key=abcdefghijklmnop"):
            sb.run("step", "bad", "--do", "echo " + text, "--undo", "true", check=2)

    # --- docs the tool writes
    def test_timeline_md_tells_a_cold_reader_not_to_delete_the_ledger_and_that_plans_run_code(self):
        sb = self.sandbox()
        md = (sb.tl / "TIMELINE.md").read_text().lower()
        self.assertIn("keep this folder", md)
        self.assertIn("runs the shell commands", md)

    # --- undo --merge: keep later edits, remove only what the step added
    def test_undo_merge_removes_only_the_steps_own_lines_and_keeps_later_edits(self):
        sb = self.sandbox()
        sb.write(".zshrc", "a\nb\nc\n")
        sb.run("step", "s1", "--do", "printf 'export X=1\\nexport Y=2\\n' >> .zshrc", "--touches", ".zshrc", check=0)
        sb.run("do", "s1", check=0)
        sb.write(".zshrc", sb.read(".zshrc") + "human-line\n")
        r = sb.run("undo", "--yes", check=3)
        self.assertIn("--merge", r.all)
        self.assertIn("human-line", sb.read(".zshrc"))
        sb.run("undo", "--yes", "--merge", check=0)
        self.assertEqual(sb.read(".zshrc"), "a\nb\nc\nhuman-line\n")

    def test_undo_merge_blocks_on_a_real_conflict_and_changes_nothing(self):
        sb = self.sandbox()
        sb.write(".zshrc", "a\nb\nc\n")
        sb.run("step", "s1", "--do", "sed 's/^b$/B/' .zshrc > .z && mv .z .zshrc", "--touches", ".zshrc", check=0)
        sb.run("do", "s1", check=0)
        sb.write(".zshrc", "a\nB-edited-by-human\nc\n")
        r = sb.run("undo", "--yes", "--merge", check=3)
        self.assertIn("conflict", r.all.lower())
        self.assertEqual(sb.read(".zshrc"), "a\nB-edited-by-human\nc\n")

    def test_undo_merge_of_a_created_file_keeps_what_was_appended_later(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "printf 'one\\ntwo\\n' > new.txt", "--touches", "new.txt", check=0)
        sb.run("do", "s1", check=0)
        sb.write("new.txt", sb.read("new.txt") + "more\n")
        sb.run("undo", "--yes", "--merge", check=0)
        self.assertEqual(sb.read("new.txt"), "more\n")

    def test_undo_merge_of_a_created_file_nobody_touched_deletes_it(self):
        sb = self.sandbox()
        sb.run("step", "s1", "--do", "printf 'one\\ntwo\\n' > new.txt", "--touches", "new.txt", check=0)
        sb.run("do", "s1", check=0)
        sb.run("undo", "--yes", "--merge", check=0)
        self.assertFalse(sb.exists("new.txt"))


if __name__ == "__main__":
    unittest.main()
