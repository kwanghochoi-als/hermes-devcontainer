"""Hermes entry points for the two project CLI workers."""

import json
from pathlib import Path

from agent.runtime_cwd import resolve_agent_cwd, scoped_session_cwd
from gateway.session_context import get_session_env

from .board import BOARD, ENGINES, board_for_checkout, enqueue, knowledge_root, project_dir


def session_directory(platform):
    if path := scoped_session_cwd():
        return path
    if platform in ("", "cli", "local", "tui", "desktop"):
        return resolve_agent_cwd()
    raise ValueError("이 채널에 프로젝트 폴더가 연결되어 있지 않습니다. 원래 프로젝트 세션에서 접수하세요.")


def slash(engine, prompt, session_id, platform):
    if not prompt.strip():
        return f"/{engine} <작업 지시>"
    try:
        slug, card = enqueue(engine, prompt, session_directory(platform), session_id)
    except (OSError, ValueError) as exc:
        return str(exc)
    return f"{engine} 작업 {card} 접수 · hermes kanban --board {slug} show {card}"


def submit_tool(args, *, session_id=None, tool_call_id=None, **kwargs):
    try:
        cwd = session_directory(get_session_env("HERMES_SESSION_PLATFORM", ""))
        slug, card = enqueue(args["agent"], args["prompt"], cwd, session_id,
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
    commands.add_parser("board", help="Print the current project's Kanban board")
    commands.add_parser("knowledge", help="Print the QMD collection for findings in the current project")
    commands.add_parser("serve", help="Run the Kanban CLI worker service")
    run = commands.add_parser("run-card", help="Internal supervised worker for one claimed checkout")
    run.add_argument("card_id")
    run.add_argument("lock_fd", type=int)
    run.add_argument("--board", default=BOARD)


def cli(args):
    if args.action == "serve":
        from .worker import serve
        serve()
    elif args.action == "run-card":
        from .worker import process_card
        process_card(args.card_id, args.lock_fd, args.board)
    elif args.action == "knowledge":
        from .knowledge import collection_name
        print(collection_name(knowledge_root(project_dir(Path.cwd()))))
    elif args.action == "board":
        print(board_for_checkout(project_dir(Path.cwd())))
    else:
        _, card = enqueue(args.agent, args.prompt, Path.cwd(), args.session)
        print(card)


def register(ctx):
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
                "prompt": {"type": "string"}},
                "required": ["agent", "prompt"]},
        },
    )
