"""Trusted observer hooks. Only fetch into private bare mirrors, never into team checkouts."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = Path('/workspaces/pharmmate')
MANIFEST = json.loads(Path(__file__).with_name('sources.json').read_text())
REPOS = {**MANIFEST['pharmmate'], **{Path(path).name: url for path, url in MANIFEST['pharmscope'].items()
                                     if path.startswith('cicd/')}}
CACHE = Path.home()/'.cache/pharmmate-observer'
READER = '/usr/bin/python3 -I -B /workspaces/pharmmate/.observer/gate.py read'
SAFE = r'[A-Za-z0-9_.@/+\-]+'

def git(*args):
    return subprocess.check_output(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','gc.auto=0', *map(str,args)], stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=45, env=dict(os.environ,GIT_TERMINAL_PROMPT='0'))

def refresh(checkout, cache):
    # Only the configured remote address is used, not developer files or local branch state.
    remote = (git('-C',checkout,'remote','get-url','origin').decode().strip()
              if isinstance(checkout, Path) else checkout)
    git('init','--bare','--quiet',cache)
    git('--git-dir',cache,'fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--',remote,'refs/heads/main')
    return git('--git-dir',cache,'rev-parse','--verify','FETCH_HEAD^{commit}').decode().strip()

def words(cmd):
    match = re.fullmatch(re.escape(READER) + r' ([0-9a-f]{32}) (list|show) ([a-z-]+)(?: (' + SAFE + r'))?',cmd)
    if not match: return None
    token, op, name, path = match.groups()
    if name not in REPOS or (op=='show') != bool(path): return None
    if path and any(p in ('','.','..') for p in path.split('/')): return None
    return token,op,name,path

def allowed(event):
    inp = event.get('tool_input') or {}
    if event.get('tool_name') not in ('Bash','exec_command'): return False
    # Never permit shell selection, wrappers, environment overrides or extra commands.
    if set(inp)-{'command','cmd','description','timeout','yield_time_ms','max_output_tokens','login','workdir'}: return False
    if 'cmd' in inp and 'command' in inp: return False
    if inp.get('workdir',str(ROOT)) != str(ROOT): return False
    return bool(words(inp.get('command',inp.get('cmd',''))))

def key(e):
    sid = e['session_id']; assert isinstance(sid,str) and sid
    return CACHE / ('session-' + hashlib.sha256(sid.encode()).hexdigest())

def deny_tool(reason):
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                             'permissionDecisionReason': reason}}))

def worker_output(e):
    if e.get('tool_name') != 'StructuredOutput' or not os.environ.get('PHARMMATE_OBSERVER_RECEIPT'):
        return False
    report = e.get('tool_input')
    return (isinstance(report, dict) and set(report) == {'status','summary','tests','knowledge'}
            and report['status'] in ('completed','blocked','failed') and isinstance(report['summary'], str)
            and bool(report['summary'].strip()) and isinstance(report['tests'], str)
            and isinstance(report['knowledge'], list)
            and all(isinstance(k, dict) and set(k) == {'fact','evidence'}
                    and all(isinstance(v, str) for v in k.values()) for k in report['knowledge']))

def probe_seen(challenge, event):
    if directory := os.environ.get('PHARMMATE_OBSERVER_PROBE_DIR'):
        (Path(directory) / challenge).write_text(event)

def hook(e):
    import fcntl
    event = e['hook_event_name']; state = key(e)
    challenge = os.environ.get('PHARMMATE_OBSERVER_PROBE','')
    if re.fullmatch('tool-[0-9a-f]{32}',challenge):
        if event == 'UserPromptSubmit':
            print('{}'); return
        if event != 'PreToolUse': raise ValueError('invalid probe event')
        probe_seen(challenge, event)
        deny_tool('OBSERVER_HOOK_BLOCK_' + challenge)
        return
    if event == 'PreToolUse':
        if worker_output(e) and re.fullmatch('[0-9a-f]{32}',state.read_text()):
            print(json.dumps({'hookSpecificOutput':{'hookEventName':event,'permissionDecision':'allow',
                                                    'permissionDecisionReason':'Format the verified worker report'}}))
            return
        if not allowed(e): raise ValueError('read-only')
        inp = e['tool_input']; parsed = words(inp.get('command',inp.get('cmd','')))
        if parsed is None: raise ValueError('unknown request')
        token = parsed[0]
        if token != state.read_text(): raise ValueError('no current snapshot')
        print(json.dumps({'hookSpecificOutput':{'hookEventName':event,'permissionDecision':'allow','permissionDecisionReason':'Pinned Git object reader only'}})); return
    if event != 'UserPromptSubmit': raise ValueError('invalid event')
    if re.fullmatch('[0-9a-f]{32}',challenge):
        probe_seen(challenge, event)
        print('OBSERVER_HOOK_BLOCK_'+challenge,file=sys.stderr); sys.exit(2)
    CACHE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (CACHE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state.unlink(missing_ok=True)
        with ThreadPoolExecutor(max_workers=len(REPOS)) as pool:
            ids=dict(zip(REPOS,pool.map(lambda n:refresh(REPOS[n],CACHE/(n+'.git')),REPOS)))
        token=secrets.token_hex(16); (CACHE/token).write_text(json.dumps(ids)); state.write_text(token)
        if receipt := os.environ.get('PHARMMATE_OBSERVER_RECEIPT'):
            target = Path(receipt)
            target.write_text(json.dumps({'session_id': e['session_id'], 'repositories': ids}))
    message=f'PHARMMATE_OBSERVER_OK {token}. Read only, via exact commands: {READER} {token} list REPO or {READER} {token} show REPO PATH. Repositories: '+', '.join(ids)
    print(json.dumps({'hookSpecificOutput':{'hookEventName':event,'additionalContext':message}}))

def reader(args):
    parsed=words(READER+' '+' '.join(args))
    if not parsed: raise ValueError('invalid request')
    token,op,name,path=parsed; rev=json.loads((CACHE/token).read_text())[name]; assert re.fullmatch('[0-9a-f]{40,64}',rev)
    repo=CACHE/(name+'.git')
    if op=='list': data=git('--git-dir',repo,'ls-tree','-r','--name-only',rev)
    else:
        if any(re.search(r'(^\.env($|\.)|secret|credential|password|\.pem$|\.key$|\.tfstate|^id_(rsa|ed25519))',p,re.I) for p in path.split('/')): raise ValueError('sensitive path')
        obj=rev+':'+path
        if int(git('--git-dir',repo,'cat-file','-s',obj))>2_000_000: raise ValueError('file too large')
        data=git('--git-dir',repo,'cat-file','blob',obj)
    sys.stdout.buffer.write(data)

if __name__=='__main__':
    os.umask(0o077)
    event = {}
    try:
        if sys.argv[1:2]==['read']: reader(sys.argv[2:])
        else:
            event = json.load(sys.stdin)
            hook(event)
    except Exception:
        if isinstance(event, dict) and event.get('hook_event_name') == 'PreToolUse':
            deny_tool('Pharmmate 관찰 정책은 이번 세션의 고정된 Git 객체 조회만 허용합니다.')
            sys.exit(0)
        print('BLOCKED: 최신 원격 main을 확인하지 못했거나 읽기 전용 정책에서 허용하지 않는 요청입니다.',file=sys.stderr); sys.exit(2)
