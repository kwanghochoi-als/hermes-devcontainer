"""Exercise the installed Hermes middleware and Docker broker using disposable profiles."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4


def in_runtime(home, workspace, function):
    install = Path(os.environ.get("HERMES_TEST_INSTALL", Path.home() / ".hermes/hermes-agent"))
    (home / "plugins").mkdir()
    (home / "plugins/project-cli").symlink_to(Path(__file__).resolve().parents[1] / "project-cli")
    (home / "config.yaml").write_text("plugins:\n  enabled: [project-cli]\n")
    sys.path.insert(0, str(install))
    try:
        from hermes_cli._launchers import runtime_command
    finally:
        sys.path.pop(0)
    env = dict(os.environ, HERMES_DASHBOARD_FILES_ROOT=str(workspace), HERMES_KANBAN_HOME=str(home),
               CODEX_HOME=str(home / "fixture-codex"), CLAUDE_CONFIG_DIR=str(home / "fixture-claude"))
    env["HERMES_HOME"] = str(install.parent)
    for key in ("HERMES_KANBAN_DB", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)
    code = (f"import os, runpy; os.environ['HERMES_HOME']={str(home)!r}; "
            f"runpy.run_path({str(Path(__file__).resolve())!r})[{function!r}]()")
    subprocess.run(runtime_command(install, code=code), cwd=workspace, env=env, check=True, timeout=100)


def check_middleware():
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from hermes_cli.plugins import _get_pre_tool_call_directive_details, discover_plugins
    from hermes_cli.workspace_plugin import extension
    knowledge = extension("knowledge")
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    home = get_hermes_home()
    (home / "qmd").mkdir()
    (home / "qmd/index.yml").write_text(json.dumps({"collections": {
        key: {"path": str(home / "qmd" / key), "pattern": "*.md"} for key in ("hermes-workspace", "second")
    }}))
    repo = Path.cwd() / "project"
    repo.mkdir()
    session = str(uuid4())
    with SessionDB() as sessions:
        sessions.create_session(session, "cli", cwd=str(repo))
    discover_plugins(force=True)
    args = {"searches": [{"type": "lex", "query": "fixture"}]}
    def query(args, sid=""):
        return _get_pre_tool_call_directive_details("mcp__qmd__query", args, session_id=sid).modified_args
    t = unittest.TestCase()
    expected = dict(args, collections=[knowledge.collection_name(repo)])
    t.assertEqual(query(args, session), expected)
    token = set_session_cwd(str(repo))
    try:
        t.assertEqual(query(args), expected)
        t.assertIsNone(query(dict(args, collections=["second"])))
        t.assertEqual(query(dict(args, collections=["*"]))["collections"], ["hermes-workspace", "second"])
    finally:
        reset_session_cwd(token)
    t.assertEqual(query(args)["collections"], ["hermes-workspace"])
    receipt = home / "revisions.json"
    commits = {name: f"{i:040x}" for i, name in enumerate(("core", "service-2"), start=1)}
    receipt.write_text(json.dumps(dict(session_id=session, repositories=commits)))
    t.assertEqual(knowledge.read_revisions(receipt, session, commits), commits)
    with t.assertRaises(ValueError):
        knowledge.read_revisions(receipt, str(uuid4()), commits)
    receipt.write_text(json.dumps(dict(session_id=session, repositories={"foreign": "a" * 40})))
    with t.assertRaises(ValueError):
        knowledge.read_revisions(receipt, session, commits)
    row = dict(task_id="card", id=1, ended_at=1, profile="cli:codex")
    file = knowledge.write_note(home, row, dict(workspace=str(repo), source_commits=commits), "observer")
    note = (home / file).read_text()
    for name, sha in commits.items():
        t.assertIn(f"{name} · {sha}", note)

    from hermes_cli import projects_db as pdb, kanban_db as kb
    observer, worker = extension("observer"), extension("worker")
    with pdb.connect_closing() as db:
        from tui_gateway import server
        catalog = server._methods["i18n.catalog"](1, {"lang": "ko", "surface": "desktop"})
        t.assertIn("영구 삭제", catalog["result"]["messages"]["sidebar.projects.deleteOwnedConfirm"])
        reply = server._methods["projects.create_readonly"](1, {"name": "Research"})
        t.assertNotIn("error", reply)
        created = reply["result"]["project"]
        t.assertTrue(created["readonly"])
        owner = pdb.get_project(db, created["id"])
        t.assertTrue(owner.to_dict()["readonly"])
        repo = Path(owner.primary_path)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", "https://git.example.org/org/core.git"], check=True)
        t.assertEqual(observer.creation_sources(repo, [], None), {"Research": "https://git.example.org/org/core.git"})
        t.assertEqual(json.loads(owner.observer_sources), {"Research": str(repo)})
        t.assertEqual(json.loads(observer.refresh_sources(owner).observer_sources),
                      {"Research": "https://git.example.org/org/core.git"})
        subprocess.run(["git", "-C", repo, "remote", "remove", "origin"], check=True)
        with t.assertRaises(ValueError):
            observer.creation_sources(repo, [], ["bad=https://user@git.example.org/repo.git"])
        t.assertEqual(worker.card_workspace(str(repo), "worktree", owner.board_slug, "t_12345678", owner.id), repo)
        t.assertFalse((repo / ".worktrees").exists())
        token = set_session_cwd(str(repo))
        try:
            t.assertEqual(query(dict(args, collections=["second"]))["collections"], [knowledge.collection_name(repo)])
            t.assertEqual(_get_pre_tool_call_directive_details("terminal", {"command": "pwd"}).action, "block")
            t.assertNotEqual(_get_pre_tool_call_directive_details("project_cli_submit", {}).action, "block")
            t.assertEqual(_get_pre_tool_call_directive_details("kanban_list", {"board": "default"}).modified_args["board"], owner.board_slug)
        finally:
            reset_session_cwd(token)
        with worker.board(owner.board_slug) as cards:
            card = kb.create_task(cards, board=owner.board_slug, title="readonly fixture", assignee="default",
                                  workspace_kind="dir", workspace_path=str(repo))
            t.assertTrue(observer.requires_observer(cards, card, owner.board_slug))
            from unittest.mock import patch
            from hermes_cli import kanban_db_dispatch as dispatch
            with patch.object(dispatch, "_profile_exists_fn", return_value=lambda _name: True):
                result = dispatch.dispatch_once(cards, board=owner.board_slug, dry_run=True)
            t.assertIn(card, result.skipped_nonspawnable)
        for i in (1, 2):
            (repo / "README.md").write_text(f"committed {i}")
            subprocess.run(["git", "-C", repo, "add", "README.md"], check=True)
            subprocess.run(["git", "-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "-qm", str(i)], check=True)
            (repo / "README.md").write_text("developer edits")
            target = home / f"snapshots-{i}"
            target.mkdir()
            revision = observer.prepare(owner, target)["Research"]
            src = target / "Research.git"
            t.assertEqual(subprocess.check_output(["git", "--git-dir", src, "show", revision + ":README.md"], text=True), f"committed {i}")
            t.assertEqual((repo / "README.md").read_text(), "developer edits")
        owner.folders = []
        with t.assertRaises(observer.EnvironmentActionRequired):
            observer.prepare(owner, home / "missing")


def check_container():
    from hermes_cli.workspace_plugin import extension
    isolation = extension("isolation")
    from hermes_constants import get_hermes_home

    isolation.check_observer_mount()
    home = get_hermes_home()
    repo, linked, sibling = (Path.cwd() / s for s in ("source", "worktree", "sibling"))
    sibling.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                    "commit", "-qm", "initial", "--allow-empty"], check=True)
    subprocess.run(["git", "-C", repo, "worktree", "add", "--quiet", "--detach", linked], check=True)
    for checkout in (repo, linked):
        (checkout / "private-marker").touch()
    assets = home / "cli-workers"
    assets.mkdir(exist_ok=True)
    cached = Path.home() / ".hermes/cli-workers/toolchains"
    if cached.is_dir():
        (assets / "toolchains").symlink_to(cached)
    t = unittest.TestCase()
    for engine, cwd in (("codex", linked), ("claude", repo)):
        files = home / engine
        files.mkdir()
        attachment = files / "input attachment.txt"
        attachment.write_text(engine)
        target = isolation.IO + "/attachments/0/input attachment.txt"
        check = """set -eu
        for hidden in /var/run/docker.sock /run/docker-host.sock /home/ubuntu/.hermes/state.db "$1" "$2"; do
            test ! -e "$hidden"
        done
        test "$(git rev-parse --show-toplevel)" = "$PWD"
        test "$(id -u)" != 0
        test "$(cat "$4")" = "$3"
        test ! -w "$4"
        "$3" --version
        cat > worker-response
        """
        outside = repo if cwd == linked else linked
        # A linked worktree sees its backing Git metadata and its own files.
        with isolation.launch(engine, ["/bin/sh", "-c", check, "worker", str(sibling), str(outside / "private-marker"), engine, target],
                              cwd, repo, files, engine, None, None, False, {attachment: target}) as job:
            end = time.monotonic() + 40
            while time.monotonic() < end and job.poll() is None:
                time.sleep(.1)
            t.assertEqual(job.poll(), 0, (files / "stderr.log").read_text())
        t.assertEqual((cwd / "worker-response").read_text(), engine)
        t.assertTrue((files / "events.jsonl").stat().st_size)

    from types import SimpleNamespace
    owner = SimpleNamespace(id="p_fixture", folders=[SimpleNamespace(path=repo)],
                            observer_sources=json.dumps({"Data_API": str(repo)}))
    from hermes_cli.workspace_plugin import extension
    capture = extension("observer_runs").capture
    snapshot = capture(home / "fixture-runs", owner, "t_fixture", "1")
    for engine in ("codex", "claude"):
        files = home / f"readonly-{engine}"
        files.mkdir()
        check = """set -eu
        test "$PWD" = /opt/hermes-observer
        test -r .observer/gate.py
        test ! -w .observer/gate.py
        test ! -e "$1"
        test ! -e /home/ubuntu/.hermes/git-auth
        test ! -w /run/hermes-job/snapshots/Data_API.git/config
        printf '%s' '{"session_id":"fixture","hook_event_name":"UserPromptSubmit"}' | python3 -I -B .observer/gate.py
        git --git-dir=/run/hermes-job/snapshots/Data_API.git ls-tree -r --name-only FETCH_HEAD > /tmp/files
        ! grep -qx private-marker /tmp/files
        """
        with isolation.launch(engine, ["/bin/sh", "-c", check, "worker", str(repo)], repo, repo, files,
                              "", None, None, snapshot, {}) as job:
            end = time.monotonic() + 30
            while job.poll() is None and time.monotonic() < end:
                time.sleep(.1)
            t.assertEqual(job.poll(), 0, (files / "stderr.log").read_text())
        receipt = json.loads((files / "revisions.json").read_text())
        t.assertEqual(receipt["repositories"], json.loads((files / "snapshot.json").read_text()))
        t.assertEqual(set(receipt["repositories"]), {"Data_API"})


def check_clone_discovery():
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Barrier
    from types import SimpleNamespace
    from unittest.mock import patch
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from hermes_cli import kanban_db as kb, projects_db as pdb
    from hermes_cli.workspace_plugin import extension
    board, git_auth, isolation, observer, worker = (extension(c) for c in ("board", "git_auth", "isolation", "observer", "worker"))
    slash, submit_tool = extension().slash, extension().submit_tool
    from hermes_constants import get_hermes_home

    t = unittest.TestCase()
    home = get_hermes_home()
    state = home / "cli-workers"
    for d in ("locks", "sessions"):
        (state / d).mkdir(parents=True)
    (home / "qmd").mkdir()
    (home / "qmd/index.yml").write_text("collections: {}\n")
    upstream = Path.cwd() / "upstream"
    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
                                        *map(str, args)], text=True).strip()
    git("init", "-q", "-b", "main", upstream)
    (upstream / "version.txt").write_text("checkout version")
    git("-C", upstream, "add", "version.txt")
    git("-C", upstream, "commit", "-qm", "initial")
    old_sha = git("-C", upstream, "rev-parse", "HEAD")
    def clone(path, origin=None):
        git("clone", "-q", upstream, path)
        if origin:
            git("-C", path, "remote", "set-url", "origin", origin)
        return path

    urls = {k: f"https://git.example.org/org/{k}.git" for k in ("core", "api", "later", "headless")}
    urls["guest"] = "https://another.example.org/group/api.git"
    preset = ["core=" + urls["core"]]
    with pdb.connect_closing() as db:
        pid = pdb.create_project(db, name="Review", readonly=True, sources=preset)
        project = pdb.get_project(db, pid)
        root = Path(project.primary_path)
        extra = Path.cwd() / "extra"
        pdb.add_folder(db, pid, str(extra))
        other = pdb.create_project(db, name="Other")
        pdb.add_folder(db, other, str(root / "foreign"))
        def stored():
            return json.loads(pdb.get_project(db, pid).observer_sources)
        slug, first = board.enqueue("codex", "inspect", root)
        local = clone(root / "services/local")
        ssh = clone(extra / "ssh-docs", "git@git.example.org:docs/handbook.git")
        git("-C", ssh, "remote", "rename", "origin", "upstream")
        (local / "untracked").write_text("private working copy")
        api = clone(root / "services/api", urls["api"])
        clone(root / "tools/api", urls["guest"])
        clone(root / "copies/ssh-api", "git@git.example.org:org/api")
        clone(root / "copies/extra-core", "https://GIT.example.org:443/org/core/")
        # A clone can have an unborn HEAD when the server's default branch is missing.
        # It still has an authoritative remote main and must enter the build.
        headless = root / "headless"
        git("init", "-q", "-b", "unborn", headless)
        git("-C", headless, "remote", "add", "origin", urls["headless"])
        clone(root / "z-copies/local", upstream.as_uri())
        clone(api / "module", "https://git.example.org/org/module.git")
        git("-C", api, "submodule", "add", upstream, "module")
        clone(api / "module/nested", "https://git.example.org/org/module-child.git")
        for i, parent in enumerate((".worktrees", "node_modules", "foreign")):
            clone(root / parent / "ignored", f"https://git.example.org/org/ignored{i}.git")
        git("-C", api, "worktree", "add", "-q", "--detach", root / "linked")
        clone(root / "linked/ignored", "https://git.example.org/org/worktree-child.git")
        (root / "link").symlink_to(root / "foreign", target_is_directory=True)
        t.assertEqual(stored(), {"core": urls["core"]})
        (upstream / "version.txt").write_text("first build")
        git("-C", upstream, "commit", "-qam", "advance remote")
        main_sha = git("-C", upstream, "rev-parse", "HEAD")
        expected = {"core": urls["core"], "api": urls["api"], "api-2": urls["guest"],
                    "local": str(local), "ssh-docs": str(ssh), "headless": urls["headless"]}
        # Route HTTPS fetches only to the fixture. Exercise the real fetch, receipt and Kanban paths.
        git_env = git_auth.git_env
        def offline_env(sources, token):
            env = git_env(sources, token)
            i = int(env["GIT_CONFIG_COUNT"])
            for url in (*urls.values(), "git@git.example.org:docs/handbook.git"):
                env[f"GIT_CONFIG_KEY_{i}"] = f"url.{upstream.as_uri()}.insteadOf"
                env[f"GIT_CONFIG_VALUE_{i}"] = url
                i += 1
            env.update(GIT_CONFIG_COUNT=str(i), GIT_ALLOW_PROTOCOL="file")
            return env
        @contextmanager
        def fixture_cli(engine, cli, cwd, project_path, files, prompt, lock, session, staging, attachments):
            revisions = json.loads((files / "snapshot.json").read_text())
            if not (root / "later").exists():
                clone(root / "later", urls["later"])
            t.assertEqual(git("--git-dir", staging / "snapshots/local.git", "rev-parse", "refs/heads/snapshot"), revisions["local"])
            t.assertNotIn("untracked", git("--git-dir", staging / "snapshots/local.git", "ls-tree", "-r", revisions["local"]))
            expected_text = "first build" if revisions["local"] == main_sha else "second build"
            t.assertEqual(git("--git-dir", staging / "snapshots/local.git", "show", revisions["local"] + ":version.txt"), expected_text)
            sid = str(uuid4())
            report = {"status": "completed", "summary": "fixture", "tests": "snapshot verified"}
            events = ([{"type": "thread.started", "thread_id": sid},
                       {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}},
                       {"type": "turn.completed"}] if engine == "codex" else
                      [{"type": "system", "subtype": "init", "session_id": sid},
                       {"type": "result", "subtype": "success", "structured_output": report}])
            (files / "revisions.json").write_text(json.dumps({"session_id": sid, "repositories": revisions}))
            (files / "events.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
            yield SimpleNamespace(poll=lambda: 0)

        def run_card(card, blocked=False):
            with (state / "locks" / worker.digest(str(root))).open("a") as lock:
                worker.process_card(card, os.dup(lock.fileno()), slug)
            with board.board(slug) as cards:
                task = kb.get_task(cards, card)
                blocks = [e.payload for e in kb.list_events(cards, card) if e.kind == "blocked"]
                t.assertEqual(task.status, "blocked" if blocked else "done", blocks)
                if blocked:
                    t.assertEqual(blocks[-1]["request_kind"], "environment")
                    return blocks[-1]["reason"]
                return kb.latest_run(cards, card).metadata

        from hermes_cli.kanban_db_graph import decompose_triage_task
        with patch.object(git_auth, "git_env", offline_env), patch.object(isolation, "_launch", fixture_cli):
            initial = run_card(first)
            t.assertEqual(initial["source_commits"], dict.fromkeys(expected, main_sha))
            t.assertEqual(initial["local_sources"], [])
            t.assertEqual(git("-C", local, "rev-parse", "HEAD"), old_sha)
            t.assertTrue((local / "untracked").exists())
            _, paused_child = board.enqueue("claude", "await input after pinning", root, parents=[first])
            with patch.object(isolation, "_launch", side_effect=observer.EnvironmentActionRequired("fixture unavailable")):
                t.assertIn("fixture unavailable", run_card(paused_child, blocked=True))
            (upstream / "version.txt").write_text("second build")
            git("-C", upstream, "commit", "-qam", "next build")
            advanced = git("-C", upstream, "rev-parse", "HEAD")
            no_fetch = AssertionError("a descendant tried to refresh sources")
            with patch.object(observer, "refresh_sources", side_effect=no_fetch), patch.object(observer, "prepare", side_effect=no_fetch):
                cwd_token = set_session_cwd(str(root))
                try:
                    reply = slash("claude", f"--parent {first} first child", "", "cli")
                    t.assertIn("접수", reply)
                    child = reply.split()[2]
                    sibling = json.loads(submit_tool({"agent": "codex", "prompt": "second child",
                                                      "parents": [first]}))["task_id"]
                finally:
                    reset_session_cwd(cwd_token)
                for card in (child, sibling):
                    meta = run_card(card)
                    t.assertEqual(meta["source_snapshot"], initial["source_snapshot"])
                    t.assertEqual(meta["source_commits"], initial["source_commits"])
                _, join = board.enqueue("codex", "join", root, parents=[child, sibling])
                t.assertEqual(run_card(join)["source_snapshot"], initial["source_snapshot"])
                t.assertEqual(stored(), expected)
            with board.board(slug) as cards:
                second = kb.create_task(cards, board=slug, title="new build", assignee="cli:claude", workspace_kind="worktree")
            next_build = run_card(second)
            expected["later"] = urls["later"]
            t.assertEqual(next_build["source_commits"], dict.fromkeys(expected, advanced))
            _, conflict = board.enqueue("claude", "incompatible builds", root, parents=[first, second])
            t.assertIn("서로 다릅니다", run_card(conflict, blocked=True))

            with board.board(slug) as cards:
                group = kb.create_task(cards, board=slug, title="fan out", triage=True,
                                       assignee="cli:codex", workspace_kind="dir", workspace_path=str(root))
                members = decompose_triage_task(cards, group, root_assignee="cli:claude",
                                                children=[{"title": "part A", "assignee": "cli:codex"},
                                                          {"title": "part B", "assignee": "cli:claude", "parents": [0]}])
            # The split itself pinned main. Move the *only* upstream offline before a child starts.
            offline = upstream.rename(upstream.with_name("offline"))
            try:
                _, down = board.enqueue("claude", "fetch required", root)
                t.assertIn("스냅샷 준비 실패", run_card(down, blocked=True))
                with board.board(slug) as cards:
                    paused = kb.create_task(cards, board=slug, title="offline fan-out", triage=True,
                                            assignee="cli:codex", workspace_kind="dir", workspace_path=str(root))
                    with t.assertRaisesRegex(ValueError, "환경 조치"):
                        decompose_triage_task(cards, paused, root_assignee="cli:codex", children=[{"title": "unpublished"}])
                    t.assertEqual(kb.get_task(cards, paused).status, "blocked")
                    t.assertFalse(kb.parent_ids(cards, paused))
                    t.assertEqual(kb.list_events(cards, paused)[-1].payload["request_kind"], "environment")
                    t.assertTrue(kb.unblock_task(cards, paused))
                    t.assertEqual(kb.get_task(cards, paused).status, "triage")
                with patch.object(observer, "prepare", side_effect=no_fetch):
                    for card in [*members, group]:
                        meta = run_card(card)
                        t.assertEqual(meta["source_snapshot"]["card"], group)
                        t.assertEqual(meta["source_commits"], next_build["source_commits"])
            finally:
                offline.rename(upstream)
            # Planning a dependent fan-out does not start its build before the external root.
            _, queued = board.enqueue("claude", "queued build", root)
            with board.board(slug) as cards, patch.object(observer, "prepare", side_effect=no_fetch):
                delayed = kb.create_task(cards, board=slug, title="plan early", triage=True, parents=[queued],
                                         assignee="cli:claude", workspace_kind="dir", workspace_path=str(root))
                part, = decompose_triage_task(cards, delayed, root_assignee="cli:codex",
                                               children=[{"title": "await build", "assignee": "cli:claude"}])
                t.assertEqual(kb.get_task(cards, part).status, "todo")
                t.assertIn(queued, kb.parent_ids(cards, part))
            upstream_pin = run_card(queued)["source_snapshot"]
            with patch.object(observer, "prepare", side_effect=no_fetch):
                for card in (part, delayed):
                    t.assertEqual(run_card(card)["source_snapshot"], upstream_pin)

            # No child fetches objects even when the parent has only a SHA receipt.
            ref = initial["source_snapshot"]
            folder = state / "boards" / slug / "runs" / first / ref["generation"]
            (folder / "sources").rename(home / "saved-first-build")
            _, missing = board.enqueue("claude", "missing bundle", root, parents=[first])
            with patch.object(observer, "prepare", side_effect=no_fetch):
                t.assertIn("스냅샷", run_card(missing, blocked=True))
            (folder / "snapshot-ref.json").unlink()
            with board.board(slug) as cards, kb.write_txn(cards):
                cards.execute("UPDATE task_runs SET metadata=json_remove(metadata,'$.source_snapshot') WHERE task_id=?", (first,))
            _, legacy = board.enqueue("claude", "historical parent", root, parents=[first])
            with patch.object(observer, "prepare", side_effect=no_fetch):
                t.assertIn("스냅샷", run_card(legacy, blocked=True))
            (home / "saved-first-build").rename(folder / "sources")
            from plugins.kanban.dashboard.plugin_api import _drag_to
            with board.board(slug) as cards:
                t.assertTrue(_drag_to(cards, first, "ready"))
            rebuilt = run_card(first)
            t.assertEqual(rebuilt["source_commits"], next_build["source_commits"])
            with board.board(slug) as cards:
                t.assertTrue(kb.unblock_task(cards, paused_child))
            with patch.object(observer, "prepare", side_effect=no_fetch):
                # Kanban invalidated the completed child when the operator reopened
                # the parent. The already blocked attempt is an ordinary resumption.
                t.assertEqual(run_card(child)["source_snapshot"], rebuilt["source_snapshot"])
                t.assertEqual(run_card(paused_child)["source_commits"], initial["source_commits"])
        t.assertEqual(pdb.create_project(db, name="Review", readonly=True, sources=preset), pid)
        t.assertEqual(stored(), expected)
        pdb.add_folder(db, other, str(local))
        with t.assertRaisesRegex(observer.EnvironmentActionRequired, "local"):
            with tempfile.TemporaryDirectory() as tmp, patch.object(git_auth, "git_env", offline_env):
                observer.prepare(pdb.get_project(db, pid), Path(tmp))

        explicit_clone = clone(Path.cwd() / "explicit-clone")
        explicit_pid = pdb.create_project(db, name="Explicit", primary_path=str(explicit_clone), readonly=True)
        git("-C", explicit_clone, "remote", "remove", "origin")
        with tempfile.TemporaryDirectory() as tmp, t.assertRaisesRegex(observer.EnvironmentActionRequired, "원격을 복구"):
            observer.prepare(observer.refresh_sources(pdb.get_project(db, explicit_pid)), Path(tmp))

        # Separate card processes can finish scans with different views of the checkout tree.
        new_urls = {name: f"https://git.example.org/org/{name}.git" for name in ("left", "right")}
        scans = iter([{clone(root / name, url): url} for name, url in new_urls.items()])
        ready = Barrier(2)
        def concurrent_scan(project, owners):
            found = next(scans)
            ready.wait(timeout=5)
            return found
        with patch.object(observer, "find_clones", concurrent_scan), ThreadPoolExecutor(2) as pool:
            list(pool.map(observer.refresh_sources, [project, project]))
        t.assertEqual(stored(), {**expected, **new_urls})


class ProjectRuntime(unittest.TestCase):
    def test_knowledge_scope_and_pinned_sources(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as w:
            in_runtime(Path(h), Path(w), "check_middleware")

    def test_clones_join_at_card_start(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as w:
            in_runtime(Path(h), Path(w), "check_clone_discovery")

    def test_docker_worker_with_real_clis(self):
        root = Path("/workspaces")
        controller = os.environ.get("HERMES_CLI_CONTROLLER", "agent-cli")
        if not shutil.which("docker") or not root.is_dir() or subprocess.run(
                ["docker", "inspect", controller], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            self.skipTest("Requires the running Hermes devcontainer and its Docker daemon")
        storage = Path.home() / ".hermes/cli-workers"
        storage.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=storage) as h, tempfile.TemporaryDirectory(dir=root, prefix=".worker-test-") as w:
            in_runtime(Path(h), Path(w), "check_container")
