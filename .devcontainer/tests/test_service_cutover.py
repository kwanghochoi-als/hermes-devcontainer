"""Exercise the deployment gate without starting a production gateway or using bot credentials."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4


HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hermes_volume", HERE / "hermes_volume.py")
volume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume)
INSTALL = Path(os.environ.get("HERMES_TEST_INSTALL", Path.home() / ".hermes/hermes-agent"))


def native(code, **environment):
    sys.path.insert(0, str(INSTALL))
    try:
        from hermes_cli._launchers import runtime_command
        args = runtime_command(INSTALL, code=code)
    finally:
        sys.path.pop(0)
    env = dict(os.environ, HERMES_HOME=str(INSTALL.parent), **environment)
    env.pop("HERMES_KANBAN_DB", None)
    return subprocess.check_output(args, env=env, text=True, timeout=30).strip()

SERVICE = """#!/usr/bin/env python3
import fcntl, os, signal, sys
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
cmd = tuple(sys.argv[1:3])
gateway = cmd[0] == 'gateway'
path = home / ('gateway.lock' if gateway else 'cli-workers/dispatcher.lock')
with path.open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if cmd == ('gateway', 'status'):
            print('✓ Gateway is running')
            sys.exit(0)
        sys.exit(2)
    if cmd == ('gateway', 'status'):
        sys.exit(1)
    if cmd in [('gateway', 'run'), ('project-cli', 'serve')]:
        with (home / 'claims').open('a') as record:
            record.write(('telegram' if gateway else 'card') + '\\n')
        signal.pause()
"""


class ServiceCutoverTest(unittest.TestCase):
    def test_supervisor_start_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            home, state = base / "home", base / "state"
            repo = home / "hermes-agent"
            binary = repo / ".hermes/bin"
            binary.mkdir(parents=True)
            (home / "cli-workers").mkdir()
            runtime = state / "hermes-project-cli"
            runtime.mkdir(parents=True)
            script = base / "start-service.sh"
            shutil.copy(HERE / script.name, script)
            shutil.copy(HERE / "hermes_volume.py", base)
            (binary / "hermes").write_text(SERVICE)
            (binary / "docker").write_text("#!/bin/sh\necho fixture-volume\n")
            for command in binary.iterdir():
                command.chmod(0o755)
            (base / "setup-projects.sh").write_text("exit 0\n")
            (base / "verify-hermes.sh").write_text('test -f "$HERMES_HOME/pass"\n')
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(["git", "-C", repo, "-c", "user.name=test", "-c", "user.email=test@localhost",
                            "commit", "-qm", "fixture", "--allow-empty"], check=True)
            actual = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
            env = dict(os.environ, HERMES_HOME=str(home), XDG_STATE_HOME=str(state), HERMES_HOME_VOLUME="fixture-volume")

            def invoke(command):
                return subprocess.run(["bash", script, command], env=env, text=True, capture_output=True, timeout=45)

            revision = base / "hermes-revision"
            revision.write_text("0" * 40)
            marker = runtime / "configured"
            marker.write_text(revision.read_text())
            wrong = invoke("start")
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn(actual, wrong.stderr)
            self.assertIn("0" * 40, wrong.stderr)
            revision.write_text(actual)
            marker.write_text(actual)
            (base / ".env").write_text("HERMES_HOME_VOLUME=other-volume\n")
            self.assertIn("실제 마운트 fixture-volume", invoke("start").stderr)
            (base / ".env").write_text("HERMES_HOME_VOLUME=fixture-volume\n")
            hold = home / volume.HOLD
            hold.write_text("verify\n")
            proc = subprocess.Popen(["bash", script, "supervise"], env=env, start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for command in ("start", "status", "restart-gateway"):
                    result = invoke(command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("검증 대기", result.stdout)
                for args in (("gateway", "run"), ("project-cli", "serve")):
                    direct = subprocess.run([binary / "hermes", *args], env=env, timeout=3)
                    self.assertNotEqual(direct.returncode, 0)
                if (INSTALL / ".hermes/bin/hermes").is_file():
                    native("from pathlib import Path\n"
                           "from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership\n"
                           f"try: ProfileOwnership().reserve([Path({str(home)!r})])\n"
                           "except OwnershipConflict: pass\n"
                           "else: raise SystemExit('Gateway acquired the held profile')")
                self.assertNotEqual(invoke("resume").returncode, 0)
                self.assertTrue(hold.exists())
                (home / "pass").touch()
                hold.write_text("standby newer-release\n")
                self.assertNotEqual(invoke("resume").returncode, 0)
                self.assertFalse((home / "claims").exists())
                hold.write_text("verify\n")
                for command in ("resume", "start", "status"):
                    result = invoke(command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(hold.exists())
                self.assertCountEqual((home / "claims").read_text().splitlines(), ["card", "telegram"])
            finally:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)

    @unittest.skipUnless((INSTALL / ".hermes/bin/hermes").is_file(), "Installed Hermes runtime required")
    def test_rollback_checks_uncheckpointed_decisions_and_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = (Path(folder) / key for key in ("source", "target"))
            db = Path("kanban/boards/example/kanban.db")
            tg = Path("cli-workers/telegram.sqlite3")
            (source / "plugins").mkdir(parents=True)
            (source / "plugins/project-cli").symlink_to(HERE / "project-cli")
            (source / "config.yaml").write_text("plugins:\n  enabled: [project-cli]\n")
            card = native(f"import os\nos.environ['HERMES_HOME']={str(source)!r}\n" + """
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.workspace_plugin import extension
kb.init_db(board="default")
kb.create_board("example")
with connect_closing(board="example") as conn:
    card = kb.create_task(conn, title="Release fixture", assignee="cli:codex", board="example")
