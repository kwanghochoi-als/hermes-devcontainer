"""Deliver new project board events and apply an operator reply to one blocked attempt."""

import hashlib
import logging
from pathlib import Path
import time

from hermes_cli import kanban_db as kb

from . import telegram as tg
from hermes_constants import get_hermes_home
from .board import BOARD, LANES, board, worker_boards

log = logging.getLogger(__name__)
REPORTS = {"completed": "작업 완료", "blocked": "사람의 확인 필요", "review_requested": "검토 요청",
           "block_loop_detected": "반복 차단", "crashed": "워커 중단", "gave_up": "재시도 종료",
           "timed_out": "실행 시간 초과"}
REQUEST_LABELS = {"approval": ("승인 요청", "승인 대기"), "materials": ("자료 요청", "자료 대기"),
                  "environment": ("환경 조치 요청", "환경 조치 대기")}


def pending_status(conn, card, event):
    # Bind the answer to one unchanged block, including a native escalation for repeated blocks.
    row = conn.execute("""
        SELECT t.status, (SELECT MAX(e.id) FROM task_events e WHERE e.task_id=t.id AND
            e.kind IN ('blocked','block_loop_detected','unblocked','claimed','specified','edited','telegram_decided')) AS last
        FROM tasks t WHERE t.id=?
    """, (card,)).fetchone()
    if row and row["status"] in ("blocked", "triage") and row["last"] == event:
        return row["status"]
    return None


def pending_event(conn, card):
    row = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected') "
                       "ORDER BY id DESC LIMIT 1", (card,)).fetchone()
    if row and pending_status(conn, card, row["id"]):
        return kb.Event.from_row(row)
    return None


def request_kind(assignee, event):
    detail = event.payload or {}
    declared = detail.get("request_kind")
    if declared in REQUEST_LABELS:
        return declared
    if assignee in LANES and detail.get("kind") in ("transient", "capability"):
        return None
    # An older or native block carries no specific proposal authorizing a one-tap approval.
    return "materials"


def work_status(selected=""):
    lines = ["Hermes 프로젝트 작업"]
    with worker_boards() as refresh:
        boards = refresh()
        if selected and selected not in boards:
            return "조회할 보드 · " + ", ".join(sorted(boards))
        for slug, conn in boards.items():
            if selected and slug != selected:
                continue
            rows = conn.execute("SELECT id,title,status,assignee,current_run_id,last_heartbeat_at FROM tasks "
                                "WHERE status NOT IN ('done','archived','cancelled') ORDER BY created_at,id").fetchall()
            counts = {}
            details = []
            for row in rows:
                card, state = row["id"], row["status"]
                label = {"ready": "실행 대기", "running": "실행 중", "review": "검토",
                         "todo": "예정", "triage": "분류 대기"}.get(state, state)
                hint = ""
                if state in ("blocked", "triage"):
                    event = pending_event(conn, card)
                    if event:
                        kind = request_kind(row["assignee"], event)
                        label = REQUEST_LABELS[kind][1] if kind else "실행 오류"
                        if kind:
                            hint = f" · /workrequest {slug} {card}"
                    elif state == "blocked":
                        label = "보류"
                if state == "running" and row["assignee"] in LANES:
                    base = get_hermes_home() / "cli-workers"
                    runs = base / "runs" if slug == BOARD else base / "boards" / slug / "runs"
                    folder = runs / card / str(row["current_run_id"])
                    latest = max((p.stat().st_mtime for p in (folder / "events.jsonl", folder / "stderr.log")
                                  if p.is_file() and p.stat().st_size), default=None)
                    hint = (f" · 마지막 출력 {max(0, int(time.time() - latest)) // 60}분 전"
                            if latest else " · 첫 출력 대기")
                    heartbeat = kb._to_epoch(row["last_heartbeat_at"])
                    if heartbeat:
                        hint += f" · 워커 응답 {max(0, int(time.time() - heartbeat))}초 전"
                counts[label] = counts.get(label, 0) + 1
                if selected or hint:
                    details.append(f"  {card} · {label} · {tg.clean(row['title'])[:80]}{hint}")
            lines.append(f"{slug} · " + (", ".join(f"{k} {v}" for k,v in counts.items()) or "대기 작업 없음"))
            lines.extend(details)
    return "\n".join(lines)


