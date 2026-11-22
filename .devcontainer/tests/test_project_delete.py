"""Exercise the same permanent deletion from the native CLI and the Webapp RPC."""

from contextlib import ExitStack, closing
from http.server import ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest

from test_cli_knowledge import Embeddings


class ProjectDeletion(unittest.TestCase):
    def test_delete_owned_data_and_keep_the_source(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as resources:
            root = Path(tmp)
            home, workspace = root / "home", root / "projects"
            workspace.mkdir()
            plugin = Path(__file__).resolve().parents[1] / "project-cli"
            (home / "plugins").mkdir(parents=True)
            (home / "plugins/project-cli").symlink_to(plugin)
            (home / "config.yaml").write_text("plugins:\n  enabled: [project-cli]\n")
            qmd = home / "qmd"
            qmd.mkdir()
            api = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Embeddings))
            api.recovered = threading.Event()
            api.recovered.set()
            threading.Thread(target=api.serve_forever, daemon=True).start()
            resources.callback(api.shutdown)
            (qmd / "index.yml").write_text(f'collections: {{}}\nmodels:\n  embed: "http://127.0.0.1:{api.server_port}/v1#fixture"\n')
            env = dict(os.environ, HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
                       HERMES_DASHBOARD_FILES_ROOT=str(workspace), QMD_CONFIG_DIR=str(qmd), XDG_CACHE_HOME=str(home))
            for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK_ID"):
                env.pop(key, None)

            def run(*args, cwd=workspace, check=True, input_text=""):
                return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True,
                                      input=input_text, check=check, timeout=90)

            def rpc(method, **params):
                with subprocess.Popen(["hermes", "--run-module", "tui_gateway.entry"], cwd=workspace, env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process:
                    try:
                        process.stdin.write(json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)) + "\n")
                        process.stdin.flush()
                        for line in process.stdout:
                            reply = json.loads(line)
                            if reply.get("id") == 1:
                                return reply
                        self.fail("No project RPC response")
                    finally:
                        process.terminate()
                        process.wait(timeout=15)

            def rows(file, query, *params):
                with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as db:
                    return db.execute(query, params).fetchall()

            name, peer = "remove-fixture", "keep-fixture"
            repo, sibling = workspace / name, workspace / peer
            for project in (peer, name):
                run("hermes", "project", "create", project, "--use")
            registration, state = home / "projects.db", home / "state.db"
            boards = home / "kanban/boards"
            pid = rows(registration, "SELECT id FROM projects WHERE slug=?", name)[0][0]
            peer_id = rows(registration, "SELECT id FROM projects WHERE slug=?", peer)[0][0]
            sid = rows(state, "SELECT id FROM sessions WHERE cwd=?", str(repo))[0][0]
            peer_sid = rows(state, "SELECT id FROM sessions WHERE cwd=?", str(sibling))[0][0]
            collections = []
            for path in (repo, sibling):
                collection = run("hermes", "project-cli", "knowledge", cwd=path).stdout.strip()
                collections.append(collection)
                notes = qmd / "knowledge" / collection
                (notes / "report.md").write_text("# " + path.name + "\n\nA unique finding.")
                (notes / "shared.md").write_text("# A finding in both projects")
                run("node", plugin / "index-knowledge.mjs", qmd, collection, input_text='["report.md","shared.md"]')
            removed_name, kept_name = collections
            index = qmd / "index.sqlite"
            content = rows(index, "SELECT hash FROM documents WHERE collection=? AND path='report.md'", removed_name)[0][0]
            common = rows(index, "SELECT hash FROM documents WHERE collection=? AND path='shared.md'", removed_name)[0][0]
            self.assertTrue(rows(index, "SELECT 1 FROM content_vectors WHERE hash=?", content))
            (repo / "local-work").write_text("in progress")
            tree = boards / name / "workspaces/linked"
            run("git", "worktree", "add", "-q", "--detach", tree, "HEAD", cwd=repo)
            (tree / "pending").write_text("uncommitted")
            head = run("git", "rev-parse", "HEAD", cwd=repo).stdout
            # A nested registered project owns its own conversations and QMD data.
            nested = repo / "subproject"
            run("git", "init", "-q", str(nested))
            run("hermes", "project", "create", "nested-project", "--primary", nested)
            nested_sid = rows(state, "SELECT id FROM sessions WHERE cwd=?", str(nested))[0][0]
            with closing(sqlite3.connect(state)) as db, db:
                db.execute("INSERT INTO sessions(id,source,started_at,cwd,git_repo_root,archived) VALUES(?,?,?,?,?,1)",
                           ("linked-session", "cli", time.time(), str(tree), str(repo)))
            transcript = home / "sessions/linked-session.jsonl"
            transcript.parent.mkdir(exist_ok=True)
            transcript.write_text("old transcript")

            def kanban(*args):
                return run("hermes", "kanban", "--board", name, *args).stdout

            pending = json.loads(kanban("create", "Pending decision", "--assignee", "cli:codex", "--json"))["id"]
            self.assertNotEqual(run("hermes", "project", "delete", name, check=False).returncode, 0)
            self.assertIn("error", rpc("projects.delete", id=pid))  # an older UI never confirmed data deletion
            kanban("claim", pending)
            busy = rpc("projects.delete", id=pid, delete_owned_data=True)
            self.assertIn(pending, busy["error"]["message"])
            self.assertEqual(rows(registration, "SELECT value FROM project_meta WHERE key='active_id'"), [(pid,)])
            self.assertFalse(json.loads((boards / name / "board.json").read_text())["archived"])
            kanban("block", pending, "--kind", "needs_input")
            with closing(sqlite3.connect(state)) as db, db:
                db.execute("INSERT INTO session_turn_leases VALUES(?,?,?,?)", (sid, "fixture", time.time(), time.time()+300))
            self.assertIn(sid, rpc("projects.delete", id=pid, delete_owned_data=True)["error"]["message"])
            self.assertTrue(transcript.is_file())
            self.assertTrue(rows(index, "SELECT 1 FROM content_vectors WHERE hash=?", content))
            with closing(sqlite3.connect(state)) as db, db:
                db.execute("DELETE FROM session_turn_leases WHERE conversation_id=?", (sid,))
            with closing(sqlite3.connect(registration)) as db, db:
                db.execute("UPDATE projects SET board_slug=? WHERE id=?", (name, peer_id))
            self.assertIn("다른 프로젝트", rpc("projects.delete", id=pid, delete_owned_data=True)["error"]["message"])
            self.assertTrue(transcript.is_file())
            with closing(sqlite3.connect(registration)) as db, db:
                db.execute("UPDATE projects SET board_slug=? WHERE id=?", (peer, peer_id))
            tg = home / "cli-workers/telegram.sqlite3"
            tg.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(tg)) as db, db:
                db.executescript("CREATE TABLE requests(board,ref); CREATE TABLE cursors(board); CREATE TABLE turn_reports(session);")
                db.executemany("INSERT INTO requests VALUES(?,?)", [(name, "old-ref"), (peer, "live-ref")])
                db.executemany("INSERT INTO cursors VALUES(?)", [(name,), (peer,)])
                db.executemany("INSERT INTO turn_reports VALUES(?)", [(sid,), (peer_sid,)])
                db.execute("CREATE TRIGGER simulate_interruption BEFORE DELETE ON requests "
                           "BEGIN SELECT RAISE(ABORT, 'fixture interruption'); END")
            worker_home = tg.parent / "homes" / hashlib.sha256(str(repo).encode()).hexdigest()
            for artifact in (worker_home / "codex/transcript", tg.parent / "boards" / name / "runs/card/log"):
                artifact.parent.mkdir(parents=True)
                artifact.touch()

            interrupted = rpc("projects.delete", id=pid, delete_owned_data=True)
            self.assertIn("fixture interruption", interrupted["error"]["message"])
            self.assertTrue(rows(registration, "SELECT 1 FROM projects WHERE id=?", pid))
            self.assertTrue(json.loads((boards / name / "board.json").read_text())["archived"])
            self.assertTrue(transcript.is_file())
            with closing(sqlite3.connect(tg)) as db, db:
                db.execute("DROP TRIGGER simulate_interruption")
            deleted = rpc("projects.delete", id=pid, delete_owned_data=True)
            self.assertIn("result", deleted, deleted)
            self.assertIsNone(deleted["result"]["active_id"])
            self.assertNotIn(pid, [p["id"] for p in deleted["result"]["projects"]])
            self.assertFalse(rows(registration, "SELECT 1 FROM project_folders WHERE project_id=?", pid))
            self.assertFalse(rows(registration, "SELECT 1 FROM project_meta WHERE key=?", "project-cli.session." + pid))
            self.assertNotIn(str(repo), [p.get("path") for p in rpc("projects.tree")["result"]["projects"]])
            self.assertFalse(rows(state, "SELECT id FROM sessions WHERE id IN (?,?)", sid, "linked-session"))
            self.assertFalse(rows(state, "SELECT 1 FROM messages WHERE session_id=?", sid))
            self.assertFalse(transcript.exists())
            self.assertEqual({r[0] for r in rows(state, "SELECT id FROM sessions WHERE id IN (?,?)", peer_sid, nested_sid)},
                             {peer_sid, nested_sid})
            self.assertFalse((boards / name / "kanban.db").exists())
            self.assertFalse((boards / name / "board.json").exists())
            self.assertNotEqual(run("hermes", "kanban", "--board", name, "list", check=False).returncode, 0)
            self.assertFalse((boards / name / "kanban.db").exists())
            self.assertTrue((boards / peer / "kanban.db").is_file())
            self.assertEqual(rows(tg, "SELECT ref FROM requests"), [("live-ref",)])
            self.assertEqual(rows(tg, "SELECT board FROM cursors"), [(peer,)])
            self.assertEqual(rows(tg, "SELECT session FROM turn_reports"), [(peer_sid,)])
            self.assertFalse(worker_home.exists())
            self.assertFalse((tg.parent / "boards" / name).exists())
            self.assertEqual((repo / "local-work").read_text(), "in progress")
            self.assertEqual((tree / "pending").read_text(), "uncommitted")
            self.assertEqual(run("git", "rev-parse", "HEAD", cwd=tree).stdout, head)
            self.assertFalse((qmd / "knowledge" / removed_name).exists())
            self.assertNotIn(removed_name, (qmd / "index.yml").read_text())
            self.assertFalse(rows(index, "SELECT 1 FROM documents WHERE collection=?", removed_name))
            self.assertFalse(rows(index, "SELECT 1 FROM content WHERE hash=?", content))
            self.assertFalse(rows(index, "SELECT 1 FROM content_vectors WHERE hash=?", content))
            self.assertTrue(rows(index, "SELECT 1 FROM content_vectors WHERE hash=?", common))
            self.assertEqual(rows(index, "SELECT count(*) FROM documents WHERE collection=?", kept_name), [(2,)])

            run("hermes", "project", "create", name)
            new_id, new_board = rows(registration, "SELECT id,board_slug FROM projects WHERE slug=?", name)[0]
            self.assertNotEqual(new_id, pid)
            self.assertEqual(new_board, name + "-2")
            self.assertEqual(rows(boards / new_board / "kanban.db", "SELECT count(*) FROM tasks"), [(0,)])
            new_sid = rows(state, "SELECT id FROM sessions WHERE cwd=?", str(repo))[0][0]
            self.assertNotEqual(new_sid, sid)
            self.assertFalse(list((qmd / "knowledge" / removed_name).iterdir()))
            self.assertNotEqual(run("hermes", "project", "delete", pid, "--yes", check=False).returncode, 0)
            run("hermes", "project", "remove-folder", name, repo)
            run("hermes", "project", "delete", name, "--yes")
            self.assertFalse((qmd / "knowledge" / removed_name).exists())
            self.assertFalse(rows(state, "SELECT id FROM sessions WHERE id=?", new_sid))
            self.assertTrue(rows(registration, "SELECT 1 FROM projects WHERE id=?", peer_id))


if __name__ == "__main__":
    unittest.main()