tg = extension("telegram")
tg.bind(tg.database(), "0", "1")
with tg.store(tg.database()) as conn:
    conn.execute("INSERT INTO requests(ref,route,kind,expires,board,card) "
                 "SELECT 'req',generation,'approval',9999999999,'example',? FROM binding", (card,))
print(card)
""", HERMES_KANBAN_HOME=str(source)).splitlines()[-1]
            shutil.copytree(source, target, symlinks=True)
            homes = {"source": source, "target": target}
            self.assertEqual(volume.fence("target", homes), [])
            self.assertEqual((source / volume.HOLD).read_text().strip(), "standby target")
            self.assertEqual((target / volume.HOLD).read_text().strip(), "verify")
            native("""
import os
os.environ["HERMES_HOME"] = os.environ["HERMES_KANBAN_HOME"]
from unittest.mock import patch
from hermes_cli import kanban_db_dispatch
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.workspace_plugin import extension
for slug in ("default", "example"):
    with connect_closing(board=slug) as conn:
        before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        with patch.object(kanban_db_dispatch, "_dispatch_once_locked", side_effect=AssertionError("dispatch")):
            result = kanban_db_dispatch.dispatch_once(conn, board=slug)
        assert result.skipped_locked and before == conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
worker = extension("worker")
with patch.object(worker, "try_lock", side_effect=AssertionError("worker entered queue")):
    try: worker.serve()
    except SystemExit as exc: assert "전환" in str(exc)
    else: raise AssertionError("direct worker start")
""", HERMES_KANBAN_HOME=str(target))
            (target / volume.HOLD).unlink()
            with sqlite3.connect(target / db) as board, sqlite3.connect(target / tg) as telegram:
                for conn in (board, telegram):
                    conn.execute("PRAGMA journal_mode=WAL")
                board.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) "
                              "VALUES (?,'cli:codex','done',1,2,'completed')", (card,))
                board.execute("UPDATE task_events SET payload=? WHERE task_id=?", ('{"fixture":true}', card))
                telegram.execute("DELETE FROM requests")
                board.commit()
                telegram.commit()
                with self.assertRaises(ValueError) as error:
                    volume.fence("source", homes)
                for key in ("task_runs", "task_events", "requests"):
                    self.assertIn(key, str(error.exception))
                self.assertFalse((target / volume.HOLD).exists())
                self.assertEqual((source / volume.HOLD).read_text().strip(), "standby target")
                self.assertEqual(volume.fence("source", {"source": source}), ["target"])
            board.close()
            telegram.close()

    def test_offline_docker_selection_persists_across_shells(self):
        controller = os.environ.get("HERMES_CLI_CONTROLLER", "agent-cli")
        if not shutil.which("docker") or subprocess.run(["docker", "inspect", "--type", "container", controller],
                                                       capture_output=True).returncode:
            self.skipTest("Running Hermes devcontainer and Docker daemon required")
        image = volume.docker("inspect", "--format", "{{.Image}}", controller)
        name = "hermes-cutover-" + uuid4().hex[:12]
        original, target = (name + suffix for suffix in ("-old", "-new"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / ".devcontainer"
            root.mkdir()
            for filename in ("hermes_volume.py", "docker-compose.yml"):
                shutil.copy(HERE / filename, root)
            envfile = root / ".env"
            envfile.write_text("KEEP_THIS=yes\n")
            env = os.environ.copy()
            for key in ("HERMES_SELECTED_HOME_VOLUME", "HERMES_HOME_VOLUME_EXTERNAL", "COMPOSE_ENV_FILES"):
                env.pop(key, None)
            env["HERMES_HOME_VOLUME"] = "old-shell-setting"

            def choose(which):
                return subprocess.run(["python3", root / "hermes_volume.py", "select", which, "--controller", name],
                                      env=env, text=True, capture_output=True, timeout=45)

            try:
                for v in (original, target):
                    volume.docker("volume", "create", v)
                volume.docker("create", "--name", name, "--pull=never", "-v", original + ":/home/ubuntu/.hermes",
                              "--entrypoint", "sleep", image, "90")
                volume.docker("start", name)
                self.assertNotEqual(choose(target).returncode, 0)
                self.assertEqual(envfile.read_text(), "KEEP_THIS=yes\n")
                volume.docker("stop", "-t", "1", name)
                for v in (target, original):
                    result = choose(v)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    config = subprocess.check_output(["docker", "compose", "-p", name, "-f", root / "docker-compose.yml",
                                                      "config", "--format", "json"], cwd=root.parent, env=env, text=True)
                    self.assertEqual(json.loads(config)["volumes"]["hermes_home"], {"name": v, "external": True})
                    self.assertIn("KEEP_THIS=yes", envfile.read_text())
                    if v == target:
                        # Recover from the old controller in a checkout with no saved selection.
                        envfile.write_text("KEEP_THIS=yes\n")
                        volume.docker("run", "-d", "--name", name + "-peer", "--pull=never",
                                      "-v", target + ":/home/ubuntu/.hermes", "--entrypoint", "sleep", image, "90")
                        denied = choose(original)
                        self.assertNotEqual(denied.returncode, 0)
                        self.assertIn(target, denied.stderr)
                        self.assertEqual(envfile.read_text(), "KEEP_THIS=yes\n")
                        volume.docker("stop", "-t", "1", name + "-peer")
            finally:
                subprocess.run(["docker", "rm", "-f", name, name + "-peer"], capture_output=True)
                subprocess.run(["docker", "volume", "rm", original, target], capture_output=True)
