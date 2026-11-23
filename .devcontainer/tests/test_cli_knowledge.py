"""Exercise completed cards, a failing embedding endpoint, and restart recovery offline."""

from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest

import fixture_transport

from test_cli_workers import FAKE_CLI


class Embeddings(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        texts = payload['input']
        if isinstance(texts, str):
            texts = [texts]
        if not self.server.recovered.is_set() and any('RETRY_INVOICE' in text for text in texts):
            self.server.saw_failure.set()
            self.send_error(404, 'model temporarily unavailable')
            return
        def vector(text):
            if 'RETRY_INVOICE' in text or 'billing backoff' in text:
                return [1, 0, 0, 0]
            if 'PICKUP_WINDOW' in text or 'parcel cutoff' in text:
                return [0, 1, 0, 0]
            return [0, 0, 1, 0]
        data = [{'index': i, 'object': 'embedding', 'embedding': vector(text)}
                for i, text in enumerate(texts)]
        body = json.dumps({'object': 'list', 'model': payload['model'], 'data': data}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class KnowledgeRecovery(unittest.TestCase):
    def wait_for(self, predicate, description, seconds=60):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(description)
            time.sleep(0.2)

    @unittest.skipUnless((Path.home() / '.local/share/hermes-qmd/bin/qmd').exists(), 'requires Dev Container QMD')
    def test_bounded_recall_and_revision_only_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            qmd = Path(tmp) / 'qmd'
            name = 'learned-fixture'
            notes = qmd / 'knowledge' / name
            notes.mkdir(parents=True)
            env = dict(os.environ, XDG_CACHE_HOME=tmp, QMD_CONFIG_DIR=str(qmd))
            env.pop('INDEX_PATH', None)
            config = qmd / 'index.yml'
            def configure(path):
                config.write_text(json.dumps({'collections': {name: {'path': str(path), 'pattern': '*.md'}}}))
            configure(notes)
            sha = 'b' * 40
            (notes / 'snapshot.md').write_text(f'# observer\n\n- 카드 · t_11111111 / 실행 1 / codex\n'
                                               f'- 원격 main · repo · {sha}')
            for i in range(5):
                (notes / f'run{i}.md').write_text(f'# fixture\n\n## 확인한 사실\n\nmatched_token {i}\n\n근거 · rules.py')
            binary = Path.home() / '.local/share/hermes-qmd/bin/qmd'
            subprocess.run([binary, '--index', 'index', 'update'], env=env, cwd=tmp, check=True, capture_output=True)
            script = Path(__file__).resolve().parents[1] / 'project-cli/index-knowledge.mjs'
            def search(query):
                return subprocess.run(['node', script, qmd, name, '--recall'], input=json.dumps(query),
                                      env=env, text=True, capture_output=True, timeout=8)
            refs = json.loads(search('matched_token').stdout)
            self.assertEqual(len({ref['document'] for ref in refs}), 3)
            self.assertTrue(all('rules.py' in ref['excerpt'] for ref in refs))
            snapshot, = json.loads(search('t_11111111').stdout)
            self.assertEqual(snapshot['document'], f'qmd://{name}/snapshot.md')
            self.assertIn(f'원격 main · repo · {sha}', snapshot['source'])
            self.assertEqual(snapshot['excerpt'], '')
            configure(Path(tmp))
            redirected = search('matched_token')
            self.assertNotEqual(redirected.returncode, 0)
            self.assertFalse(redirected.stdout.strip())

    @unittest.skipUnless((Path.home() / '.local/share/hermes-qmd/bin/qmd').exists(), 'requires Dev Container QMD')
    def test_indexing_is_scoped_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as resources:
            api = resources.enter_context(ThreadingHTTPServer(('127.0.0.1', 0), Embeddings))
            api.recovered = threading.Event()
            api.saw_failure = threading.Event()
            threading.Thread(target=api.serve_forever, daemon=True).start()
            resources.callback(api.shutdown)
            root = Path(tmp)
            home = root / 'hermes'
            (home / 'plugins').mkdir(parents=True)
            fixture_transport.install(home)
            (home / 'config.yaml').write_text('plugins:\n  enabled: [project-cli]\n')
            qmd = home / 'qmd'
            qmd.mkdir()
            (qmd / 'index.yml').write_text(
                f'collections: {{}}\nmodels:\n  embed: "http://127.0.0.1:{api.server_port}/v1#fixture"\n')
            bins = root / 'bin'
            bins.mkdir()
            for engine in ('claude', 'codex'):
                (bins / engine).write_text(FAKE_CLI)
                (bins / engine).chmod(0o755)
            projects = root / 'projects'
            env = dict(os.environ, HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
                       HERMES_DASHBOARD_FILES_ROOT=str(projects), PATH=f"{bins}:{os.environ['PATH']}")
            env.pop('HERMES_KANBAN_DB', None)

            def cmd(cwd, *args):
                return subprocess.check_output(['hermes', 'project-cli', *args], cwd=cwd, env=env, text=True).strip()

            def submit(project, engine, fact):
                subprocess.run(['git', 'init', '-q', str(project)], check=True)
                item = {'fact': fact, 'evidence': 'rules.py, RuleTest.test_contract passed'}
                (project / '.probe-result.json').write_text(json.dumps({
                    'summary': 'PRIVATE_SUMMARY', 'knowledge': [item, item, {'fact': 'guess', 'evidence': ''}]}))
                return cmd(project, 'submit', engine, 'PRIVATE_PROMPT')

            invoices, shipping = projects / 'invoices/app', projects / 'shipping/app'
            first = submit(invoices, 'codex', 'RETRY_INVOICE')
            second = submit(shipping, 'claude', 'PICKUP_WINDOW')
            db = home / 'kanban/boards/cli-workers/kanban.db'

            def count(sql, *args):
                with closing(sqlite3.connect(db)) as conn:
                    return conn.execute(sql, args).fetchone()[0]

            def receipts(card):
                return count("SELECT count(*) FROM task_events WHERE task_id=? AND kind='knowledge_indexed'", card)

            def serve():
                return subprocess.Popen(['hermes', 'project-cli', 'serve'], env=env)

            worker = serve()
            try:
                self.wait_for(lambda: api.saw_failure.is_set() and receipts(second) == 1, 'Other checkout did not index')
                self.assertEqual(count("SELECT status FROM tasks WHERE id=?", first), 'done')
                self.assertEqual(receipts(first), 0)
                first_dir = qmd / 'knowledge' / cmd(invoices, 'knowledge')
                second_dir = qmd / 'knowledge' / cmd(shipping, 'knowledge')
                self.assertNotEqual(first_dir, second_dir)
                note, = first_dir.glob('*.md')
                original_mtime = note.stat().st_mtime_ns
                self.assertEqual(note.read_text().count('RETRY_INVOICE'), 1)
                for text in ('PICKUP_WINDOW', 'PRIVATE_SUMMARY', 'PRIVATE_PROMPT', 'guess'):
                    self.assertNotIn(text, note.read_text())
                worker.terminate()
                worker.wait(timeout=15)
                api.saw_failure.clear()
                worker = serve()
                self.wait_for(api.saw_failure.is_set, 'Restart did not pick up the pending embedding')
                api.recovered.set()
                self.wait_for(lambda: receipts(first) == 1, 'Embedding did not retry after recovery', seconds=75)

                # Simulate losing the acknowledgement after QMD committed its vectors.
                worker.terminate()
                worker.wait(timeout=15)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='knowledge_indexed'", (first,))
                worker = serve()
                self.wait_for(lambda: receipts(first) == 1, 'Indexed note did not replay')
                self.assertEqual(list(first_dir.glob('*.md')), [note])
                self.assertEqual(note.stat().st_mtime_ns, original_mtime)
                with closing(sqlite3.connect(qmd / 'index.sqlite')) as index:
                    self.assertEqual(index.execute('SELECT count(*) FROM content_vectors').fetchone()[0], 2)

                def start_card(project, engine, prompt):
                    (project / '.probe-result.json').write_text(json.dumps({'knowledge': []}))
                    card = cmd(project, 'submit', engine, prompt)
                    self.wait_for(lambda: count('SELECT status FROM tasks WHERE id=?', card) == 'done', card)
                    meta = json.loads(count('SELECT metadata FROM task_runs WHERE task_id=?', card))
                    return (project / '.probe-prompt').read_text(), meta.get('knowledge_references', [])

                # New sessions, opposite engines, and two unrelated checkouts both named app.
                invoice_uri = f'qmd://{first_dir.name}/{note.name}'
                prompt, refs = start_card(invoices, 'claude', 'billing backoff')
                self.assertEqual(refs, [invoice_uri])
                self.assertIn('RETRY_INVOICE', prompt)
                self.assertIn('rules.py, RuleTest.test_contract passed', prompt)
                self.assertNotIn('PICKUP_WINDOW', prompt)
                prompt, refs = start_card(shipping, 'codex', 'parcel cutoff')
                self.assertEqual(refs, [f'qmd://{second_dir.name}/{next(second_dir.glob("*.md")).name}'])
                self.assertIn('PICKUP_WINDOW', prompt)
                self.assertNotIn('RETRY_INVOICE', prompt)
                prompt, refs = start_card(shipping, 'codex', 'billing backoff')
                self.assertEqual(refs, [])
                self.assertNotIn('Historical project references', prompt)

                api.saw_failure.clear()
                api.recovered.clear()
                prompt, refs = start_card(invoices, 'codex', 'Investigate RETRY_INVOICE')
                self.assertTrue(api.saw_failure.is_set())
                self.assertEqual(refs, [invoice_uri])
                self.assertIn('RuleTest.test_contract', prompt)
                # With the index configuration unavailable the requested work still runs.
                config = qmd / 'index.yml'
                paused = config.with_suffix('.paused')
                config.rename(paused)
                try:
                    prompt, refs = start_card(invoices, 'claude', 'RETRY_INVOICE')
                    self.assertEqual(refs, [])
                    self.assertNotIn('RuleTest.test_contract', prompt)
                finally:
                    paused.rename(config)
                    api.recovered.set()

                for status, expected in [('blocked', 'blocked'), ('completed', 'done')]:
                    report = {'status': status, 'knowledge': [] if status == 'completed' else [
                        {'fact': 'unverified', 'evidence': 'draft'}]}
                    (shipping / '.probe-result.json').write_text(json.dumps(report))
                    card = cmd(shipping, 'submit', 'claude', 'no publishable findings')
                    self.wait_for(lambda: count("SELECT status FROM tasks WHERE id=?", card) == expected, expected)
                    published = count("""SELECT count(*) FROM task_runs WHERE task_id=?
                                         AND json_array_length(metadata, '$.knowledge') > 0""", card)
                    self.assertEqual(published, 0)
                self.assertEqual(len(list(second_dir.glob('*.md'))), 1)
            finally:
                worker.terminate()
                worker.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
