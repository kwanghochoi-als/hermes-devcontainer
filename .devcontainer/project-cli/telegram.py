"""One operator DM for Hermes human input. Only the Hermes gateway consumes updates."""

import asyncio
from contextlib import closing, contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
import getpass
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

from agent.redact import redact_sensitive_text
from hermes_cli.config import get_env_value_prefer_dotenv, load_config
from hermes_cli.plugin_session import session_cwd, session_surface
from hermes_constants import get_hermes_home

TRANSPORT = "project-telegram"
STALE = "이미 처리됐거나 만료된 요청입니다."
OPERATOR_COMMANDS = {"workok", "workno", "workreply", "workstatus", "workrequest"}
LOCAL_SURFACES = {"", "cli", "local", "tui", "desktop", "gui", "acp", "kanban"}


def database():
    return get_hermes_home() / "cli-workers" / "telegram.sqlite3"


@contextmanager
def store(path):
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn


@dataclass(frozen=True)
class Route:
    path: Path
    key: str
    owner: str
    token: str = field(repr=False)


def credentials():
    token = (get_env_value_prefer_dotenv("TELEGRAM_BOT_TOKEN") or "").strip()
    users = (get_env_value_prefer_dotenv("TELEGRAM_ALLOWED_USERS") or "").split(",")
    return token, [item.strip() for item in users if item.strip()]