def reissue(channel, parts):
    if len(parts) != 2:
        return "/workrequest <보드> <카드>"
    slug, card = parts
    with worker_boards() as refresh:
        conn = refresh().get(slug)
        if conn is None:
            return tg.STALE
        event = pending_event(conn, card)
        if not event:
            return tg.STALE
        task = kb.get_task(conn, card)
        kind = request_kind(task.assignee, event) if task else None
        if not kind:
            return tg.STALE
        if not publish(channel, slug, conn, event):
            return tg.STALE
        return f"{slug} · {card} · {REQUEST_LABELS[kind][0]}을 다시 표시했습니다."


def decide(row, action, reply):
    slug, card = row["board"], row["card"]
    if kb.read_board_metadata(slug).get("archived") or not kb.kanban_db_path(board=slug).is_file():
        return tg.STALE, True
    with kb.scoped_current_board(slug), board(slug) as conn, kb.write_txn(conn):
        event = pending_event(conn, card)
        if not event or event.id != row["event"]:
            return tg.STALE, True
        task = kb.get_task(conn, card)
        kind = request_kind(task.assignee, event)
        if not kind:
            return tg.STALE, True
        scoped_reply = kind == "approval" and action == "reply"
        if action not in tg.REQUEST_ACTIONS[kind] and not scoped_reply:
            return tg.ACTION_HINT[kind], False
        message = {"ok": "제시된 작업을 승인했습니다. 카드를 재개합니다.",
                   "no": "요청을 거절하고 카드를 보류합니다." if kind == "approval" else "요청과 카드를 보류합니다.",
                   "reply": ("운영자가 지정한 범위로 작업을 재개합니다." if scoped_reply else "제공한 자료·답변으로 카드를 재개합니다.") + f"\n{reply}",
                   "fixed": "운영자가 환경 조치를 완료했습니다. 실행 환경을 다시 확인하고 같은 카드를 재시도합니다."}[action]
        proposal = (event.payload or {}).get("reason")
        if proposal:
            label = {"approval": "결정 대상", "materials": "요청 자료", "environment": "확인할 환경"}[kind]
            message += f"\n{label} · {tg.clean(proposal)}"
        kb.add_comment(conn, card, "Telegram 운영자", message)
        if action != "no":
            if task.status == "triage":
                # Only a delivered block_loop_detected reaches this branch. Re-gate on parents.
                landing = kb._landing_status_after_parents(conn, card)
                conn.execute("UPDATE tasks SET status=? WHERE id=?", (landing, card))
                kb._append_event(conn, card, "specified", {"source": "telegram", "block_event": event.id})
            elif not kb.unblock_task(conn, card, allow_nested=True):
                raise RuntimeError("카드 상태가 변경되어 결정을 반영할 수 없습니다.")
        kb._append_event(conn, card, "telegram_decided", {"block_event": event.id, "request_kind": kind, "decision": action})
    return f"{slug} · {card}\n{message.splitlines()[0]}", True


