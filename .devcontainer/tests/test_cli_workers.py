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
codex = pathlib.Path(sys.argv[0]).name == 'codex'
resume = 'resume' if codex else '--resume'
session = args[args.index(resume) + 1] if resume in args else str(uuid.uuid4())
def emit(event):
    print(json.dumps(event), flush=True)
emit({'type': 'thread.started', 'thread_id': session} if codex else
     {'type': 'system', 'subtype': 'init', 'session_id': session})
prompt = sys.stdin.read()
pathlib.Path('.probe-prompt').write_text(prompt)
if 'WAIT_FOR_RESTART' in prompt and not pathlib.Path(os.environ['PROBE_GATE']).exists():
    time.sleep(120)
report = {'status': 'completed', 'summary': session, 'tests': 'fixture'}
if codex:
    emit({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(report)}})
    emit({'type':'turn.completed'})
else:
    emit({'type':'result','subtype':'success','structured_output':report})
"""


class WorkerRecovery(unittest.TestCase):
    def test_restart_preserves_queue_and_native_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = Path(__file__).resolve().parents[1] / "project-cli"
            state = root / "hermes"
            (state / "plugins").mkdir(parents=True)
            (state / "plugins/project-cli").symlink_to(plugin)
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
                worker.kill()
                worker.wait(timeout=10)
                worker = serve()
                wait_for(first, "blocked")
                wait_for(follow, "done")
                self.assertEqual(query("SELECT count(*) FROM task_runs WHERE task_id=?", first), 1)
                gate.touch()
                subprocess.run(["hermes", "kanban", "--board", "cli-workers", "unblock", first],
                               env=env, stdout=subprocess.DEVNULL, check=True)
                wait_for(first, "done")
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
