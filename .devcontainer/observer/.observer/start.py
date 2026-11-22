"""Trusted one-shot entry point. Never pass the user's request to an unverified CLI."""
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).absolute().parent.parent
worker = len(sys.argv)==4 and sys.argv[2]=='--worker'
if (not worker and len(sys.argv) != 3) or sys.argv[1] not in ('claude','codex'):
    sys.exit('Usage: python3 -I -B .observer/start.py codex|claude "question"')
os.chdir(ROOT)
agent = sys.argv[1]
args = (['codex','exec','--strict-config','--skip-git-repo-check','--ephemeral','--sandbox','read-only','-c','approval_policy="never"'] if agent=='codex' else ['claude','-p','--settings',str(ROOT/'.claude/settings.json'),'--strict-mcp-config','--permission-mode','dontAsk','--tools','Bash','--no-session-persistence'])
# Fail before even launching a probe if the upstream cannot be verified. The live prompt
# rechecks freshness and pins its own token (also protects resumed/follow-up turns).
import json
gate = ROOT/'.observer/gate.py'
env = dict(os.environ); env.pop('PHARMMATE_OBSERVER_PROBE',None)
env.pop('PHARMMATE_OBSERVER_RECEIPT',None)
pre = subprocess.run(['/usr/bin/python3','-I','-B',str(gate)], input=json.dumps({'session_id':'launcher-'+secrets.token_hex(16),'hook_event_name':'UserPromptSubmit'}), text=True, env=env,stdout=subprocess.DEVNULL)
if pre.returncode: sys.exit(pre.returncode)
# A skipped/untrusted hook must not be mistaken for success even if the CLI exits 0.
with tempfile.TemporaryDirectory(prefix='pharmmate-probe-') as directory:
    for prefix, event, prompt in [
        ('', 'UserPromptSubmit', 'Respond OK without tools.'),
        ('tool-', 'PreToolUse', 'Policy self-test: request the harmless shell command pwd once, then stop.'),
    ]:
        nonce = prefix + secrets.token_hex(16)
        try:
            subprocess.run(args+['--',prompt], env=dict(env, PHARMMATE_OBSERVER_PROBE=nonce,
                           PHARMMATE_OBSERVER_PROBE_DIR=directory), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        except (OSError, subprocess.TimeoutExpired): sys.exit('BLOCKED: CLI hook probe unavailable; request not sent.')
        proof = Path(directory) / nonce
        if not proof.is_file() or proof.read_text() != event:
            sys.exit(f'BLOCKED: {event} hook execution not verified; request not sent.')
# Real prompt invokes the same UserPromptSubmit gate without the probe nonce.
os.environ.pop('PHARMMATE_OBSERVER_PROBE',None)
if worker:
    schema=Path(sys.argv[3]).resolve(strict=True)
    options=['--json','--output-schema',str(schema),'-'] if agent=='codex' else ['--verbose','--output-format','stream-json','--json-schema',schema.read_text()]
else: options=['--',sys.argv[2]]
os.execvp(agent,args+options)
