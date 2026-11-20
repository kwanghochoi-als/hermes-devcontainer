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
from hermes_cli.kanban_db_dispatch import adopt_worker_pid, heartbeat_worker
from hermes_constants import get_hermes_home
from tools.environments.local import hermes_subprocess_env

from .board import BOARD, LANES, board, project_dir

SCHEMA = Path(__file__).with_name("result.schema.json")
REPORT = """Execute the user's Kanban task in this checkout. Include the tests actually run and
their results in the final report. If work is incomplete or needs a person's approval, report
blocked and explain what is needed. Respect the project's existing approval hooks for publishing,
deployments, deletion, external messages, payments and access changes. Do not repeat an external
action if a previous attempt may have performed it."""
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
        args = ["codex", "exec", "--sandbox", "workspace-write", "--output-schema", str(SCHEMA)]
        if session:
            args += ["resume", session]
        return args + ["--json", "-"]
    args = ["claude", "-p", "--verbose", "--output-format", "stream-json",
            "--json-schema", SCHEMA.read_text()]
    return args + (["--resume", session] if session else [])


def decode_report(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if (isinstance(value, dict) and value.get("status") in ("completed", "blocked")
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


def execute(conn, task, cwd, state, project_lock, stopping):
    engine = LANES[task.assignee]
    session_key = digest(str(cwd), task.session_id or task.id, engine)
    events = Events(state / "sessions" / session_key)
    run_id = task.current_run_id
    files = state / "runs" / task.id / str(run_id)
    files.mkdir(parents=True, exist_ok=True)
    log = files / "events.jsonl"
    env = hermes_subprocess_env(inherit_credentials=True, base_env=dict(
        os.environ, HERMES_KANBAN_TASK=task.id, HERMES_KANBAN_BOARD=BOARD,
        HERMES_KANBAN_DB=str(kb.kanban_db_path(board=BOARD))))
    # Inherit the project lock into the CLI. A replacement worker cannot edit the
    # checkout while an earlier process is winding down.
    cmd = ["setpriv", "--pdeathsig", "TERM", "--", *arguments(engine, events.session)]
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
                    raise RuntimeError("실행 중단. 작업 폴더와 로그를 확인한 뒤 재개하세요.")
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
    if report["status"] == "blocked":
        raise RuntimeError(summary)
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=cwd,
                          capture_output=True, text=True).stdout.strip()
    metadata = {"agent": engine, "cli_session_id": events.session, "workspace": str(cwd),
                "head": head or None, "tests": report["tests"], "log": str(log)}
    if not kb.complete_task(conn, task.id, result=summary, summary=summary, expected_run_id=run_id,
                            metadata=metadata):
        raise RuntimeError("Kanban 완료 조건 또는 실행 소유권을 확인하세요.")


def stop_event():
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    return stopping


def process_card(card_id, lock_fd):
    state = get_hermes_home() / "cli-workers"
    stopping = stop_event()
    with os.fdopen(lock_fd, "a") as lock, board() as conn:
        task = kb.get_task(conn, card_id)
        if not task or task.assignee not in LANES:
            return
        try:
            workspace = project_dir(task.workspace_path) if task.workspace_path else None
        except (OSError, ValueError):
            workspace = None
        if not os.path.samestat(os.fstat(lock_fd), (state / "locks" / digest(str(workspace))).stat()):
            raise RuntimeError("작업 폴더와 잠금 파일이 일치하지 않습니다.")
        if task.status == "running":
            # Nobody holds its checkout lock, including the previous CLI. Its last
            # command might already have changed files; let the user reconcile it.
            kb.block_task(conn, card_id, reason="이전 워커가 중단되었습니다. 작업 폴더와 실행 로그를 확인하세요.",
                          kind="needs_input", expected_run_id=task.current_run_id)
            return
        task = kb.claim_task(conn, card_id, expect_assignee=task.assignee)
        if not task:
            return
        try:
            if not adopt_worker_pid(conn, task.id, task.current_run_id, os.getpid()):
                return
            if task.workspace_kind != "dir" or not workspace:
                raise ValueError("이 워커에는 dir:<프로젝트 또는 Git worktree 경로>가 필요합니다.")
            execute(conn, task, workspace, state, lock, stopping)
        except Exception as exc:
            kb.block_task(conn, card_id, reason=str(exc), kind="needs_input", expected_run_id=task.current_run_id)


def serve():
    state = get_hermes_home() / "cli-workers"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("locks", "sessions", "runs"):
        (state / name).mkdir(mode=0o700, exist_ok=True)
    stopping = stop_event()
    with try_lock(state / "dispatcher.lock") as owner, board() as conn:
        if owner is None:
            raise SystemExit("project-cli 워커가 이미 실행 중입니다.")
        active = {}
        projects = {}
        try:
            while not stopping.is_set():
                active = {project: child for project, child in active.items() if child.poll() is None}
                # A running attempt precedes a waiting card, then preserve FIFO
                # even when two requests have the same one-second creation time.
                rows = conn.execute("""
                    SELECT id, workspace_path FROM tasks
                    WHERE assignee IN (?, ?) AND status IN ('running', 'ready')
                    ORDER BY (status = 'running') DESC, created_at, rowid
                """, tuple(LANES)).fetchall()
                paths = {row["workspace_path"] for row in rows}
                projects = {path: cwd for path, cwd in projects.items() if path in paths}
                for row in rows:
                    if len(active) >= CONCURRENCY:
                        break
                    path = row["workspace_path"]
                    if path not in projects:
                        try:
                            projects[path] = project_dir(path) if path else None
                        except (OSError, ValueError):
                            projects[path] = None
                    workspace = projects[path]
                    if workspace in active:
                        continue
                    with try_lock(state / "locks" / digest(str(workspace))) as lock:
                        if lock is not None:
                            cmd = ["setpriv", "--pdeathsig", "TERM", "--", "hermes", "project-cli",
                                   "run-card", row["id"], str(lock.fileno())]
                            active[workspace] = subprocess.Popen(cmd, pass_fds=(lock.fileno(),),
                                                                 start_new_session=True)
                stopping.wait(1)
        finally:
            for child in active.values():
                if child.poll() is None:
                    child.terminate()
            for child in active.values():
                child.wait()
