"""Exercise the native Hermes approval host, Telegram SDK and project workers in an isolated profile."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import re
import runpy
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs


class OperatorTelegram(unittest.TestCase):
    def test_operator_dm(self):
        root = Path.home() / ".hermes/hermes-agent"
        sys.path.insert(0, str(root))
        try:
            from hermes_cli._launchers import runtime_command
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "plugins").mkdir(parents=True)
            (home / "plugins/project-cli").symlink_to(Path(__file__).resolve().parents[1] / "project-cli")
            projects = Path(tmp) / "projects"
            projects.mkdir()
            env = dict(os.environ, HERMES_KANBAN_HOME=str(home),
                       HERMES_DASHBOARD_FILES_ROOT=str(projects))
            env.pop("HERMES_HOME", None)
            env.pop("HERMES_KANBAN_DB", None)
            for key in list(env):
                if key.startswith("TELEGRAM_"):
                    del env[key]
            # Activate the installed SDK, then select only the fixture home before loading application modules.
            code = (f"import os, runpy; os.environ['HERMES_HOME'] = {str(home)!r}; "
                    f"from hermes_constants import get_hermes_home; assert str(get_hermes_home()) == {str(home)!r}; "
                    f"runpy.run_path({str(Path(__file__).resolve())!r})['exercise']()")
            subprocess.run(runtime_command(root, code=code), cwd=projects, env=env, check=True, timeout=100)


OWNER = "8200001"
CHAT = {"id": int(OWNER), "type": "private", "first_name": "Fixture", "accent_color_id": 1,
        "max_reaction_count": 1, "accepted_gift_types": dict.fromkeys(
            ("unlimited_gifts", "limited_gifts", "unique_gifts", "premium_subscription", "gifts_from_channels"), False)}


class BotAPI(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        method = self.path.rsplit("/", 1)[-1].lower()
        data = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if method == "getme":
            result = {"id": 8300002, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"}
        elif method == "getchat":
            result = CHAT
        elif method == "answercallbackquery":
            self.server.answers.put(data.get("text", [""])[0])
            result = True
        elif method == "editmessagetext":
            result = True
        elif method == "sendmessage":
            assert data["chat_id"] == [OWNER]
            if self.server.refuse:
                body = b'{"ok":false,"error_code":503,"description":"temporary fixture outage"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.server.messages.put(data["text"][0])
            if "reply_markup" in data:
                self.server.keyboards.put(json.loads(data["reply_markup"][0]))
            result = {"message_id": 10, "date": 1, "chat": CHAT, "text": data["text"][0]}
        else:
            self.send_error(404)
            return
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def exercise():
    from gateway.config import Platform
    from gateway.session import SessionSource
    from hermes_cli import kanban_db as kb
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from hermes_cli.workspace_projects import telegram as tg, telegram_kanban as reports
    from hermes_cli.workspace_projects.board import BOARD, board, enqueue
    from tools.approval_context import set_current_observability_context, reset_current_observability_context
    from tools.approval_prompt import _present_with_selected_transport
    from tools.clarify_tool import clarify_tool
    from telegram import Update
    from telegram.ext import Application

    tests = unittest.TestCase()
    home = Path(os.environ["HERMES_HOME"])
    project = Path.cwd() / "operator-test"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    # Data created before linking must not be sent later.
    old_slug, old_id = enqueue("codex", "old card", project)
    with board(old_slug) as conn:
        kb.block_task(conn, old_id, kind="needs_input", reason="old")

    source = SessionSource(platform=Platform.TELEGRAM, chat_id=OWNER, user_id=OWNER)

    def dispatch(name, args):
        return tg.command(name, args, channel)

    async def receive(data, src=source, callback=True):
        # Real PTB wire decoding and handler selection, with the gateway's auth callback as a seam.
        bot = tg.bot_client(channel.token)
        async with bot:
            app = Application.builder().bot(bot).updater(None).build()
            adapter = Mock()
            adapter._is_callback_user_authorized.return_value = True
            adapter.gateway_runner._check_slash_access.return_value = None
            with patch("threading.Thread.start"):
                wire(app, adapter)
            user = {"id": int(src.user_id), "is_bot": src.is_bot, "first_name": "Fixture"}
            chat = dict(CHAT, id=int(src.chat_id), type="private" if src.chat_type == "dm" else "group")
            message = {"message_id": 20, "date": 1, "chat": chat, "from": user, "text": "Fixture approval"}
            if callback:
                payload = {"callback_query": {"id": "cb1", "chat_instance": "1", "from": user,
                                               "data": data, "message": message}}
            else:
                message.update(text=data, entities=[{"type": "bot_command", "offset": 0,
                                                      "length": len(data.split()[0])}])
                payload = {"message": message}
            update = Update.de_json(dict(payload, update_id=1), bot)
            handlers = [h for h in app.handlers[0] if h.check_update(update)]
            tests.assertEqual(len(handlers), 1)
            await handlers[0].callback(update, SimpleNamespace(bot=bot))

    def with_native_approval(turn=""):
        tokens = set_current_observability_context(turn_id=turn, session_id="operator-session")
        try:
            return _present_with_selected_transport(command="printf fixture", description="Fixture approval",
                pattern_key="fixture", pattern_keys=["fixture"], session_key="fixture/session", surface="cli",
                allow_session=True, allow_permanent=True)
        finally:
            reset_current_observability_context(tokens)

    with ThreadingHTTPServer(("127.0.0.1", 0), BotAPI) as api, ThreadPoolExecutor(max_workers=3) as pool:
        api.messages, api.answers, api.keyboards, api.refuse = Queue(), Queue(), Queue(), False
        threading.Thread(target=api.serve_forever, daemon=True).start()
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: [project-cli]\napprovals:\n  timeout: 6\n"
            f"platforms:\n  telegram:\n    extra:\n      base_url: http://127.0.0.1:{api.server_port}\n")
        tests.assertEqual(asyncio.run(tg.verify_dm("8300002:TelegramFixtureTokenOnly", OWNER)), "8300002")
        # Local setup uses the real SDK/getChat; only terminal prompts and the production service restart are replaced.
        with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=OWNER), \
                patch("getpass.getpass", return_value="8300002:TelegramFixtureTokenOnly"), \
                patch.object(tg.subprocess, "run", wraps=subprocess.run) as run:
            existing_run = run._mock_wraps
            run.side_effect = lambda argv, **kw: (subprocess.CompletedProcess(argv, 0)
                if str(argv[-1]) == "restart-gateway" else existing_run(argv, **kw))
            tg.setup()
        channel = tg.route()
        tests.assertTrue(channel and tg.selected())
        discover_plugins(force=True)
        manager = get_plugin_manager()
        tests.assertIsNotNone(manager.get_approval_transport(tg.TRANSPORT))
        wire = next(factory for factory, plugin in manager.get_platform_handler_factories("telegram")
                    if plugin == "project-cli")

        for decision, expected in [("ok", "once"), ("no", "deny")]:
            pending = pool.submit(with_native_approval, decision)
            message = api.messages.get(timeout=10)
            tests.assertIn("printf fixture", message)
            ref = re.search(r"/workok (a[0-9a-f]+)", message)[1]
            payload = api.keyboards.get(timeout=2)["inline_keyboard"][0][decision == "no"]["callback_data"]
            tests.assertEqual(payload, f"hwork:{decision}:{ref}")
            for foreign in (replace(source, user_id="99"), replace(source, chat_type="group"),
                            replace(source, chat_id="99"), replace(source, is_bot=True)):
                asyncio.run(receive(payload, foreign))
                tests.assertIn("개인 대화", api.answers.get(timeout=2))
            tests.assertFalse(pending.done())
            asyncio.run(receive(payload))
            tests.assertNotEqual(api.answers.get(timeout=2), tg.STALE)
            tests.assertEqual(pending.result(timeout=8)["choice"], expected)
            tests.assertEqual(dispatch("ok", ref), tg.STALE)
            invoke_hook("on_session_end", session_id="operator-session", turn_id=decision, completed=True)
            tg.deliver_turns(channel)
            tests.assertIn("Hermes 응답 완료", api.messages.get(timeout=5))
            tg.deliver_turns(channel)
            tests.assertTrue(api.messages.empty())

        pending = pool.submit(with_native_approval)
        expired = re.search(r"/workok (a[0-9a-f]+)", api.messages.get(timeout=5))[1]
        tests.assertEqual(pending.result(timeout=9)["choice"], "deny")
        tests.assertEqual(dispatch("ok", expired), tg.STALE)

        pending = pool.submit(clarify_tool, [{"question": "Choose", "choices": ["one", "two"], "multi_select": True},
                                             {"question": "Which file?"}], lambda _: tests.fail("local prompt"))
        refs = [re.search(r"/workreply (q[0-9a-f]+)", api.messages.get(timeout=10))[1] for _ in range(2)]
        asyncio.run(receive(f"/workreply {refs[1]} notes.md", callback=False))
        tests.assertIn("전달", api.messages.get(timeout=3))
        dispatch("reply", refs[0] + " 2,1")
        result = json.loads(pending.result(timeout=6))
        tests.assertEqual([r["user_response"] for r in result["responses"]], [["two", "one"], "notes.md"])

        with board(BOARD) as conn:
            reports.drain(channel, BOARD, conn)
            tests.assertTrue(api.messages.empty())
            slug, card = enqueue("claude", "Confirm the output file", project, "operator-session")
            before = kb.claim_task(conn, card, expect_assignee="cli:claude")
            kb.block_task(conn, card, kind="needs_input", reason="Which file?", expected_run_id=before.current_run_id)
            api.refuse = True
            with tests.assertRaises(Exception):
                reports.drain(channel, slug, conn)
            api.refuse = False
            reports.drain(channel, slug, conn)
            msg = api.messages.get(timeout=5)
            tests.assertIn(card, msg)
            ref = re.search(r"/workok (k[0-9a-f]+)", msg)[1]
            tests.assertIn("보류", dispatch("no", ref))
            tests.assertEqual(kb.get_task(conn, card).status, "blocked")
            tests.assertEqual(dispatch("ok", ref), tg.STALE)
            # A local unblock/reblock fences a delivered request even with the same card ID.
            kb.unblock_task(conn, card)
            kb.block_task(conn, card, kind="needs_input", reason="Which file now?")
            reports.drain(channel, slug, conn)
            obsolete = re.search(r"/workok (k[0-9a-f]+)", api.messages.get(timeout=5))[1]
            tests.assertEqual(kb.get_task(conn, card).status, "triage")
            kb.specify_triage_task(conn, card)
            kb.claim_task(conn, card, expect_assignee="cli:claude")
            kb.block_task(conn, card, kind="needs_input", reason="Choose final file")
            tests.assertEqual(dispatch("ok", obsolete), tg.STALE)
            reports.drain(channel, slug, conn)
            ref = re.search(r"/workok (k[0-9a-f]+)", api.messages.get(timeout=5))[1]
            replies = [pool.submit(dispatch, "reply", ref + " verified.md") for _ in range(2)]
            tests.assertEqual(sum(f.result() != tg.STALE for f in replies), 1)
            tests.assertEqual(kb.get_task(conn, card).status, "ready")
            from hermes_cli.workspace_projects.worker import task_context
            tests.assertIn("verified.md", task_context(conn, kb.get_task(conn, card)))
            kb.claim_task(conn, card, expect_assignee="cli:claude")
            kb.complete_task(conn, card, result="Done", summary="Done\n테스트 · fixture passed")
            reports.drain(channel, slug, conn)
            tests.assertIn("fixture passed", api.messages.get(timeout=5))
            reports.drain(channel, slug, conn)
            tests.assertTrue(api.messages.empty())

            # Run the unmodified worker entry point with fixture CLIs. Check that each engine
            # sees the operator comment and resumes the very same native CLI session.
            from hermes_cli.workspace_projects.worker import digest, process_card
            bins = home / "fixture-bin"
            bins.mkdir()
            fake_cli = runpy.run_path(str(Path(__file__).with_name("test_cli_workers.py")))["FAKE_CLI"]
            state = home / "cli-workers"
            for directory in ("sessions", "locks"):
                (state / directory).mkdir(exist_ok=True)

            def execute_card():
                fd = os.open(state / "locks" / digest(str(project)), os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                process_card(card, fd, slug)

            with patch.dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}"):
                # The connected gateway owns this independent notifier, including when both CLIs are busy.
                wire(Application.builder().bot(tg.bot_client(channel.token)).updater(None).build(), Mock())
                for engine in ("codex", "claude"):
                    binary = bins / engine
                    binary.write_text(fake_cli)
                    binary.chmod(0o755)
                    slug, card = enqueue(engine, "Choose output", project, "same-project-session")
                    fixture = project / ".probe-result.json"
                    fixture.write_text(json.dumps({"status": "blocked", "summary": "Use verified.md?"}))
                    execute_card()
                    tests.assertEqual(kb.get_task(conn, card).status, "blocked")
                    ref = re.search(r"/workok (k[0-9a-f]+)", api.messages.get(timeout=12))[1]
                    if engine == "codex":
                        dispatch("ok", ref)
                    else:
                        dispatch("reply", ref + " verified.md")
                    fixture.unlink()
                    execute_card()
                    tests.assertEqual(kb.get_task(conn, card).status, "done")
                    sessions = [c.body.split()[2] for c in kb.list_comments(conn, card) if c.author == "project-cli"]
                    tests.assertEqual(len(sessions), 2)
                    tests.assertEqual(*sessions)
                    tests.assertIn("Telegram 운영자", (project / ".probe-prompt").read_text())
                    tests.assertIn("테스트 · fixture", api.messages.get(timeout=12))
        api.shutdown()
