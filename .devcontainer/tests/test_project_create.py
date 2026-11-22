"""A name-only Hermes project is usable from its session, Kanban and QMD."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import fixture_transport

from test_cli_knowledge import Embeddings
from test_cli_workers import FAKE_CLI


class ProjectCreation(unittest.TestCase):
    def test_workspace_git_preserves_new_project_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

            def git(*args, cwd=root):
                return subprocess.check_output(["git", *args], cwd=cwd, env=env, text=True).strip()

            git("init", "-q")
            (root / ".gitignore").write_bytes((Path(__file__).resolve().parents[2] / ".gitignore").read_bytes())
            for name in (".devcontainer", ".github"):
                (root / name).mkdir()
                (root / name / "config").touch()
            project = root / "new-project"
            git("init", "-q", str(project))
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                "commit", "-qm", "Initial commit", "--allow-empty", cwd=project)
            head = git("rev-parse", "HEAD", cwd=project)
            draft = root / "등록 전 프로젝트" / "draft.txt"
            draft.parent.mkdir()
            draft.write_text("in progress")
            (project / "local.txt").write_text("local work")

            git("add", "-A")
            self.assertEqual(git("ls-files").splitlines(), [".devcontainer/config", ".github/config", ".gitignore"])
            self.assertEqual(git("rev-parse", "--show-toplevel"), str(root.resolve()))
            self.assertEqual(git("clean", "-nd"), "")
            git("clean", "-fd")
            self.assertEqual(git("rev-parse", "HEAD", cwd=project), head)
            self.assertEqual((project / "local.txt").read_text(), "local work")
            self.assertEqual(draft.read_text(), "in progress")

    def test_native_create_and_worker_boards(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as resources:
            root = Path(tmp)
            home, projects = root / "home", root / "projects"
            projects.mkdir()
            subprocess.run(["git", "init", "-q", projects], check=True)
            (projects / ".devcontainer").mkdir()
            (projects / ".devcontainer/projects.json").write_text("{}")
            (home / "plugins").mkdir(parents=True)
            fixture_transport.install(home)
            (home / "config.yaml").write_text("plugins:\n  enabled: [project-cli]\n")
            (home / "qmd").mkdir()
            api = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Embeddings))
            api.recovered = threading.Event()
            api.recovered.set()
            threading.Thread(target=api.serve_forever, daemon=True).start()
            resources.callback(api.shutdown)
            config = home / "qmd/index.yml"
            config.write_text(f'collections: {{}}\nmodels:\n  embed: "http://127.0.0.1:{api.server_port}/v1#fixture"\n')
            bins = root / "bin"
            bins.mkdir()
            for engine in ("codex", "claude"):
                (bins / engine).write_text(FAKE_CLI)
                (bins / engine).chmod(0o755)
            gate = root / "allow-first-card"
            env = dict(os.environ, HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), PROBE_GATE=str(gate),
                       HERMES_CLI_CONCURRENCY="2", HERMES_CLI_PROJECT_CONCURRENCY="1", HERMES_CLI_MAX_RUNTIME_SECONDS="86400",
                       HERMES_DASHBOARD_FILES_ROOT=str(projects), PATH=f"{bins}:{os.environ['PATH']}")
            env.pop("HERMES_KANBAN_DB", None)

            def run(*args, cwd=projects):
                return subprocess.check_output(args, env=env, cwd=cwd, text=True).strip()

            def rows(db, query, *params):
                with closing(sqlite3.connect(db)) as conn:
                    return conn.execute(query, params).fetchall()

            def wait_card(slug, card, status):
                db = home / "kanban/boards" / slug / "kanban.db"
                end = time.monotonic() + 40
                while time.monotonic() < end:
                    if rows(db, "SELECT status FROM tasks WHERE id=?", card) == [(status,)]:
                        return
                    time.sleep(0.2)
                self.fail(str(rows(db, "SELECT status,block_kind FROM tasks WHERE id=?", card)))

            run("hermes", "kanban", "boards", "create", "cli-workers")
            process = subprocess.Popen(["hermes", "project-cli", "serve"], env=env)
            try:
                repo = projects / "test-prj"
                # A retry can enter through the webapp's RPC and the CLI at the same time.
                rpc_input = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "projects.create",
                                        "params": {"name": "test-prj"}}) + "\n"
                def rpc_create():
                    proc = subprocess.Popen(["hermes", "--run-module", "tui_gateway.entry"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True)
                    try:
                        proc.stdin.write(rpc_input)
                        proc.stdin.flush()
                        for line in proc.stdout:
                            result = json.loads(line)
                            if result.get("id") == 1:
                                return result["result"]["project"]
                        self.fail("Project RPC exited without a result")
                    finally:
                        proc.terminate()
                        proc.wait(timeout=15)
                        proc.stdin.close()
                        proc.stdout.close()
                with ThreadPoolExecutor() as pool:
                    rpc = pool.submit(rpc_create)
                    run("hermes", "project", "create", "test-prj")
                    project = rpc.result(timeout=45)
                self.assertEqual((project["primary_path"], project["board_slug"]), (str(repo), "test-prj"))
                self.assertEqual(run("git", "rev-parse", "--show-toplevel", cwd=repo), str(repo))
                self.assertEqual(run("git", "rev-list", "--count", "HEAD", cwd=repo), "1")
                self.assertEqual(run("git", "ls-tree", "--name-only", "HEAD", cwd=repo), "")
                sessions = rows(home / "state.db", "SELECT title,message_count FROM sessions WHERE cwd=?", str(repo))
                self.assertEqual(sessions, [("test-prj-세션", 1)])
                self.assertEqual(rows(home / "projects.db", "SELECT count(*) FROM projects"), [(1,)])
                board = home / "kanban/boards/test-prj"
                self.assertEqual(json.loads((board / "board.json").read_text())["project_id"], project["id"])
                collection = run("hermes", "project-cli", "knowledge", cwd=repo)
                self.assertEqual(run("hermes", "project-cli", "board", cwd=repo), "test-prj")
                self.assertIn(collection, config.read_text())
                self.assertEqual(list((home / "qmd/knowledge" / collection).iterdir()), [])

                fixture = {"knowledge": [{"fact": "retry token is kept", "evidence": "rules.py verify_retry()"}]}
                (repo / ".probe-result.json").write_text(json.dumps(fixture))
                run("git", "add", ".probe-result.json", cwd=repo)
                run("git", "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                    "commit", "-qm", "test fixture", cwd=repo)
                first = run("hermes", "project-cli", "submit", "codex", "check the contract", cwd=repo)
                second = json.loads(run("hermes", "kanban", "--board", "test-prj", "create",
                    "review in a worktree", "--assignee", "cli:claude", "--json"))["id"]
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    receipts = rows(board / "kanban.db",
                        "SELECT task_id,payload FROM task_events WHERE kind='knowledge_indexed'")
                    if {r[0] for r in receipts} == {first, second}:
                        break
                    time.sleep(0.2)
                else:
                    self.fail(str(rows(board / "kanban.db", "SELECT id,status,last_failure_error FROM tasks")))
                self.assertTrue(all(json.loads(r[1])["document"].startswith(f"qmd://{collection}/") for r in receipts))
                expected = [(first, "done", project["id"], "dir"), (second, "done", project["id"], "worktree")]
                self.assertEqual(rows(board / "kanban.db", "SELECT id,status,project_id,workspace_kind FROM tasks ORDER BY rowid"), expected)
                self.assertEqual(rows(home / "kanban/boards/cli-workers/kanban.db", "SELECT count(*) FROM tasks"), [(0,)])
                sibling = projects / "linked-checkout"
                run("git", "worktree", "add", "--quiet", "--detach", str(sibling), "HEAD", cwd=repo)
                self.assertEqual(run("hermes", "project-cli", "board", cwd=sibling), "test-prj")
                self.assertEqual(run("hermes", "project-cli", "knowledge", cwd=sibling), collection)
                self.assertEqual(run("hermes", "project-cli", "search-scope", cwd=sibling), collection)
                self.assertEqual(rows(board / "kanban.db", "SELECT max_runtime_seconds FROM tasks WHERE id=?", first), [(86400,)])
                self.assertEqual(run("git", "check-ignore", "test-prj"), "test-prj")

                other = projects / "other"
                run("git", "init", "-q", "-b", "existing", str(other))
                (other / "staged.txt").write_text("existing work")
                run("git", "add", "staged.txt", cwd=other)
                # A folderless project from an earlier Hermes install keeps its identity.
                with closing(sqlite3.connect(home / "projects.db")) as conn, conn:
                    conn.execute("""INSERT INTO projects (id,slug,name,created_at,board_slug)
                                    VALUES ('p_01234567','other','다른 프로젝트',1,'cli-workers')""")
                run("hermes", "project", "create", "다른 프로젝트", str(other), "--slug", "other")
                self.assertEqual(rows(home / "projects.db", "SELECT primary_path,board_slug FROM projects WHERE id='p_01234567'"),
                                 [(str(other), "other")])
                self.assertEqual(run("git", "diff", "--cached", "--name-only", cwd=other), "staged.txt")
                self.assertEqual(run("git", "branch", "--show-current", cwd=other), "existing")
                other_collection = run("hermes", "project-cli", "knowledge", cwd=other)
                self.assertNotEqual(other_collection, collection)
                self.assertCountEqual(run("hermes", "project-cli", "search-scope", "--all-projects", cwd=sibling).splitlines(),
                                      [collection, other_collection])
                wrong = json.loads(run("hermes", "kanban", "--board", "test-prj", "create", "wrong board",
                                       "--workspace", f"dir:{other}", "--assignee", "cli:claude", "--json"))["id"]
                wait_card("test-prj", wrong, "blocked")
                self.assertEqual(rows(board / "kanban.db", "SELECT block_kind FROM tasks WHERE id=?", wrong), [("capability",)])
                self.assertFalse((other / ".probe-args.json").exists())
                held = run("hermes", "project-cli", "submit", "codex", "WAIT_FOR_RESTART", cwd=repo)
                wait_card("test-prj", held, "running")
                queued = run("hermes", "project-cli", "submit", "claude", "parallel worktree", cwd=sibling)
                independent = run("hermes", "project-cli", "submit", "claude", "independent project", cwd=other)
                wait_card("other", independent, "done")
                wait_card("test-prj", queued, "ready")
                gate.touch()
                wait_card("test-prj", queued, "done")

                run("hermes", "project-cli", "restore", "pharmmate")
                policy = projects / "pharmmate/.observer"
                self.assertTrue((policy / "start.py").is_symlink())
                self.assertIn("pharmmate", json.loads((policy / "sources.json").read_text()))
                self.assertFalse((projects / "pharmmate/pharmmate").exists())
                # Route both inventory origins to a fixture without changing their stored URLs.
                mirrors = root / "fixture-mirrors"
                mirrors.mkdir()
                for name in ("pharmmate", "pharmmate-als"):
                    run("git", "clone", "--quiet", "--bare", str(repo), str(mirrors / f"{name}.git"))
                with patch.dict(env, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{mirrors.as_uri()}/.insteadOf",
                                GIT_CONFIG_VALUE_0="https://github.com/alssolution/"):
                    run("hermes", "project-cli", "restore", "pharmmate", "--clone")
                    clone = projects / "pharmmate/pharmmate"
                    keep = clone / "local-edit"
                    keep.write_text("wip")
                    run("hermes", "project-cli", "restore", "pharmmate", "--clone")
                    self.assertEqual(keep.read_text(), "wip")
                    run("git", "remote", "set-url", "origin", "local-only", cwd=clone)
                    refused = subprocess.run(["hermes", "project-cli", "restore", "pharmmate", "--clone"],
                                             cwd=projects, env=env, capture_output=True)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertEqual(run("git", "remote", "get-url", "origin", cwd=clone), "local-only")
                self.assertEqual(rows(home / "state.db", "SELECT count(*) FROM sessions WHERE title='pharmmate-세션'"), [(1,)])
                attempt = subprocess.run(["hermes", "project", "create", "../outside"], env=env, cwd=projects,
                                         capture_output=True)
                self.assertNotEqual(attempt.returncode, 0)
                self.assertFalse((root / "outside").exists())
                # Recover a partially initialized filesystem without another commit or session.
                paused = config.with_suffix(".paused")
                config.rename(paused)
                interrupted = subprocess.run(["hermes", "project", "create", "recover"], env=env, cwd=projects,
                                             capture_output=True, text=True)
                self.assertNotEqual(interrupted.returncode, 0)
                self.assertEqual(rows(home / "projects.db", "SELECT id FROM projects WHERE slug='recover'"), [])
                paused.rename(config)
                run("hermes", "project", "create", "recover")
                self.assertEqual(run("git", "rev-list", "--count", "HEAD", cwd=projects / "recover"), "1")
                self.assertEqual(rows(home / "state.db", "SELECT count(*) FROM sessions WHERE title='recover-세션'"), [(1,)])
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    unittest.main()
