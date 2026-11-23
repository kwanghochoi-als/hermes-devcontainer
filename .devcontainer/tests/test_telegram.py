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
import time
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
            if "reply_markup" in data:
                markup = json.loads(data["reply_markup"][0])
                self.server.keyboards.put(markup)
                self.server.buttons[data["text"][0]] = {b["text"]: b["callback_data"]
                                                       for row in markup["inline_keyboard"] for b in row}
            self.server.messages.put(data["text"][0])
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
        api.buttons = {}
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
            tests.assertTrue(message.startswith("승인 요청"))
            tests.assertIn("printf fixture", message)
            ref = re.search(r"/workok (a[0-9a-f]+)", message)[1]
            payload = api.keyboards.get(timeout=2)["inline_keyboard"][0][decision == "no"]["callback_data"]
            tests.assertEqual(payload, f"hwork:{decision}:{ref}")
            tests.assertEqual(dispatch("fixed", ref), tg.ACTION_HINT["approval"])
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
        questions = [api.messages.get(timeout=10) for _ in range(2)]
        tests.assertTrue(all(q.startswith("자료 요청") and set(api.buttons[q]) == {"보류"} for q in questions))
        refs = [re.search(r"/workreply (q[0-9a-f]+)", q)[1] for q in questions]
        tests.assertEqual(dispatch("fixed", refs[1]), tg.ACTION_HINT["materials"])
        asyncio.run(receive(f"/workreply {refs[1]} notes.md", callback=False))
        tests.assertIn("전달", api.messages.get(timeout=3))
        dispatch("reply", refs[0] + " 2,1")
        result = json.loads(pending.result(timeout=6))
        tests.assertEqual([r["user_response"] for r in result["responses"]], [["two", "one"], "notes.md"])

        with board(BOARD) as conn:
            reports.drain(channel, BOARD, conn)
            tests.assertTrue(api.messages.empty())
            tests.assertIn(f"/workrequest {BOARD} {old_id}", dispatch("status", BOARD))
            tests.assertIn(old_id, dispatch("request", f"{BOARD} {old_id}"))
            recovered = re.search(r"/workreply (k[0-9a-f]+)", api.messages.get(timeout=5))[1]
            tests.assertEqual(dispatch("ok", recovered), tg.ACTION_HINT["materials"])
            tests.assertIn("보류", dispatch("no", recovered))
            tests.assertEqual(dispatch("request", f"{BOARD} {old_id}"), tg.STALE)
            tests.assertNotIn(f"/workrequest {BOARD} {old_id}", dispatch("status", BOARD))
            slug, card = enqueue("claude", "Confirm the output file", project, "operator-session")
            before = kb.claim_task(conn, card, expect_assignee="cli:claude")
            from hermes_cli.kanban_db_dispatch import heartbeat_worker
            heartbeat_worker(conn, card, expected_run_id=before.current_run_id)
            tests.assertIn("첫 출력 대기", dispatch("status", BOARD))
            output = home / "cli-workers/runs" / card / str(before.current_run_id) / "events.jsonl"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("{}\n")
            old = time.time() - 125
            os.utime(output, (old, old))
            summary = dispatch("status", BOARD)
            tests.assertIn("마지막 출력 2분 전", summary)
            tests.assertIn("워커 응답", summary)
            kb.block_task(conn, card, kind="needs_input", request_kind="materials", reason="Which file?",
                          expected_run_id=before.current_run_id)
            api.refuse = True
            with tests.assertRaises(Exception):
                reports.drain(channel, slug, conn)
            api.refuse = False
            reports.drain(channel, slug, conn)
            msg = api.messages.get(timeout=5)
            tests.assertIn(card, msg)
            tests.assertTrue(msg.startswith("자료 요청"))
            tests.assertNotIn("/workok", msg)
            tests.assertEqual(set(api.buttons[msg]), {"보류"})
            ref = re.search(r"/workreply (k[0-9a-f]+)", msg)[1]
            tests.assertEqual(dispatch("fixed", ref), tg.ACTION_HINT["materials"])
            tests.assertIn("<답변>", dispatch("reply", ref))
            # Even the same card ID on another board has its own request and permitted action.
            peer = "peer-project"
            kb.create_board(peer, name="운영 프로젝트", project_id="fixture-peer")
            with kb.scoped_current_board(peer), board(peer) as other:
                with patch.object(kb, "_new_task_id", return_value=card):
                    kb.create_task(other, board=peer, title="Worker cannot reach Docker", assignee="cli:codex")
                kb.block_task(other, card, kind="capability", request_kind="environment", reason="Check docker info")
                reports.drain(channel, peer, other)
                notice = api.messages.get(timeout=5)
                tests.assertIn("환경 조치 요청 · 운영 프로젝트", notice)
                tests.assertEqual(set(api.buttons[notice]), {"조치 완료 · 재시도", "보류"})
                env_ref = re.search(r"/workfixed (k[0-9a-f]+)", notice)[1]
                tests.assertNotEqual(ref, env_ref)
                tests.assertNotIn("/workok", notice)
                tests.assertIn("환경 조치 대기", dispatch("status", peer))
                tests.assertIn("자료 대기", dispatch("status", slug))
                tests.assertIn("환경 조치 요청", dispatch("request", f"{peer} {card}"))
                tests.assertIn(env_ref, api.messages.get(timeout=5))
                asyncio.run(receive(f"hwork:ok:{env_ref}"))
                tests.assertEqual(api.answers.get(timeout=2), tg.ACTION_HINT["environment"])
                tests.assertEqual(dispatch("reply", env_ref + " yes"), tg.ACTION_HINT["environment"])
                tests.assertEqual(kb.get_task(other, card).status, "blocked")
                asyncio.run(receive(api.buttons[notice]["조치 완료 · 재시도"]))
                tests.assertIn("재시도", api.answers.get(timeout=2))
                tests.assertEqual(kb.get_task(other, card).status, "ready")
                tests.assertEqual(kb.get_task(conn, card).status, "blocked")
                tests.assertEqual(dispatch("fixed", env_ref), tg.STALE)
            tests.assertIn("보류", dispatch("no", ref))
            tests.assertEqual(kb.get_task(conn, card).status, "blocked")
            tests.assertEqual(dispatch("ok", ref), tg.STALE)
            # A local unblock/reblock fences a delivered request even with the same card ID.
            kb.unblock_task(conn, card)
            kb.block_task(conn, card, kind="needs_input", reason="Which file now?")
            reports.drain(channel, slug, conn)
            obsolete = re.search(r"/workreply (k[0-9a-f]+)", api.messages.get(timeout=5))[1]
            tests.assertEqual(kb.get_task(conn, card).status, "triage")
            kb.specify_triage_task(conn, card)
            kb.claim_task(conn, card, expect_assignee="cli:claude")
            kb.block_task(conn, card, kind="needs_input", reason="Choose final file")
            tests.assertEqual(dispatch("ok", obsolete), tg.STALE)
            reports.drain(channel, slug, conn)
            ref = re.search(r"/workreply (k[0-9a-f]+)", api.messages.get(timeout=5))[1]
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
                return json.loads((project / ".probe-args.json").read_text())

            import importlib.util
            spec = importlib.util.spec_from_file_location("fixture_transport", Path(__file__).with_name("fixture_transport.py"))
            fixture_transport = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fixture_transport)
            with patch("hermes_cli.workspace_projects.worker.isolation", fixture_transport), patch.dict(os.environ, PATH=f"{bins}:{os.environ['PATH']}"):
                # The connected gateway owns this independent notifier, including when both CLIs are busy.
                wire(Application.builder().bot(tg.bot_client(channel.token)).updater(None).build(), Mock())
                for engine in ("codex", "claude"):
                    binary = bins / engine
                    binary.write_text(fake_cli)
                    binary.chmod(0o755)
                    slug, card = enqueue(engine, "Prepare the release", project, "same-project-session")
                    fixture = project / ".probe-result.json"
                    proposal = "Deploy the prepared release to production?"
                    fixture.write_text(json.dumps({"status": "blocked", "request_kind": "approval", "summary": proposal}))
                    first_args = execute_card()
                    task = kb.get_task(conn, card)
                    tests.assertEqual((task.status, task.block_kind), ("blocked", "needs_input"))
                    notice = api.messages.get(timeout=12)
                    tests.assertTrue(notice.startswith("승인 요청"))
                    tests.assertEqual(set(api.buttons[notice]), {"승인", "거절"})
                    tests.assertIn("승인 대기", dispatch("status", BOARD))
                    ref = re.search(r"/workok (k[0-9a-f]+)", notice)[1]
                    if engine == "codex":
                        dispatch("ok", ref)
                    else:
                        dispatch("reply", ref + " Use staging only")
                    fixture.unlink()
                    resumed_args = execute_card()
                    bypass = ("--dangerously-bypass-approvals-and-sandbox" if engine == "codex"
                              else "--dangerously-skip-permissions")
                    for args in (first_args, resumed_args):
                        tests.assertEqual(args.count(bypass), 1)
                        tests.assertNotIn("--sandbox", args)
                        tests.assertNotIn("--permission-mode", args)
                    tests.assertEqual(kb.get_task(conn, card).status, "done")
                    sessions = [c.body.split()[2] for c in kb.list_comments(conn, card) if c.author == "project-cli"]
                    tests.assertEqual(len(sessions), 2)
                    tests.assertEqual(*sessions)
                    resumed_prompt = (project / ".probe-prompt").read_text()
                    tests.assertIn("결정 대상 · " + proposal, resumed_prompt)
                    if engine == "claude":
                        tests.assertIn("Use staging only", resumed_prompt)
                    tests.assertIn("테스트 · fixture", api.messages.get(timeout=12))

                for engine, kind, action, reason in [
                    ("claude", "materials", "reply", "Which fixture should I use?"),
                    ("codex", "environment", "fixed", "Restore the host service and check its status"),
                ]:
                    slug, card = enqueue(engine, "Run the integration check", project)
                    fixture.write_text(json.dumps({"status": "blocked", "request_kind": kind, "summary": reason}))
                    execute_card()
                    notice = api.messages.get(timeout=12)
                    ref = re.search(rf"/work{action} (k[0-9a-f]+)", notice)[1]
                    tests.assertEqual(dispatch("ok", ref), tg.ACTION_HINT[kind])
                    tests.assertEqual(reports.pending_event(conn, card).payload["request_kind"], kind)
                    command = f"/work{action} {ref}" + (" fixtures/orders.csv" if action == "reply" else "")
                    asyncio.run(receive(command, callback=False))
                    tests.assertIn(card, api.messages.get(timeout=5))
                    tests.assertEqual(kb.get_task(conn, card).status, "ready")
                    audit = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='telegram_decided'",
                                         (card,)).fetchone()
                    tests.assertEqual(json.loads(audit[0])["request_kind"], kind)
                    fixture.unlink()
                    execute_card()
                    resumed_prompt = (project / ".probe-prompt").read_text()
                    tests.assertIn(reason, resumed_prompt)
                    tests.assertIn("fixtures/orders.csv" if action == "reply" else "환경 조치를 완료", resumed_prompt)
                    tests.assertIn("테스트 · fixture", api.messages.get(timeout=12))

                # The real Docker launcher can require an environment fix before either CLI emits a report.
                from hermes_cli.workspace_projects import isolation
                docker = bins / "docker"
                docker.write_text("#!/bin/sh\necho 'fixture daemon unavailable' >&2\nexit 1\n")
                docker.chmod(0o755)
                slug, card = enqueue("codex", "Inspect the checkout", project)
                with patch("hermes_cli.workspace_projects.worker.isolation", isolation):
                    execute_card()
                notice = api.messages.get(timeout=12)
                tests.assertTrue(notice.startswith("환경 조치 요청"))
                tests.assertIn("docker info", notice)
                env_ref = re.search(r"/workfixed (k[0-9a-f]+)", notice)[1]
                tests.assertIn("보류", dispatch("no", env_ref))
                tests.assertEqual(kb.get_task(conn, card).status, "blocked")
                tests.assertEqual(dispatch("fixed", env_ref), tg.STALE)

                slug, card = enqueue("claude", "Check the build", project)
                for status, title in [("failed", "작업 실행 오류"), ("invalid-report", "반복 실행 오류")]:
                    fixture.write_text(json.dumps({"status": status, "summary": "Dependency unavailable"}))
                    execute_card()
                    tests.assertEqual(kb.get_task(conn, card).block_kind, "capability")
                    notice = api.messages.get(timeout=12)
                    tests.assertIn(title, notice)
                    tests.assertNotIn("/workok", notice)
                    with tg.store(channel.path) as requests:
                        tests.assertEqual(requests.execute("SELECT count(*) FROM requests WHERE board=? AND card=?",
                                                           (slug, card)).fetchone()[0], 0)
                    if status == "failed":
                        tests.assertIn("Dependency unavailable", notice)
                        kb.unblock_task(conn, card)
        api.shutdown()
