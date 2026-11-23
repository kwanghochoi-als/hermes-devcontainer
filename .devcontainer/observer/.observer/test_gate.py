import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import gate
import snapshot_mcp


class ObserverTest(unittest.TestCase):
    def test_refresh_reads_new_remote_commit_without_touching_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, checkout, cache = (root / k for k in ('source', 'checkout', 'cache'))
            def git(*args):
                return subprocess.check_output(['git', *map(str, args)], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-b', 'main', origin)
            def publish(text):
                (origin / 'README').write_text(text)
                git('-C', origin, 'add', 'README')
                git('-C', origin, '-c', 'user.name=fixture', '-c', 'user.email=test@invalid', 'commit', '-m', text)
            publish('one'); git('clone', origin, checkout); publish('two')
            (checkout / 'README').write_text('developer WIP')
            rev = gate.refresh(checkout, cache)
            self.assertEqual(rev, git('-C', origin, 'rev-parse', 'HEAD'))
            self.assertEqual(git('--git-dir', cache, 'show', rev + ':README'), 'two')
            self.assertEqual((checkout / 'README').read_text(), 'developer WIP')
            receipt = root / 'receipt.json'
            with patch.object(gate,'CACHE',root/'mirrors'), patch.object(gate,'REPOS',{'pharmmate':checkout}), \
                    patch.dict(os.environ, PHARMMATE_OBSERVER_RECEIPT=str(receipt)):
                event={'session_id':'test','hook_event_name':'UserPromptSubmit'}
                with redirect_stdout(StringIO()): gate.hook(event)
                self.assertEqual(json.loads(receipt.read_text()), {'session_id':'test','repositories':{'pharmmate':rev}})
                token=gate.key(event).read_text()
                output = dict(event, hook_event_name='PreToolUse', tool_name='StructuredOutput',
                              tool_input=dict(status='completed', summary='done', tests='ok', knowledge=[]))
                with redirect_stdout(StringIO()) as printed: gate.hook(output)
                self.assertEqual(json.loads(printed.getvalue())['hookSpecificOutput']['permissionDecision'], 'allow')
                tool=dict(event,hook_event_name='PreToolUse',tool_name='Bash',tool_input={'command':gate.READER+' '+token+' show pharmmate README'})
                with redirect_stdout(StringIO()) as output: gate.hook(tool)
                self.assertEqual(json.loads(output.getvalue())['hookSpecificOutput']['permissionDecision'],'allow')
                args = {'snapshot': token, 'repository': 'pharmmate'}
                self.assertEqual(snapshot_mcp.read_for_run(args).splitlines(), ['README'])
                self.assertEqual(snapshot_mcp.read_for_run(dict(args, path='README')), 'two')
                call = dict(method='tools/call', jsonrpc='2.0', id=42)
                for invalid in (dict(args, snapshot='0' * 32), dict(args, repository='other'),
                                dict(args, path='../README'), dict(args, path='.env'), dict(args, extra='field')):
                    reply = snapshot_mcp.reply(dict(call, params={'name': 'read_snapshot', 'arguments': invalid}))
                    self.assertTrue(reply['result']['isError'])
                old_receipt = receipt.read_text()
                receipt.write_text(json.dumps({'session_id': 'other', 'repositories': {'pharmmate': rev}}))
                with self.assertRaises(OSError): snapshot_mcp.read_for_run(args)
                receipt.write_text(json.dumps({'session_id': 'test', 'repositories': {'pharmmate': '0' * 40}}))
                with self.assertRaises(ValueError): snapshot_mcp.read_for_run(args)
                receipt.write_text(old_receipt)
                with patch.dict(os.environ, PHARMMATE_OBSERVER_READER='mcp'):
                    with redirect_stdout(StringIO()) as out:
                        gate.hook(dict(tool, tool_name=gate.MCP_TOOL, tool_input=args))
                    self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'allow')
                    with self.assertRaises(ValueError): gate.hook(tool)
                origin.rename(root / 'offline')
                with self.assertRaises(subprocess.SubprocessError): gate.hook(event)
                with self.assertRaises(FileNotFoundError): gate.hook(tool)
                self.assertEqual(receipt.read_text(), '')
                with self.assertRaises(ValueError): snapshot_mcp.read_for_run(args)
            with self.assertRaises(subprocess.SubprocessError): gate.refresh(checkout, cache)

    def test_tool_denied_by_default(self):
        for tool in ['Edit', 'Write', 'Agent', 'apply_patch', 'mcp__git__push', 'Read']:
            self.assertFalse(gate.requested_read({'tool_name': tool, 'tool_input': {}}))
        for cmd in ['git push', 'touch x', gate.READER + ' list pharmmate; touch x']:
            self.assertFalse(gate.requested_read({'tool_name': 'Bash', 'tool_input': {'command': cmd}}))

    def test_probe_requires_its_own_fresh_snapshot_and_blocks_the_read(self):
        challenge = 'tool-' + '1' * 32
        session = {'session_id': 'probe-session', 'hook_event_name': 'UserPromptSubmit'}
        commit = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proof, receipt = root / challenge, root / 'business-receipt'
            with patch.object(gate, 'CACHE', root / 'cache'), patch.object(gate, 'REPOS', {'pharmmate': 'origin'}), \
                    patch.object(gate, 'refresh', return_value=commit) as fetch, \
                    patch.dict(os.environ, PHARMMATE_OBSERVER_PROBE=challenge, PHARMMATE_OBSERVER_PROBE_DIR=tmp,
                               PHARMMATE_OBSERVER_RECEIPT=str(receipt)):
                with redirect_stdout(StringIO()) as output:
                    gate.hook(session)
                context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                token = context.split('PHARMMATE_OBSERVER_OK ')[1].split('.')[0]
                command = f'{gate.READER} {token} list pharmmate'
                self.assertIn(command, context)
                fetch.assert_called_once()
                self.assertEqual(json.loads((gate.CACHE / token).read_text()), {'pharmmate': commit})
                self.assertFalse(receipt.exists())

                def call(cmd, sid='probe-session'):
                    with redirect_stdout(StringIO()) as response:
                        gate.hook(dict(session, session_id=sid, hook_event_name='PreToolUse',
                                       tool_name='Bash', tool_input={'command': cmd}))
                    self.assertEqual(json.loads(response.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')

                call('pwd')
                call(command, sid='another-session')
                call(command.replace(token, '0' * 32))
                call(command.replace('list pharmmate', 'show pharmmate README'))
                with patch.dict(os.environ, PHARMMATE_OBSERVER_PROBE='tool-' + '2' * 32):
                    call(command)
                    self.assertFalse((root / ('tool-' + '2' * 32)).exists())
                self.assertFalse(proof.exists())
                call(command)
                self.assertEqual(proof.read_text(), 'PreToolUse')

                mcp_challenge = 'tool-' + '3' * 32
                with patch.dict(os.environ, PHARMMATE_OBSERVER_READER='mcp', PHARMMATE_OBSERVER_PROBE=mcp_challenge):
                    with redirect_stdout(StringIO()) as output:
                        gate.hook(session)
                    context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
                    self.assertIn(gate.MCP_TOOL, context)
                    mcp_token = context.split('PHARMMATE_OBSERVER_OK ')[1].split('.')[0]
                    call(f'{gate.READER} {mcp_token} list pharmmate')
                    mcp_proof = root / mcp_challenge
                    for current in (token, mcp_token):
                        with redirect_stdout(StringIO()) as response:
                            gate.hook(dict(session, hook_event_name='PreToolUse', tool_name=gate.MCP_TOOL,
                                           tool_input={'snapshot': current, 'repository': 'pharmmate'}))
                        self.assertEqual(json.loads(response.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')
                        self.assertEqual(mcp_proof.is_file(), current == mcp_token)
                    self.assertFalse(receipt.exists())

    def test_worker_reports_the_missing_hook_without_sending_the_request(self):
        script = Path(__file__).with_name('start.py')

        def cli(args, **kwargs):
            challenge = kwargs['env'].get('PHARMMATE_OBSERVER_PROBE')
            self.assertNotIn('PHARMMATE_OBSERVER_RECEIPT', kwargs['env'])
            if challenge and not challenge.startswith('tool-'):
                (Path(kwargs['env']['PHARMMATE_OBSERVER_PROBE_DIR']) / challenge).write_text('UserPromptSubmit')
            return subprocess.CompletedProcess(args, 0)

        with patch.dict(os.environ, PHARMMATE_OBSERVER_RECEIPT='business'), patch('os.chdir'), \
                patch.object(sys, 'argv', [str(script), 'codex', '--worker', 'schema']), \
                patch('subprocess.run', side_effect=cli), patch('os.execvp') as submit, \
                redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()), self.assertRaises(SystemExit) as exit_:
            runpy.run_path(str(script), run_name='__main__')
        self.assertNotEqual(exit_.exception.code, 0)
        *steps, event = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([s["step"] for s in steps], ["snapshot", "prompt_hook"])
        self.assertEqual(event['type'], 'turn.failed')
        self.assertIn('PreToolUse', event['error']['message'])
        submit.assert_not_called()


if __name__ == '__main__': unittest.main()
