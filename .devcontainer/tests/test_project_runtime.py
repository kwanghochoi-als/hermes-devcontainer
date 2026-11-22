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
    install = Path.home() / ".hermes/hermes-agent"
    sys.path.insert(0, str(install))
    try:
        from hermes_cli._launchers import runtime_command
    finally:
        sys.path.pop(0)
    env = dict(os.environ, HERMES_DASHBOARD_FILES_ROOT=str(workspace), HERMES_KANBAN_HOME=str(home),
               CODEX_HOME=str(home / "fixture-codex"), CLAUDE_CONFIG_DIR=str(home / "fixture-claude"))
    for key in ("HERMES_HOME", "HERMES_KANBAN_DB", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(key, None)
    code = (f"import os, runpy; os.environ['HERMES_HOME']={str(home)!r}; "
            f"runpy.run_path({str(Path(__file__).resolve())!r})[{function!r}]()")
    subprocess.run(runtime_command(install, code=code), cwd=workspace, env=env, check=True, timeout=100)


def check_middleware():
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from hermes_cli.plugins import _get_pre_tool_call_directive_details, discover_plugins
    from hermes_cli.workspace_projects import git_auth, knowledge
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    home = get_hermes_home()
    (home / "plugins").mkdir()
    (home / "plugins/project-cli").symlink_to(Path(__file__).resolve().parents[1] / "project-cli")
    (home / "config.yaml").write_text("plugins:\n  enabled: [project-cli]\n")
    (home / "qmd").mkdir()
    (home / "qmd/index.yml").write_text("collections: {hermes-workspace: {}, second: {}}\n")
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
    commits = {name: f"{i:040x}" for i, name in enumerate(git_auth.REPOS, start=1)}
    receipt.write_text(json.dumps(dict(session_id=session, repositories=commits)))
    t.assertEqual(knowledge.read_revisions(receipt, session), commits)
    with t.assertRaises(ValueError):
        knowledge.read_revisions(receipt, str(uuid4()))
    receipt.write_text(json.dumps(dict(session_id=session, repositories={"pharmmate": "a" * 40})))
    with t.assertRaises(ValueError):
        knowledge.read_revisions(receipt, session)
    row = dict(task_id="card", id=1, ended_at=1, profile="cli:codex")
    file = knowledge.write_note(home, row, dict(workspace=str(repo), source_commits=commits), "observer")
    note = (home / file).read_text()
    for name, sha in commits.items():
        t.assertIn(f"{name} · {sha}", note)


def check_container():
    from hermes_cli.workspace_projects import isolation
    from hermes_constants import get_hermes_home

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


class ProjectRuntime(unittest.TestCase):
    def test_knowledge_scope_and_pinned_sources(self):
        with tempfile.TemporaryDirectory() as h, tempfile.TemporaryDirectory() as w:
            in_runtime(Path(h), Path(w), "check_middleware")

    def test_docker_worker_with_real_clis(self):
        root = Path("/workspaces")
        if not shutil.which("docker") or not root.is_dir() or subprocess.run(
                ["docker", "inspect", "agent-cli"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            self.skipTest("Requires the running Hermes devcontainer and its Docker daemon")
        storage = Path.home() / ".hermes/cli-workers"
        storage.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=storage) as h, tempfile.TemporaryDirectory(dir=root, prefix=".worker-test-") as w:
            in_runtime(Path(h), Path(w), "check_container")
