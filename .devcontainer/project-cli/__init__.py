"""Hermes entry points for the two project CLI workers."""

import json
import os
import sqlite3
from pathlib import Path

from agent.runtime_cwd import resolve_agent_cwd, scoped_session_cwd
from gateway.session_context import get_session_env

from .board import BOARD, ENGINES, board_for_checkout, enqueue, knowledge_root, project_dir


def bound_directory(session_id):
    path = scoped_session_cwd()
    if not path and session_id:
        from hermes_state import SessionDB
        with SessionDB() as sessions:
            path = (sessions.get_session(session_id) or {}).get("cwd")
    return path


def session_directory(platform, session_id=None):
    if path := bound_directory(session_id):
        return path
    if platform in ("", "cli", "local", "tui", "desktop"):
        return resolve_agent_cwd()
    raise ValueError("이 채널에 프로젝트 폴더가 연결되어 있지 않습니다. 원래 프로젝트 세션에서 접수하세요.")


def slash(engine, prompt, session_id, platform):
    parents = []
    prompt = prompt.strip()
    while prompt.split(None, 1)[:1] == ["--parent"]:
        parts = prompt.split(None, 2)
        if len(parts) != 3:
            return f"/{engine} --parent <상위 카드 ID> <하위 작업 지시>"
        _, parent, prompt = parts
        parents.append(parent)
    if not prompt.strip():
        return f"/{engine} <작업 지시>"
    try:
        slug, card = enqueue(engine, prompt, session_directory(platform, session_id), session_id, parents=parents)
    except (OSError, ValueError) as exc:
        return str(exc)
    return f"{engine} 작업 {card} 접수 · hermes kanban --board {slug} show {card}"


def submit_tool(args, *, session_id=None, tool_call_id=None, **kwargs):
    try:
        cwd = session_directory(get_session_env("HERMES_SESSION_PLATFORM", ""), session_id)
        slug, card = enqueue(args["agent"], args["prompt"], cwd, session_id, parents=args.get("parents", ()),
                             key=f"{session_id}/{tool_call_id}" if session_id and tool_call_id else None)
        return json.dumps({"board": slug, "task_id": card})
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def setup_cli(parser):
    commands = parser.add_subparsers(dest="action", required=True)
    submit = commands.add_parser("submit", help="Queue work in the current Git checkout")
    submit.add_argument("agent", choices=ENGINES)
    submit.add_argument("prompt")
    submit.add_argument("--session", help="Originating Hermes session ID for follow-up tasks")
    submit.add_argument("--parent", action="append", default=[], help="Upstream Kanban card ID (repeatable)")
    commands.add_parser("board", help="Print the current project's Kanban board")
    log = commands.add_parser("log", help="Read the sanitized log for a CLI attempt")
    log.add_argument("card")
    log.add_argument("--board", help="Kanban board (default: current project)")
    log.add_argument("--run", type=int, help="Run ID (default: latest)")
    log.add_argument("--tail", type=int, help="Maximum bytes per log stream")
    commands.add_parser("knowledge", help="Print the QMD collection for findings in the current project")
    scope = commands.add_parser("search-scope", help="Print the collections for a QMD search")
    scope.add_argument("--all-projects", action="store_true")
    from .layout import PROJECTS
    restore = commands.add_parser("restore", help="Restore a managed project from the workspace manifest")
    restore.add_argument("project", choices=sorted(PROJECTS))
    restore.add_argument("--clone", action="store_true", help="Clone missing source repositories")
    telegram = commands.add_parser("telegram", help="Connect or inspect the Telegram operator DM")
    telegram.add_argument("operation", choices=("setup", "status"))
    git_auth = commands.add_parser("git-auth", help="Configure or check this observer project’s read credentials")
    git_auth.add_argument("--project", help="Project ID or slug (default: current folder)")
    git_auth.add_argument("operation", choices=("setup", "status"))
    commands.add_parser("observer-check", help="Verify read-only observer mounts and both CLI hook paths in a fresh worker")
    commands.add_parser("serve", help="Run the Kanban CLI worker service")
    run = commands.add_parser("run-card", help="Internal supervised worker for one claimed checkout")
    run.add_argument("card_id")
    run.add_argument("lock_fd", type=int)
    run.add_argument("--board", default=BOARD)


