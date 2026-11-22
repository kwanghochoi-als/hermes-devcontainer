import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import gate


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
                origin.rename(root / 'offline')
                with self.assertRaises(subprocess.SubprocessError): gate.hook(event)
                with self.assertRaises(FileNotFoundError): gate.hook(tool)
            with self.assertRaises(subprocess.SubprocessError): gate.refresh(checkout, cache)

    def test_tool_denied_by_default(self):
        for tool in ['Edit', 'Write', 'Agent', 'apply_patch', 'mcp__git__push', 'Read']:
            self.assertFalse(gate.allowed({'tool_name': tool, 'tool_input': {}}))
        for cmd in ['git push', 'touch x', gate.READER + ' list pharmmate; touch x']:
            self.assertFalse(gate.allowed({'tool_name': 'Bash', 'tool_input': {'command': cmd}}))


if __name__ == '__main__': unittest.main()
