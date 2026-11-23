"""Read one CLI attempt's diagnostics from its board and controller-owned files."""

from contextlib import ExitStack, contextmanager
import errno
import fcntl
import json
import logging
import os
import stat
from pathlib import Path

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_worker_log import open_worker_log
from hermes_constants import get_hermes_home

from .board import BOARD, LANES

TAIL_LIMIT = 65536
ATTACHMENT_AUTHOR = "project-cli/run/"
STEPS = {
    "workspace": "작업 폴더 검증", "container": "워커 컨테이너 시작",
    "source_pin": "상위 작업의 Git 커밋 확정", "snapshot": "관찰 스냅샷 사전 점검", "prompt_hook": "요청 훅 자가 검사",
    "tool_hook": "도구 훅 자가 검사", "session": "CLI 세션 연결",
    "report": "CLI 결과 보고 수신",
}


def checkpoint(conn, task, step, **details):
    with kb.write_txn(conn):
        current = kb.get_task(conn, task.id)
        if not current or current.current_run_id != task.current_run_id or current.status != "running":
            return
        kb._append_event(conn, task.id, "cli_checkpoint", {"step": step, **details}, run_id=task.current_run_id)
    live_line(log_board(conn), task.id, task.current_run_id, STEPS[step])


def last_steps(conn, task_id):
    stages = {}
    for event in kb.list_events(conn, task_id):
        if event.kind == "claimed":
            stages[event.run_id] = "카드 접수"
        elif event.kind == "cli_checkpoint" and isinstance(event.payload, dict):
            code = event.payload.get("step")
            step = STEPS.get(code) if isinstance(code, str) else None
            if step:
                stages[event.run_id] = step
    return stages


def last_step(run, stages):
    if run.outcome == "completed":
        return "카드 완료 기록"
    return stages.get(run.id) or "기록 없음"


def log_board(conn):
    """Bind a log to the DB actually opened, including requests using the active-board default."""
    db = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    path = Path(db).resolve()
    if path.name == "kanban.db" and path.parent.parent == kb.boards_root().resolve():
        return path.parent.name
    if path == (kb.kanban_home() / "kanban.db").resolve():
        return kb.DEFAULT_BOARD
    return None


def _tail(folder, filename, limit):
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return "", False, False
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("일반 로그 파일만 조회할 수 있습니다.")
        start = max(0, info.st_size - limit)
        stream.seek(max(0, start - 1))
        if start and stream.read(1) != b"\n":
            stream.readline(limit)
        data = stream.read(min(limit, max(0, info.st_size - stream.tell())))
    return data.decode("utf-8", errors="replace"), True, bool(start)


def event_lines(event):
    # Only event types, tool names, exit codes and errors belong in a card log.
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return []
    kind = event["type"]
    if kind in ("assistant", "user"):
        message = event.get("message")
        parts = message.get("content") if isinstance(message, dict) else None
        lines = []
        for part in parts if isinstance(parts, list) else ():
            if isinstance(part, dict) and part.get("type") in ("tool_use", "tool_result"):
                lines.append(str(part.get("name") or part["type"]) + (" · 오류" if part.get("is_error") else ""))
        return lines
    item = event.get("item")
    if isinstance(item, dict):
        if item.get("type") == "reasoning":
            return []
        kind += " · " + str(item.get("type") or "")
        if item.get("exit_code") is not None:
            kind += " · exit " + str(item["exit_code"])
    elif kind in ("error", "turn.failed"):
        error = event.get("error")
        kind += " · " + str(error.get("message", "") if isinstance(error, dict) else event.get("message", ""))
    elif kind == "observer.checkpoint":
        kind += " · " + STEPS.get(str(event.get("step")), "")
    elif isinstance(event.get("subtype"), str):
        kind += " · " + event["subtype"]
    return [kind]


def _events(text):
    lines = []
    for line in text.splitlines():
        try:
            lines.extend(event_lines(json.loads(line)))
        except ValueError:
            continue
    return "\n".join(lines)