def cli(args):
    if args.action == "restore":
        from .layout import restore
        restore(args.project, args.clone)
    elif args.action == "telegram":
        from . import telegram
        (telegram.setup if args.operation == "setup" else telegram.status)()
    elif args.action == "git-auth":
        from . import git_auth
        try:
            git_auth.configure(args.operation, args.project)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    elif args.action == "observer-check":
        from .board import EnvironmentActionRequired
        from .isolation import check_observer_mount
        try:
            check_observer_mount()
        except EnvironmentActionRequired as exc:
            raise SystemExit(str(exc)) from None
        print("Observer 읽기 전용 마운트와 Codex·Claude 훅 경로 확인 완료")
    elif args.action == "serve":
        from .worker import serve
        serve()
    elif args.action == "run-card":
        from .worker import process_card
        process_card(args.card_id, args.lock_fd, args.board)
    elif args.action == "knowledge":
        from .knowledge import collection_name
        print(collection_name(knowledge_root(project_dir(Path.cwd()))))
    elif args.action == "search-scope":
        from .knowledge import search_scope
        print("\n".join(search_scope(Path.cwd(), all_projects=args.all_projects)))
    elif args.action == "board":
        print(board_for_checkout(project_dir(Path.cwd())))
    elif args.action == "log":
        from .attempts import format_log
        from .board import board
        try:
            with board(args.board or board_for_checkout(project_dir(Path.cwd()))) as conn:
                print(format_log(conn, args.card, args.run, args.tail))
        except (ValueError, OSError, LookupError) as exc:
            raise SystemExit(str(exc)) from None
    else:
        _, card = enqueue(args.agent, args.prompt, Path.cwd(), args.session, parents=args.parent)
        print(card)


def project_tool_policy(tool_name="", args=None, session_id="", **kwargs):
    from .board import project_for_checkout
    from .knowledge import collection_name, scope_query
    try:
        cwd = bound_directory(session_id)
        if not cwd and get_session_env("HERMES_SESSION_PLATFORM", "") in ("", "cli", "local", "tui", "desktop"):
            cwd = resolve_agent_cwd()
        root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve()
        path = Path(cwd).resolve() if cwd else root
        owner = project_for_checkout(path) if path.is_relative_to(root) and path != root else None
        if owner and owner.readonly:
            if tool_name == "mcp__qmd__query":
                return {"action": "modify", "args": {"collections": [collection_name(owner.primary_path)]}}
            if tool_name in ("kanban_list", "kanban_show"):
                return {"action": "modify", "args": {"board": owner.board_slug}}
            if tool_name not in ("project_cli_submit", "clarify"):
                return {"action": "block", "message": "읽기 전용 프로젝트입니다. 소스 분석은 project_cli_submit으로 Codex 또는 Claude에 맡기세요."}
            return
        return scope_query(tool_name, args, session_id, cwd=cwd, **kwargs)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return {"action": "block", "message": f"프로젝트 작업 범위를 확인할 수 없습니다 · {exc}"}


def register(ctx):
    from . import telegram
    locales = json.loads(Path(__file__).with_name("desktop-locales.json").read_text())
    for language, messages in locales.items():
        ctx.register_locale(language, messages, surface="desktop")
    ctx.register_hook("pre_tool_call", project_tool_policy)
    ctx.register_approval_transport(telegram.TRANSPORT, telegram.present)
    ctx.register_hook("pre_approval_request", telegram.approval_started)
    ctx.register_hook("post_approval_response", telegram.approval_finished)
    ctx.register_hook("on_human_input_request", telegram.local_notice)
    ctx.register_hook("on_session_end", telegram.turn_finished)
    ctx.register_platform_handler("telegram", telegram.wire)
    for engine in ENGINES:
        def command(raw_args, *, session_id="", platform="", _engine=engine):
            return slash(_engine, raw_args, session_id, platform)
        ctx.register_command(engine, command, description=f"현재 프로젝트의 {engine} Kanban 작업 접수",
                             args_hint="<작업 지시>")
    ctx.register_cli_command("project-cli", "Manage project CLI workers", setup_cli, cli)
    ctx.register_tool(
        name="project_cli_submit", toolset="kanban", handler=submit_tool,
        schema={
            "name": "project_cli_submit",
            "description": "Queue a project task when the user asks Codex or Claude to do it (codex에게 / claude에게). Return the task ID.",
            "parameters": {"type": "object", "properties": {
                "agent": {"type": "string", "enum": list(ENGINES)},
                "prompt": {"type": "string"},
                "parents": {"type": "array", "items": {"type": "string"},
                            "description": "Upstream card IDs on this board for a subtask. Read-only subtasks inherit their Git snapshot."}},
                "required": ["agent", "prompt"]},
        },
    )
