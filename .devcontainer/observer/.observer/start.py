"""Trusted one-shot entry point. Never pass the user's request to an unverified CLI."""
import json
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

def blocked(reason, code=1):
    message = f'BLOCKED: {reason}'
    if worker:
        print(json.dumps({'type': 'turn.failed', 'error': {'message': message}}), flush=True)
    print(message, file=sys.stderr)
    raise SystemExit(code)

os.environ.pop('PHARMMATE_OBSERVER_READER', None)
if worker and agent == 'codex':
    if not os.environ.get('PHARMMATE_OBSERVER_RECEIPT'):
        blocked('이 워커의 스냅샷 기록 경로가 없습니다.')
    # The container forbids nested user namespaces. The MCP tool exposes only
    # the pinned Git reader and independently checks this run's receipt.
    os.environ['PHARMMATE_OBSERVER_READER'] = 'mcp'
    reader_args = json.dumps(['-I', '-B', str(ROOT/'.observer/snapshot_mcp.py')])
    args += ['-c', 'mcp_servers.pharmmate_snapshot={command="/usr/bin/python3",args=' + reader_args
             + ',env_vars=["PHARMMATE_OBSERVER_RECEIPT"],required=true}']

# Fail before even launching a probe if the upstream cannot be verified. The live prompt
# rechecks freshness and pins its own token (also protects resumed/follow-up turns).
gate = ROOT/'.observer/gate.py'
for key in ('PHARMMATE_OBSERVER_PROBE', 'PHARMMATE_OBSERVER_PROBE_DIR'):
    os.environ.pop(key, None)
env = dict(os.environ)
env.pop('PHARMMATE_OBSERVER_RECEIPT',None)
pre = subprocess.run(['/usr/bin/python3','-I','-B',str(gate)], input=json.dumps({'session_id':'launcher-'+secrets.token_hex(16),'hook_event_name':'UserPromptSubmit'}), text=True, env=env,stdout=subprocess.DEVNULL)
if pre.returncode:
    blocked('원격 main 스냅샷을 확인할 수 없습니다.', pre.returncode)
# A skipped/untrusted hook must not be mistaken for success even if the CLI exits 0.
with tempfile.TemporaryDirectory(prefix='pharmmate-probe-') as directory:
    for prefix, event, prompt, timeout in [
        ('', 'UserPromptSubmit', 'Respond OK without tools.', 45),
        ('tool-', 'PreToolUse', "Policy self-test: use the exact read tool and list operation supplied "
         "by this turn's verified snapshot hook once. When the hook blocks it, finish immediately.", 180),
    ]:
        nonce = prefix + secrets.token_hex(16)
        try:
            result = subprocess.run(args+['--',prompt], env=dict(env, PHARMMATE_OBSERVER_PROBE=nonce,
                           PHARMMATE_OBSERVER_PROBE_DIR=directory), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            blocked(f'{event} 훅 자가 검사 시간 초과 ({timeout}초).')
        except OSError as exc:
            blocked(f'{event} 훅 자가 검사 실행 실패 ({exc.strerror}).')
        proof = Path(directory) / nonce
        if not proof.is_file() or proof.read_text() != event:
            blocked(f'{event} 훅 자가 검사에서 실행을 확인할 수 없습니다 (CLI 종료 코드 {result.returncode}).')
# Real prompt invokes the same UserPromptSubmit gate without the probe nonce.
if worker:
    schema=Path(sys.argv[3]).resolve(strict=True)
    if receipt := os.environ.get('PHARMMATE_OBSERVER_RECEIPT'):
        Path(receipt).write_text('')
    options=['--json','--output-schema',str(schema),'-'] if agent=='codex' else ['--verbose','--output-format','stream-json','--json-schema',schema.read_text()]
else: options=['--',sys.argv[2]]
os.execvp(agent,args+options)