def route():
    path = database()
    if not path.is_file():
        return None
    with store(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='binding'").fetchone():
            return None
        row = conn.execute("SELECT * FROM binding WHERE id=1").fetchone()
    token, users = credentials()
    if row and token.partition(":")[0] == row["bot"] and row["owner"] in users:
        return Route(path, row["generation"], row["owner"], token)
    return None


def bind(path, bot, owner):
    from hermes_cli import kanban_db as kb
    from .board import BOARD
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    with store(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1),
                generation TEXT NOT NULL, bot TEXT NOT NULL, owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (route TEXT, board TEXT, event INTEGER NOT NULL,
                PRIMARY KEY(route, board));
            CREATE TABLE IF NOT EXISTS requests (ref TEXT PRIMARY KEY, route TEXT NOT NULL, kind TEXT NOT NULL,
                expires REAL, response TEXT, board TEXT, card TEXT, event INTEGER, waiter TEXT);
            CREATE TABLE IF NOT EXISTS turn_reports (route TEXT, session TEXT, turn TEXT, project TEXT, outcome TEXT,
                PRIMARY KEY(route, session, turn));
        """)
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM binding WHERE id=1").fetchone()
        if old and (old["bot"], old["owner"]) == (bot, owner):
            return
        generation = secrets.token_hex(16)
        # A previously archived board may become a project board after binding.
        slugs = {BOARD} | {meta["slug"] for meta in kb.list_boards(include_archived=True)}
        for slug in slugs:
            db = kb.kanban_db_path(board=slug)
            if not db.is_file():
                continue
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as kanban:
                last = kanban.execute("SELECT COALESCE(MAX(id),0) FROM task_events").fetchone()[0]
            conn.execute("INSERT INTO cursors VALUES (?,?,?)", (generation, slug, last))
        conn.execute("INSERT OR REPLACE INTO binding VALUES (1,?,?,?)", (generation, bot, owner))
        conn.execute("DELETE FROM requests WHERE route != ?", (generation,))
        conn.execute("DELETE FROM cursors WHERE route != ?", (generation,))
        conn.execute("DELETE FROM turn_reports WHERE route != ?", (generation,))


def selected():
    return load_config().get("security", {}).get("approval", {}).get("transport") == TRANSPORT


def clean(value):
    return redact_sensitive_text(str(value or ""), force=True)


def bot_client(token):
    from gateway.config import Platform, load_gateway_config
    from plugins.platforms.telegram.standalone import _telegram_bot
    config = load_gateway_config().platforms.get(Platform.TELEGRAM)
    return _telegram_bot(token, config.extra if config else None)


async def _send(channel, message, decision_ref):
    from plugins.platforms.telegram.standalone import _send_telegram_message_with_retry
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    # Plain text and disabled link previews, including for text supplied by a worker.
    keyboard = None
    if decision_ref:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("승인", callback_data=f"hwork:ok:{decision_ref}"),
            InlineKeyboardButton("거절", callback_data=f"hwork:no:{decision_ref}"),
        ]])
    async with bot_client(channel.token) as bot:
        # Each code point can occupy two UTF-16 units. Keep even emoji-heavy commands intact.
        for pos in range(0, len(message), 1800):
            final = pos + 1800 >= len(message)
            await _send_telegram_message_with_retry(bot, chat_id=int(channel.owner), text=message[pos:pos + 1800],
                                                    disable_web_page_preview=True, reply_markup=keyboard if final else None)


def send(channel, message, decision_ref=None):
    with store(channel.path) as conn:
        if not conn.execute("SELECT 1 FROM binding WHERE generation=?", (channel.key,)).fetchone():
            raise RuntimeError("Telegram 운영자 연결이 변경됐습니다.")
    asyncio.run(_send(channel, message, decision_ref))


async def verify_dm(token, owner):
    async with bot_client(token) as bot:
        chat = await bot.get_chat(int(owner))
        if chat.type != "private" or str(chat.id) != owner:
            raise ValueError("운영자의 개인 대화가 필요합니다.")
        return str(bot.id)


def setup():
    from hermes_cli.config import save_env_value, save_env_value_secure, set_config_value
    if not sys.stdin.isatty():
        raise SystemExit("로컬 대화형 터미널에서 실행하세요.")
    token, users = credentials()
    if not token:
        token = getpass.getpass("BotFather 봇 토큰 (입력 숨김) · ").strip()
    default = users[0] if len(users) == 1 and users[0].isdecimal() else ""
    print("BotFather로 만든 봇의 개인 대화에 /start를 보내세요. 이미 보냈다면 아래 ID 입력으로 진행하세요.")
    print("본인의 숫자 ID는 별도 대화인 https://t.me/userinfobot 에서 /start를 보내 확인할 수 있습니다.")
    owner = input("운영자 본인의 Telegram 숫자 사용자 ID" + (" [현재 등록 계정]" if default else "") + " · ").strip() or default
    if not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
        raise SystemExit("개인 계정의 양의 정수 ID가 필요합니다.")
    try:
        bot = asyncio.run(verify_dm(token, owner))
    except Exception:
        raise SystemExit("Telegram 개인 대화를 확인하지 못했습니다. 토큰, 사용자 ID, /start 및 네트워크를 확인하세요.") from None
    save_env_value_secure("TELEGRAM_BOT_TOKEN", token)
    if owner not in users:
        save_env_value("TELEGRAM_ALLOWED_USERS", ",".join([*users, owner]))
    bind(database(), bot, owner)
    set_config_value("security.approval.transport_fallback", "deny")
    set_config_value("security.approval.transport", TRANSPORT)
    if not route() or not selected():
        raise SystemExit("저장된 Hermes 프로필 설정을 확인하세요. 운영자 연결을 활성화하지 못했습니다.")
    service = Path(__file__).resolve().parents[1] / "start-service.sh"
    subprocess.run(["bash", str(service), "restart-gateway"], check=True)
    print("Telegram 운영자 DM을 연결했습니다. 봇에 /workstatus 를 보내 수신을 확인하세요.")


def status():
    token, users = credentials()
    print("Telegram 모듈 ·", "설치됨" if importlib.util.find_spec("telegram") else "설치 필요")
    print("봇 토큰 ·", "등록됨" if token else "미등록")
    print("허용 사용자 ·", "등록됨" if users else "미등록")
    print("운영자 DM ·", "연결됨" if route() else "미연결")
    print("Hermes 명령 승인 ·", "Telegram" if selected() else "기본 채널")


def authorized(channel, platform, source):
    return bool(channel and platform == "telegram" and source and
                getattr(getattr(source, "platform", None), "value", None) == "telegram" and
                source.chat_type == "dm" and not source.is_bot and str(source.user_id) == channel.owner and
                str(source.chat_id) == channel.owner)


def wire(application, adapter):
    """Receive authenticated Telegram updates directly, even while the DM's agent is waiting."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from telegram.ext import CallbackQueryHandler, CommandHandler, filters
    from .telegram_kanban import watch

    async def receive(update, context):
        query, user, chat = update.callback_query, update.effective_user, update.effective_chat
        if not user or not chat or not update.effective_message:
            return
        if query:
            _, action, raw = query.data.split(":")
        else:
            message = update.message
            if not message or message.forward_origin or message.via_bot:
                return
            name, *tail = message.text.split(maxsplit=1)
            action, raw = name.partition("@")[0].lower()[5:], tail[0] if tail else ""
        src = SessionSource(platform=Platform.TELEGRAM, chat_id=str(chat.id), user_id=str(user.id),
                            chat_type="dm" if chat.type == "private" else str(chat.type), is_bot=user.is_bot)
        channel = await asyncio.to_thread(route)
        permitted = (authorized(channel, "telegram", src) and str(context.bot.id) == channel.token.partition(":")[0]
                     and adapter._is_callback_user_authorized(src.user_id, chat_id=src.chat_id, chat_type=src.chat_type))
        runner = getattr(adapter, "gateway_runner", None)
        if permitted and runner:
            permitted = runner._check_slash_access(src, "work" + action) is None
        if not permitted:
            if query:
                await query.answer("등록된 운영자의 개인 대화에서 사용할 수 있습니다.", show_alert=True)
            return
        result = await asyncio.to_thread(command, action, raw, channel)
        if query:
            await query.answer(result[:180], show_alert=True)
            if result != STALE and query.message and query.message.text:
                await query.edit_message_text(f"{query.message.text}\n\n{result[:180]}",
                                              reply_markup=None, disable_web_page_preview=True)
        else:
            for pos in range(0, len(result), 1800):
                await update.message.reply_text(result[pos:pos + 1800])

    application.add_handler(CallbackQueryHandler(receive, pattern=r"^hwork:(?:ok|no):[akq][a-f0-9]{16}$"))
    application.add_handler(CommandHandler(sorted(OPERATOR_COMMANDS), receive, filters=filters.UpdateType.MESSAGE))
    if route():
        threading.Thread(target=copy_context().run, args=(watch,), name="telegram-kanban", daemon=True).start()


def process_identity(pid):
    try:
        # Linux proc start time also distinguishes a recycled PID after a gateway/CLI restart.
        ticks = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return f"{pid}/{ticks}"
    except (OSError, IndexError):
        return None


def command(action, raw, channel):
    if action in ("status", "request"):
        from .telegram_kanban import work_status, reissue
        return work_status(raw.strip()) if action == "status" else reissue(channel, raw.split())
    parts = raw.strip().split(maxsplit=1)
    if not parts or (action == "reply") != (len(parts) == 2):
        hint = " <답변>" if action == "reply" else ""
        return f"/work{action} <요청 ID>{hint}"
    ref = parts[0]
    with store(channel.path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM requests WHERE ref=? AND route=? AND route="
                           "(SELECT generation FROM binding)", (ref, channel.key)).fetchone()
        if not row or row["response"] is not None or (row["expires"] is not None and row["expires"] <= time.time()):
            return STALE
        if row["kind"] == "card":
            from .telegram_kanban import decide
            response = decide(row, action, parts[1] if len(parts) > 1 else "")
            conn.execute("DELETE FROM requests WHERE ref=?", (ref,))
            return response
        waiter = row["waiter"]
        if not waiter or process_identity(waiter.split("/")[0]) != waiter:
            return STALE
        if (row["kind"] == "approval" and action == "reply") or (row["kind"] == "question" and action == "ok"):
            return "요청 메시지에 표시된 명령을 사용하세요."
        value = parts[1] if action == "reply" else ""
        conn.execute("UPDATE requests SET response=? WHERE ref=?", (json.dumps([action, value]), ref))
        return {"ok": "이번 요청을 한 번 승인했습니다.", "no": "요청을 거절했습니다.",
                "reply": "원래 세션에 답변을 전달했습니다."}[action]


def project_label():
    cwd = session_cwd()
    return clean(Path(cwd).name) if cwd else ""


def request(channel, kind, ref, expires):
    with store(channel.path) as conn:
        conn.execute("DELETE FROM requests WHERE expires <= ?", (time.time(),))
        conn.execute("INSERT INTO requests(ref,route,kind,expires,waiter) VALUES (?,?,?,?,?)",
                     (ref, channel.key, kind, expires, process_identity(os.getpid())))


def forget(channel, refs):
    with store(channel.path) as conn:
        conn.executemany("DELETE FROM requests WHERE route=? AND ref=?", [(channel.key, ref) for ref in refs])


def wait_reply(channel, ref, expires):
    from tools.approval_human_wait import activity_heartbeat
    from tools.interrupt import is_interrupted
    heartbeat = activity_heartbeat("waiting for Telegram operator")
    while expires is None or time.time() < expires:
        with store(channel.path) as conn:
            row = conn.execute("SELECT response FROM requests WHERE ref=? AND route="
                               "(SELECT generation FROM binding)", (ref,)).fetchone()
        if not row or is_interrupted():
            return "cancelled", ""
        if row["response"] is not None:
            return json.loads(row["response"])
        heartbeat()
        time.sleep(0.25)
    return "timed_out", ""


# The host calls pre/post on the originating turn but presents requests on a separate thread.
# Capture the profile and workspace there and withdraw the matching request on interruption.
_approval_origins = {}
_presenting = threading.local()


def approval_started(surface, request_id=None, **kwargs):
    if surface == f"transport:{TRANSPORT}" and request_id and (channel := route()):
        ref = "a" + request_id[:16]
        _approval_origins[request_id] = (channel, project_label(), ref)
        _presenting.request_id = request_id


def approval_finished(request_id=None, **kwargs):
    origin = _approval_origins.pop(request_id, None)
    if origin:
        forget(origin[0], [origin[2]])
    if getattr(_presenting, "request_id", None) == request_id:
        _presenting.request_id = None


def local_notice(kind, platform, session_id="", **kwargs):
    if platform == "telegram" or not database().is_file() or not selected() or not (channel := route()):
        return
    from tools.approval_context import _approval_turn_id
    turn = _approval_turn_id.get()
    project = project_label() or platform
    if session_id and turn and platform in LOCAL_SURFACES and session_surface() in LOCAL_SURFACES:
        with store(channel.path) as conn:
            conn.execute("INSERT OR IGNORE INTO turn_reports(route,session,turn,project) VALUES (?,?,?,?)",
                         (channel.key, session_id, turn, project))
    # Sudo and tools whose host approval policy requires the original UI still notify the operator.
    if kind not in ("sudo", "approval") or getattr(_presenting, "request_id", None):
        return
    label = "sudo 비밀번호 입력" if kind == "sudo" else "원래 세션에서의 승인"
    text = f"Hermes · {project}\n{label}이 필요합니다. 원래 작업 화면을 열어 확인하세요."

    def deliver():
        try:
            send(channel, text)
        except Exception as exc:
            logging.getLogger(__name__).warning("Telegram local prompt notice failed (%s)", type(exc).__name__)

    threading.Thread(target=deliver, name="telegram-local-notice", daemon=True).start()


def turn_finished(session_id, turn_id="", completed=False, failed=False, interrupted=False, **kwargs):
    if not turn_id or not (path := database()).is_file():
        return
    if interrupted:
        outcome = "실행 중단"
    elif failed:
        outcome = "실행 실패"
    elif completed:
        outcome = "응답 완료"
    else:
        outcome = "실행 종료"
    with store(path) as conn:
        conn.execute("UPDATE turn_reports SET outcome=? WHERE route=(SELECT generation FROM binding) "
                     "AND session=? AND turn=? AND outcome IS NULL", (outcome, session_id, turn_id))


def deliver_turns(channel):
    with store(channel.path) as conn:
        pending = conn.execute("SELECT * FROM turn_reports WHERE route=? AND outcome IS NOT NULL ORDER BY rowid LIMIT 16",
                               (channel.key,)).fetchall()
    for item in pending:
        send(channel, f"Hermes {item['outcome']} · {item['project']}\n세션 {clean(item['session'])}")
        with store(channel.path) as conn:
            conn.execute("DELETE FROM turn_reports WHERE route=? AND session=? AND turn=?",
                         (channel.key, item["session"], item["turn"]))


def present(req):
    origin = _approval_origins.get(req.request_id)
    if not origin:
        return req.respond("deny")
    channel, project, ref = origin
    expires = time.time() + req.timeout_seconds
    text = (f"Hermes 명령 승인 · {project or req.surface}\n{req.description}\n\n{req.command}\n\n"
            f"{req.timeout_seconds:g}초 이내에 선택하세요.\n/workok {ref}\n/workno {ref}")
    try:
        request(channel, "approval", ref, expires)
        if _approval_origins.get(req.request_id) is not origin:
            return req.respond("deny")
        send(channel, text, ref)
        action, _ = wait_reply(channel, ref, expires)
        return req.respond("once" if action == "ok" else "deny")
    finally:
        forget(channel, [ref])


def clarify_callback(original):
    # Other messaging participants answer in their own conversation.
    if original is None or session_surface() not in LOCAL_SURFACES:
        return original
    if not database().is_file() or not selected() or not (channel := route()):
        return original
    return lambda questions: ask(channel, questions)


def ask(channel, questions):
    from tools.clarify_gateway import get_clarify_timeout
    timeout = get_clarify_timeout()
    expires = time.time() + timeout if timeout > 0 else None
    refs, answers = [], {}
    try:
        for question in questions:
            ref = "q" + secrets.token_hex(8)
            request(channel, "question", ref, expires)
            refs.append(ref)
            choices = question["choices_offered"] or []
            text = [f"Hermes 질문 · {project_label() or '로컬 세션'}", clean(question["question"])]
            text += [f"{i}. {clean(choice)}" for i, choice in enumerate(choices, 1)]
            text += ["", f"/workreply {ref} <답변>", f"/workno {ref}"]
            if question["multi_select"]:
                text.append("여러 선택은 번호를 쉼표로 구분하세요.")
            send(channel, "\n".join(text))
        for ref, question in zip(refs, questions):
            action, value = wait_reply(channel, ref, expires)
            if action != "reply":
                return {"answers": answers, "outcome": "timed_out" if action == "timed_out" else "cancelled"}
            choices = question["choices_offered"] or []
            indices = [v.strip() for v in value.split(",")]
            if choices and (question["multi_select"] or len(indices) == 1) and all(
                    v in {str(i) for i in range(1, len(choices) + 1)} for v in indices):
                picked = [choices[int(v) - 1] for v in indices]
                value = picked if question["multi_select"] else picked[0]
            answers[question["qid"]] = value
        return {"answers": answers, "outcome": "submitted"}
    except Exception:
        return {"answers": answers, "outcome": "undelivered", "notice": "Telegram 질문 전송을 확인하지 못했습니다."}
    finally:
        forget(channel, refs)
