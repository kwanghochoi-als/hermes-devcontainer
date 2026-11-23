"""Integration regression for checkout serialization and recovery after a worker dies."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest

FAKE_CLI = """#!/usr/bin/env python3
import json, os, pathlib, sys, time, uuid
args = sys.argv[1:]
pathlib.Path('.probe-args.json').write_text(json.dumps(args))
codex = pathlib.Path(sys.argv[0]).name == 'codex'
resume = 'resume' if codex else '--resume'
session = args[args.index(resume) + 1] if resume in args else str(uuid.uuid4())
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'thread.started', 'thread_id': session} if codex else
     {'type': 'system', 'subtype': 'init', 'session_id': session})
prompt = sys.stdin.read()
pathlib.Path('.probe-prompt').write_text(prompt)
while 'WAIT_FOR_RESTART' in prompt and not pathlib.Path(os.environ['PROBE_GATE']).exists():
    time.sleep(0.2)
report = {'status': 'completed', 'summary': session, 'tests': 'fixture'}
fixture = pathlib.Path('.probe-result.json')
if fixture.exists():
    report.update(json.loads(fixture.read_text()))
if codex:
    emit({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(report)}})
    emit({'type':'turn.completed'})
else:
    emit({'type':'result','subtype':'success','structured_output':report})
"""


class WorkerRecovery(unittest.TestCase):
    def test_restart_preserves_queue_and_native_session(self):
        import fixture_transport
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "hermes"
            (state / "plugins").mkdir(parents=True)
            fixture_transport.install(state)
            (state / "config.yaml").write_text("plugins:\n  enabled: [project-cli]\n")
            bins = root / "bin"
            bins.mkdir()
            for name in ("codex", "claude"):
                (bins / name).write_text(FAKE_CLI)
                (bins / name).chmod(0o755)
            projects = root / "projects"
            for name in ("a", "b"):
                subprocess.run(["git", "init", "-q", str(projects / name)], check=True)
            gate = root / "gate"
            env = dict(os.environ, HERMES_HOME=str(state), HERMES_KANBAN_HOME=str(state),
                       HERMES_DASHBOARD_FILES_ROOT=str(projects), PROBE_GATE=str(gate),
                       PATH=f"{bins}:{os.environ['PATH']}")
            env.pop("HERMES_KANBAN_DB", None)

            def submit(project, agent, prompt):
                return subprocess.check_output(["hermes", "project-cli", "submit", agent, prompt,
                    "--session", "test-session"], cwd=projects / project, env=env, text=True).strip()

            first = submit("a", "codex", "WAIT_FOR_RESTART")
            follow = submit("a", "claude", "Review the same checkout")
            independent = submit("b", "claude", "Review the other checkout")
            db = state / "kanban/boards/cli-workers/kanban.db"
            def query(sql, *params):
                with sqlite3.connect(db) as conn:
                    row = conn.execute(sql, params).fetchone()
                    return row[0] if row else None

            def wait_for(card, status):
                deadline = time.monotonic() + 45
                while query("SELECT status FROM tasks WHERE id=?", card) != status:
                    if time.monotonic() >= deadline:
                        self.fail(f"{card} did not reach {status}")
                    time.sleep(0.2)

            def serve():
                return subprocess.Popen(["hermes", "project-cli", "serve"], cwd=root, env=env)

            worker = serve()
            try:
                wait_for(independent, "done")
                wait_for(first, "running")
                self.assertEqual(query("SELECT status FROM tasks WHERE id=?", follow), "ready")
                deadline = time.monotonic() + 30
                while not (comment := query("SELECT body FROM task_comments WHERE task_id=? AND author='project-cli'", first)):
                    if time.monotonic() >= deadline:
                        self.fail("The first CLI did not publish its session ID")
                    time.sleep(0.2)
                original_id = comment.split()[2]
                def inspect(*args):
                    return subprocess.check_output(["hermes", "kanban", "--board", "cli-workers", *args],
                                                   env=env, text=True)
                active = json.loads(inspect("runs", first, "--json"))[0]
                self.assertEqual(active["last_step"], "CLI 세션 연결")
                self.assertIn("thread.started", inspect("log", first, "--run", str(active["id"])))
                worker.kill()
                worker.wait(timeout=10)
                worker = serve()
                wait_for(first, "blocked")
                self.assertEqual(query("SELECT block_kind FROM tasks WHERE id=?", first), "transient")
                wait_for(follow, "done")
                self.assertEqual(query("SELECT count(*) FROM task_runs WHERE task_id=?", first), 1)
                gate.touch()
                subprocess.run(["hermes", "kanban", "--board", "cli-workers", "unblock", first],
                               env=env, stdout=subprocess.DEVNULL, check=True)
                wait_for(first, "done")
                first_try, retry = json.loads(inspect("runs", first, "--json"))
                self.assertEqual(first_try["last_step"], "CLI 세션 연결")
                self.assertEqual(retry["last_step"], "카드 완료 기록")
                self.assertNotIn("turn.completed", inspect("log", first, "--run", str(first_try["id"])))
                self.assertIn("turn.completed", inspect("log", first, "--run", str(retry["id"])))
                run_dir = state / "cli-workers" / "runs" / first / str(first_try["id"])
                stderr = run_dir / "stderr.log"
                stderr.write_text("API_KEY=sk-" + "x" * 48 + "\nlast diagnostic\n")
                log = inspect("log", first, "--run", str(first_try["id"]))
                self.assertIn("last diagnostic", log)
                self.assertNotIn("x" * 48, log)
                stderr.unlink()
                stderr.symlink_to(root / "unrelated")
                (root / "unrelated").write_text("private fixture")
                for task_id in (first, independent):
                    denied = subprocess.run(["hermes", "kanban", "--board", "cli-workers", "log", task_id,
                                             "--run", str(first_try["id"])], env=env, capture_output=True, text=True)
                    self.assertNotEqual(denied.returncode, 0)
                    self.assertNotIn("private fixture", denied.stdout)
                meta = query("SELECT metadata FROM task_runs WHERE task_id=? AND outcome='completed'", first)
                self.assertEqual(json.loads(meta)["cli_session_id"], original_id)
                other = submit("b", "codex", "Check the other checkout")
                wait_for(other, "done")
                self.assertNotIn("WAIT_FOR_RESTART", (projects / "b/.probe-prompt").read_text())
            finally:
                worker.terminate()
                worker.wait(timeout=15)


if __name__ == "__main__":
    unittest.main()