def publish(channel, slug, kanban, event):
    task = kb.get_task(kanban, event.task_id)
    if task is None:
        return
    detail = event.payload or {}
    project = (kb.read_board_metadata(slug).get("name") if slug != BOARD else None) or (
        Path(task.workspace_path).name if task.workspace_path else slug)
    kind = None
    title = REPORTS[event.kind]
    if event.kind in ("blocked", "block_loop_detected"):
        kind = request_kind(task.assignee, event)
        if kind:
            title = REQUEST_LABELS[kind][0]
            if event.kind == "block_loop_detected":
                title += " · 반복 차단"
        else:
            title = "작업 실행 오류" if event.kind == "blocked" else "반복 실행 오류"
    header = f"{title} · {project} · {task.assignee or 'Hermes'}\n보드 {slug} · 카드 {task.id}"
    reason = detail.get("reason") or detail.get("summary") or detail.get("error") or ""
    if event.kind == "completed":
        run = kb.get_run(kanban, event.run_id) if event.run_id is not None else None
        reason = (run.summary if run and run.task_id == task.id else "") or reason or task.result
    reason = tg.clean(reason)
    message = f"{tg.clean(header)}\n{tg.clean(task.title)[:180]}\n\n{reason if kind else reason[:2800]}".strip()
    ref = None
    if kind:
        if not pending_status(kanban, task.id, event.id):
            return
        ref = "k" + hashlib.blake2s(f"{channel.key}/{slug}/{task.id}/{event.id}".encode(), digest_size=8).hexdigest()
        with tg.store(channel.path) as conn:
            conn.execute("INSERT OR IGNORE INTO requests(ref,route,kind,board,card,event) VALUES (?,?,'card',?,?,?)",
                         (ref, channel.key, slug, task.id, event.id))
        instructions = {
            "approval": f"제안 승인 · /workok {ref}\n범위를 지정해 재개 · /workreply {ref} <지시>\n거절 · /workno {ref}",
            "materials": f"자료·경로 또는 답변 전달 · /workreply {ref} <답변>\n보류 · /workno {ref}",
            "environment": ("devcontainer 또는 호스트에서 안내된 조치를 마친 뒤 재시도하세요. "
                            f"인증 정보는 로컬에서 입력하세요.\n조치 완료 · /workfixed {ref}\n보류 · /workno {ref}"),
        }
        message += "\n\n" + instructions[kind]
    tg.send(channel, message, ref, kind=kind)
    return True


def drain(channel, slug, kanban):
    with tg.store(channel.path) as conn:
        row = conn.execute("SELECT event FROM cursors WHERE route=? AND board=?", (channel.key, slug)).fetchone()
    cursor = row[0] if row else 0  # A board created after setup starts at its first event.
    batch = kanban.execute("SELECT * FROM task_events WHERE id>? ORDER BY id LIMIT 128", (cursor,)).fetchall()
    for row in batch:
        if row["kind"] in REPORTS:
            publish(channel, slug, kanban, kb.Event.from_row(row))
        elif row is not batch[-1]:
            continue
        # Commit after delivery. A restart during send can repeat a report with the exact same decision ID.
        with tg.store(channel.path) as conn:
            conn.execute("INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(route,board) DO UPDATE SET event=excluded.event",
                         (channel.key, slug, row["id"]))
    return len(batch)


def watch():
    from .worker import try_lock
    path = tg.database()
    if not path.is_file():
        return
    with try_lock(path.with_suffix(".lock")) as lock, worker_boards() as boards:
        if lock is None:
            return
        retry_after = {}
        next_turn_report = 0
        while True:
            busy = False
            try:
                channel = tg.route()
                if channel:
                    if time.monotonic() >= next_turn_report:
                        try:
                            tg.deliver_turns(channel)
                        except Exception as exc:
                            log.warning("Telegram session report deferred (%s)", type(exc).__name__)
                            next_turn_report = time.monotonic() + 30
                    for slug, kanban in boards().items():
                        if time.monotonic() < retry_after.get(slug, 0):
                            continue
                        try:
                            busy |= drain(channel, slug, kanban) == 128
                        except Exception as exc:
                            log.warning("Telegram board delivery deferred (%s, %s)", slug, type(exc).__name__)
                            retry_after[slug] = time.monotonic() + 30
            except Exception as exc:
                log.warning("Telegram report loop deferred (%s)", type(exc).__name__)
            time.sleep(0.1 if busy else 3)
