"""Read one CLI attempt's diagnostics from its board and controller-owned files."""

from contextlib import ExitStack
import json
import os
import stat
from pathlib import Path

from agent.redact import redact_sensitive_text
from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

from .board import BOARD, LANES

TAIL_LIMIT = 65536
STEPS = {
    "workspace": "작업 폴더 검증", "container": "워커 컨테이너 시작",
    "snapshot": "원격 스냅샷 사전 점검", "prompt_hook": "요청 훅 자가 검사",
    "tool_hook": "도구 훅 자가 검사", "session": "CLI 세션 연결",
    "report": "CLI 결과 보고 수신",
}


def checkpoint(conn, task, step):
    with kb.write_txn(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id=? AND current_run_id=? AND status='running'",
                        (task.id, task.current_run_id)).fetchone():
            kb._append_event(conn, task.id, "cli_checkpoint", {"step": step}, run_id=task.current_run_id)


def last_steps(conn, task_id):
    stages = {}
    for run_id, kind, payload in conn.execute(
            "SELECT run_id,kind,payload FROM task_events WHERE task_id=? AND run_id IS NOT NULL "
            "AND kind IN ('claimed','cli_checkpoint') ORDER BY id", (task_id,)):
        if kind == "claimed":
            stages[run_id] = "카드 접수"
        else:
            try:
                detail = json.loads(payload or "{}")
            except (ValueError, TypeError):
                continue
            code = detail.get("step") if isinstance(detail, dict) else None
            step = STEPS.get(code) if isinstance(code, str) else None
            if step:
                stages[run_id] = step
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


def _events(text):
    # Show execution events; prompts, reasoning and tool payloads stay in the local transcript.
    lines = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            continue
        kind = event["type"]
        if kind in ("assistant", "user"):
            message = event.get("message")
            parts = message.get("content") if isinstance(message, dict) else None
            for part in parts if isinstance(parts, list) else ():
                if isinstance(part, dict) and part.get("type") in ("tool_use", "tool_result"):
                    lines.append(str(part.get("name") or part["type"]) + (" · 오류" if part.get("is_error") else ""))
            continue
        item = event.get("item")
        if isinstance(item, dict):
            if item.get("type") == "reasoning":
                continue
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
        lines.append(kind)
    return "\n".join(lines)


def read_log(conn, board, task_id, run_id, limit=TAIL_LIMIT):
    run = kb.get_run(conn, run_id)
    if not run or run.task_id != task_id or run.profile not in LANES or not board or log_board(conn) != board:
        raise LookupError("해당 카드의 CLI 시도를 찾을 수 없습니다.")
    slug = kb._require_slug(board)
    parts = ([] if slug == BOARD else ["boards", slug]) + ["runs", task_id, str(run.id)]
    if any(p in (".", "..") or Path(p).name != p for p in parts):
        raise ValueError("잘못된 실행 기록 경로입니다.")
    if not 1 <= limit <= TAIL_LIMIT:
        raise ValueError(f"로그 크기는 1~{TAIL_LIMIT} 바이트로 지정하세요.")
    stdout = stderr = ""
    truncated = exists = False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        try:
            directory = os.open(get_hermes_home() / "cli-workers", flags)
            stack.callback(os.close, directory)
            for part in parts:
                directory = os.open(part, flags, dir_fd=directory)
                stack.callback(os.close, directory)
            stdout, out_exists, out_cut = _tail(directory, "events.jsonl", limit)
            stderr, err_exists, err_cut = _tail(directory, "stderr.log", limit)
            truncated, exists = out_cut or err_cut, out_exists or err_exists
        except FileNotFoundError:
            pass
    content = ("events.jsonl\n" + (_events(stdout) or "(표시할 이벤트 없음)") +
               "\n\nstderr.log\n" + (stderr.rstrip() or "(출력 없음)")) if exists else "이 시도의 로그 파일이 없습니다."
    return {"run_id": run.id, "exists": exists, "last_step": last_step(run, last_steps(conn, task_id)),
            "truncated": truncated, "content": redact_sensitive_text(content, force=True)}
