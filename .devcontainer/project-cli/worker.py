"""Execute headless CLIs. Hermes owns the cards, claims and attempt history."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from uuid import UUID

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_context as context_sections
from hermes_cli import kanban_db_workspace as workspaces
from hermes_cli.kanban_db_dispatch import adopt_worker_pid, heartbeat_worker
from hermes_constants import get_hermes_home
from tools.environments.local import hermes_subprocess_env

from .board import BOARD, LANES, board, knowledge_root, project_dir, worker_boards
from . import knowledge

SCHEMA = Path(__file__).with_name("result.schema.json")
REPORT = """Execute the user's Kanban task within the project's rules. Carry out routine,
reversible work in scope, including inspection, local edits, builds, tests and troubleshooting,
without asking for permission. Make ordinary implementation choices yourself.
Before taking an action that needs an operator decision, stop and report blocked. Ask only
for a material choice you cannot resolve from the task (such as a change of product scope),
or for authorization missing for the specific action and target (such as a production deploy,
external publication or message, irreversible data loss, new spending or access changes).
State the question or proposed action, target and effect in the summary. Honor explicit
authorization already given for this card. On resume, match the operator's answer in this
card's comments to the original proposal and carry out only the scope they approved.
Check prior attempts before repeating an action with external effects.
Try safe recovery from technical errors. If a technical obstacle persists, report failed
with the cause. Include the tests actually run and their results in the final report.
For knowledge, report reusable project facts you verified yourself, in the user's language.
Cite repository-relative paths and symbols or reproducible checks and their observed results.
Omit guesses, credentials, personal data and raw transcripts. Use an empty list when there
is no such finding or the task is incomplete. Hermes publishes these notes after completion."""
CONCURRENCY = 4


def digest(*values):
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


@contextmanager
def try_lock(path):
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        # Closing the last inherited descriptor releases the lock even after a crash.
        yield handle


def arguments(engine, session):
    if engine == "codex":
        args = ["codex", "exec"]
        if session:
            args += ["resume", session]
        return args + ["--dangerously-bypass-approvals-and-sandbox", "--output-schema", str(SCHEMA),
                       "--json", "-"]
    args = ["claude", "-p", "--dangerously-skip-permissions", "--verbose", "--output-format", "stream-json",
            "--json-schema", SCHEMA.read_text()]
    return args + (["--resume", session] if session else [])


def decode_report(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if (isinstance(value, dict) and value.get("status") in ("completed", "blocked", "failed")
            and isinstance(value.get("summary"), str) and value["summary"].strip()
            and isinstance(value.get("tests"), str)):
        return value
    return None


class Events:
    def __init__(self, session_file):
        self.file = session_file
        self.session = str(UUID(self.file.read_text().strip())) if self.file.exists() else None
        self.success = False
        self.report = None
        self.error = ""

    def read(self, stream, finished):
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                return
            if not finished and not line.endswith(b"\n"):
                stream.seek(offset)
                return
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            kind = event.get("type")
            if kind == "thread.started" or (kind == "system" and event.get("subtype") == "init"):
                value = str(UUID(event.get("thread_id") or event["session_id"]))
                if self.session and self.session != value:
                    raise ValueError("CLI가 요청한 세션과 다른 세션을 열었습니다.")
                if self.session is None:
                    tmp = self.file.with_suffix(".tmp")
                    tmp.write_text(value)
                    tmp.replace(self.file)
                    self.session = value
            elif kind == "item.completed" and event["item"].get("type") == "agent_message":
                self.report = decode_report(event["item"].get("text"))
            elif kind == "turn.completed":
                self.success = True
            elif kind == "turn.failed":
                self.success = False
                self.error = str(event.get("error", {}).get("message", ""))[:500]
            elif kind == "result":
                self.success = event.get("subtype") == "success" and not event.get("is_error")
                self.report = decode_report(event.get("structured_output") or event.get("result"))
                if not self.success:
                    self.error = str(event.get("subtype", "Claude Code error"))


def stop_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        proc.wait()


def task_context(conn, task):
    # The stock context also contains board-wide recent work by assignee.
    # cli:codex and cli:claude are shared across otherwise independent projects.
    now = int(time.time())
    lines = [f"Kanban card {task.id} in {task.workspace_path}", task.body or task.title, ""]
    context_sections._ctx_attachments(lines, kb.list_attachments(conn, task.id))
    context_sections._ctx_prior_attempts(lines, conn, task.id, now)
    context_sections._ctx_parent_results(lines, conn, task.id, now)
    context_sections._ctx_comments(lines, kb.list_comments(conn, task.id), now)
    if task.session_id:
        previous = conn.execute("""
            SELECT id, result FROM tasks WHERE session_id = ? AND workspace_path = ?
            AND status = 'done' AND id != ? ORDER BY completed_at DESC, rowid DESC LIMIT 1
        """, (task.session_id, task.workspace_path, task.id)).fetchone()
        if previous and previous["result"]:
            lines += [f"Previous completed card {previous['id']} in this checkout (worker report):",
                      previous["result"][:8000]]
    return "\n".join(lines)


def execute(conn, task, cwd, state, project_lock, stopping, slug):
    engine = LANES[task.assignee]
    observer_root = Path('/workspaces/pharmmate')
    observer = slug == 'pharmmate' or Path(cwd).resolve().is_relative_to(observer_root)
    if observer and Path(cwd).resolve() != observer_root:
        raise RuntimeError('Pharmmate observer requires the protected workspace root.')
    session_key = digest(str(cwd), task.session_id or task.id, engine)
    if observer:
        session_key = digest(session_key, task.current_run_id)
    events = Events(state / "sessions" / session_key)
    run_id = task.current_run_id
    runs = state / "runs" if slug == BOARD else state / "boards" / slug / "runs"
    files = runs / task.id / str(run_id)
    files.mkdir(parents=True, exist_ok=True)
    log = files / "events.jsonl"
    env = hermes_subprocess_env(inherit_credentials=True, base_env=dict(
        os.environ, HERMES_KANBAN_TASK=task.id, HERMES_KANBAN_BOARD=slug,
        HERMES_KANBAN_DB=str(kb.kanban_db_path(board=slug))))
    env.pop("TELEGRAM_BOT_TOKEN", None)
    # Inherit the project lock into the CLI. A replacement worker cannot edit the
    # checkout while an earlier process is winding down.
    cli = (['/usr/bin/python3', '-I', '-B', str(observer_root/'.observer/start.py'), engine, '--worker', str(SCHEMA)]
           if observer else arguments(engine, events.session))
    cmd = ["setpriv", "--pdeathsig", "TERM", "--", *cli]
    context = task_context(conn, task)
    with tempfile.TemporaryFile() as prompt, log.open("wb") as out, log.open("rb") as reader, \
            (files / "stderr.log").open("wb") as err:
        prompt.write(f"{REPORT}\n\n{context}\n".encode())
        prompt.seek(0)
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=prompt, stdout=out, stderr=err,
                                start_new_session=True, pass_fds=(project_lock.fileno(),))
        try:
            started = time.monotonic()
            next_heartbeat = started
            saved_session = False
            while True:
                code = proc.poll()
                events.read(reader, finished=code is not None)
                if events.session and not saved_session:
                    kb.add_comment(conn, task.id, "project-cli",
                                   f"{engine} session {events.session} · {cwd} · log {log}")
                    saved_session = True
                if code is not None:
                    break
                now = time.monotonic()
                if stopping.is_set() or (task.max_runtime_seconds and now - started >= task.max_runtime_seconds):
                    raise InterruptedError("실행 중단. 작업 폴더와 로그를 확인한 뒤 재개하세요.")
                if now >= next_heartbeat:
                    if not kb.heartbeat_claim(conn, task.id, claimer=task.claim_lock):
                        raise RuntimeError("Kanban 작업의 소유권이 변경됐습니다.")
                    heartbeat_worker(conn, task.id, expected_run_id=run_id)
                    next_heartbeat = now + 15
                stopping.wait(0.5)
        finally:
            if proc.poll() is None:
                stop_group(proc)
    if code or not events.session or not events.success or not events.report:
        raise RuntimeError(f"{engine} 종료 코드 {code}. {events.error or '완료 보고를 확인할 수 없습니다.'} 로그 {files}")
    report = events.report
    summary = f"{report['summary']}\n\n테스트 · {report['tests']}"
    if report["status"] != "completed":
        kind = "needs_input" if report["status"] == "blocked" else "capability"
        kb.block_task(conn, task.id, reason=summary, kind=kind, expected_run_id=run_id)
        return
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=cwd,
                          capture_output=True, text=True).stdout.strip()
    metadata = {"agent": engine, "cli_session_id": events.session, "workspace": str(cwd),
                "head": head or None, "tests": report["tests"], "log": str(log)}
    findings = knowledge.findings(report.get("knowledge"))
    if findings:
        metadata["knowledge"] = findings
        metadata["knowledge_workspace"] = str(knowledge_root(cwd))
    if not kb.complete_task(conn, task.id, result=summary, summary=summary, expected_run_id=run_id,
                            metadata=metadata):
        raise RuntimeError("Kanban 완료 조건 또는 실행 소유권을 확인하세요.")


def stop_event():
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    return stopping


def card_workspace(path, kind, slug, card_id):
    if kind != "worktree":
        return project_dir(path) if path else None
    meta = kb.read_board_metadata(slug)
    if not meta.get("project_id") or not meta.get("default_workdir"):
        return None
    root = project_dir(meta["default_workdir"])
    target = root / ".worktrees" / card_id
    # Native project cards reserve this path before materializing the worktree on claim.
    if target.resolve() != target or (path and Path(path).resolve() not in (root, target)):
        return None
    return target


def process_card(card_id, lock_fd, slug):
    state = get_hermes_home() / "cli-workers"
    stopping = stop_event()
    with os.fdopen(lock_fd, "a") as lock, kb.scoped_current_board(slug), board(slug) as conn:
        task = kb.get_task(conn, card_id)
        if not task or task.assignee not in LANES:
            return
        try:
            workspace = card_workspace(task.workspace_path, task.workspace_kind, slug, card_id)
        except (OSError, ValueError):
            workspace = None
        if not os.path.samestat(os.fstat(lock_fd), (state / "locks" / digest(str(workspace))).stat()):
            raise RuntimeError("작업 폴더와 잠금 파일이 일치하지 않습니다.")
        if task.status == "running":
            # Nobody holds its checkout lock, including the previous CLI. Its last
            # command might already have changed files; let the user reconcile it.
            kb.block_task(conn, card_id, reason="이전 워커가 중단되었습니다. 작업 폴더와 실행 로그를 확인하세요.",
                          kind="transient", expected_run_id=task.current_run_id)
            return
        task = kb.claim_task(conn, card_id, expect_assignee=task.assignee)
        if not task:
            return
        try:
            if not adopt_worker_pid(conn, task.id, task.current_run_id, os.getpid()):
                return
            if not workspace or task.workspace_kind not in ("dir", "worktree"):
                raise ValueError("프로젝트 폴더 또는 프로젝트 보드의 Kanban worktree가 필요합니다.")
            if task.workspace_kind == "worktree":
                actual = workspaces.resolve_workspace(task, board=slug)
                if actual.resolve() != workspace:
                    raise ValueError("Kanban worktree가 예약한 작업 폴더와 일치하지 않습니다.")
                workspaces.set_workspace_path(conn, task.id, workspace)
                task.workspace_path = str(workspace)
            execute(conn, task, workspace, state, lock, stopping, slug)
        except Exception as exc:
            kind = "transient" if isinstance(exc, InterruptedError) else "capability"
            kb.block_task(conn, card_id, reason=str(exc), kind=kind, expected_run_id=task.current_run_id)


def serve():
    state = get_hermes_home() / "cli-workers"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("locks", "sessions", "runs"):
        (state / name).mkdir(mode=0o700, exist_ok=True)
    stopping = stop_event()
    with try_lock(state / "dispatcher.lock") as owner, worker_boards() as refresh:
        if owner is None:
            raise SystemExit("project-cli 워커가 이미 실행 중입니다.")
        indexer = threading.Thread(target=knowledge.watch, args=(stopping,), name="qmd-indexer", daemon=True)
        indexer.start()
        active = {}
        projects = {}
        try:
            while not stopping.is_set():
                active = {project: child for project, child in active.items() if child.poll() is None}
                # A running attempt precedes a waiting card, then preserve FIFO
                # even when two requests have the same one-second creation time.
                rows = []
                for slug, conn in refresh().items():
                    rows.extend(conn.execute("""
                        SELECT id, workspace_path, workspace_kind, (status = 'ready') AS waiting, created_at,
                               rowid AS sequence, ? AS board FROM tasks
                        WHERE assignee IN (?, ?) AND status IN ('running', 'ready')
                    """, (slug, *LANES)))
                rows.sort(key=lambda row: (row["waiting"], row["created_at"], row["sequence"], row["board"]))
                paths = {row["workspace_path"] for row in rows if row["workspace_kind"] == "dir"}
                projects = {path: cwd for path, cwd in projects.items() if path in paths}
                for row in rows:
                    if len(active) >= CONCURRENCY:
                        break
                    path = row["workspace_path"]
                    if row["workspace_kind"] != "dir" or path not in projects:
                        try:
                            workspace = card_workspace(path, row["workspace_kind"], row["board"], row["id"])
                        except (OSError, ValueError):
                            workspace = None
                        if row["workspace_kind"] == "dir":
                            projects[path] = workspace
                    else:
                        workspace = projects[path]
                    if workspace in active:
                        continue
                    with try_lock(state / "locks" / digest(str(workspace))) as lock:
                        if lock is not None:
                            cmd = ["setpriv", "--pdeathsig", "TERM", "--", "hermes", "project-cli",
                                   "run-card", row["id"], str(lock.fileno()), "--board", row["board"]]
                            active[workspace] = subprocess.Popen(cmd, pass_fds=(lock.fileno(),),
                                                                 start_new_session=True)
                stopping.wait(1)
        finally:
            stopping.set()
            for child in active.values():
                if child.poll() is None:
                    child.terminate()
            for child in active.values():
                child.wait()
            indexer.join(timeout=5)