@contextmanager
def _run_directory(slug, task_id, run_id):
    parts = ([] if slug == BOARD else ["boards", slug]) + ["runs", task_id, str(run_id)]
    if any(not p or p in (".", "..") or Path(p).name != p for p in parts):
        raise ValueError("잘못된 실행 기록 경로입니다.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        directory = os.open(get_hermes_home() / "cli-workers", flags)
        stack.callback(os.close, directory)
        for part in parts:
            directory = os.open(part, flags, dir_fd=directory)
            stack.callback(os.close, directory)
        yield directory


def read_log(conn, board, task_id, run_id, limit=TAIL_LIMIT):
    run = kb.get_run(conn, run_id)
    if not run or run.task_id != task_id or run.profile not in LANES or not board or log_board(conn) != board:
        raise LookupError("해당 카드의 CLI 시도를 찾을 수 없습니다.")
    if not 1 <= limit <= TAIL_LIMIT:
        raise ValueError(f"로그 크기는 1~{TAIL_LIMIT} 바이트로 지정하세요.")
    stdout = stderr = ""
    truncated = exists = False
    try:
        with _run_directory(board, task_id, run.id) as directory:
            stdout, out_exists, out_cut = _tail(directory, "events.jsonl", limit)
            stderr, err_exists, err_cut = _tail(directory, "stderr.log", limit)
            truncated, exists = out_cut or err_cut, out_exists or err_exists
    except FileNotFoundError:
        pass
    content = ("events.jsonl\n" + (_events(stdout) or "(표시할 이벤트 없음)") +
               "\n\nstderr.log\n" + (stderr.rstrip() or "(출력 없음)")) if exists else "이 시도의 로그 파일이 없습니다."
    return {"run_id": run.id, "exists": exists, "last_step": last_step(run, last_steps(conn, task_id)),
            "truncated": truncated, "content": redact_sensitive_text(content, force=True, redact_url_credentials=True)}


def format_log(conn, task_id, run_id=None, limit=None):
    if run_id is None:
        run = kb.latest_run(conn, task_id)
        if not run:
            raise LookupError("해당 카드의 시도가 없습니다.")
        run_id = run.id
    log = read_log(conn, log_board(conn), task_id, run_id, TAIL_LIMIT if limit is None else limit)
    lines = [f"Run #{run_id} · 마지막 성공 단계 · {log['last_step']}"]
    if log["truncated"]:
        lines.append("(최근 로그, 잘린 줄 생략)")
    return "\n".join([*lines, log["content"]])


def live_line(slug, task_id, run_id, text):
    if not slug or not text:
        return
    try:
        path = kb.worker_log_path(task_id, board=slug)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open_worker_log(path) as out:
            safe = redact_sensitive_text(text, force=True, redact_url_credentials=True)
            out.write(f"Run #{run_id} · {safe}\n".encode())
    except OSError:
        logging.getLogger(__name__).exception("Kanban worker log · %s/%s", slug, task_id)


def live_stderr(slug, task_id, run_id, previous, *, finished):
    try:
        with _run_directory(slug, task_id, run_id) as directory:
            text, _, _ = _tail(directory, "stderr.log", 4096)
    except (ValueError, OSError):
        return previous
    if not finished:
        text = text[:text.rfind("\n") + 1]
    safe = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    if safe and safe != previous:
        live_line(slug, task_id, run_id, "stderr.log\n" + safe.removeprefix(previous).rstrip())
    return safe


def input_attachments(conn, task_id):
    return [a for a in kb.list_attachments(conn, task_id)
            if not (a.uploaded_by or "").startswith(ATTACHMENT_AUTHOR)]


def publish_closed(conn, slug):
    """The single dispatcher publishes in batches, including attempts closed before an upgrade."""
    # Project deletion holds the exclusive side while removing the board and its blobs.
    with (get_hermes_home() / ".project-create.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        if not kb.board_exists(slug) or kb.read_board_metadata(slug).get("archived"):
            return False
        pending = conn.execute("""
            SELECT r.id, r.task_id FROM task_runs r WHERE r.ended_at IS NOT NULL AND r.profile IN (?, ?)
              AND NOT EXISTS (SELECT 1 FROM task_attachments a WHERE a.task_id=r.task_id
                              AND a.uploaded_by=? || r.id)
            ORDER BY r.id LIMIT 12
        """, (*LANES, ATTACHMENT_AUTHOR)).fetchall()
        for run_id, task_id in pending:
            try:
                log = read_log(conn, slug, task_id, run_id)
                step, content = log["last_step"], log["content"]
                if log["truncated"]:
                    content = "(최근 로그, 잘린 줄 생략)\n" + content
            except (ValueError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                step = last_step(kb.get_run(conn, run_id), last_steps(conn, task_id))
                content = "저장된 로그 파일을 안전하게 열 수 없습니다."
            title = f"Run {run_id} · {step}"
            kb.store_attachment_bytes(conn, task_id, title + ".txt",
                                      f"{title}\n\n{content}\n".encode(), content_type="text/plain",
                                      uploaded_by=ATTACHMENT_AUTHOR + str(run_id), board=slug)
    return len(pending) == 12
