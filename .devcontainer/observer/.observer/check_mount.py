"""Exercise the mounted hook commands offline in a disposable observer worker."""
import json
import os
from pathlib import Path
import shlex
import subprocess


def check_mount():
    root = Path.cwd()
    codex = Path("/etc/codex")
    for file in (root / "AGENTS.md", root / "CLAUDE.md", root / ".observer/start.py",
                 root / ".observer/snapshot_mcp.py", root / ".observer/gate.py", root / ".claude/settings.json",
                 codex / "hooks.json", codex / "requirements.toml"):
        if not file.is_file() or not os.access(file, os.R_OK) or not (os.statvfs(file).f_flag & os.ST_RDONLY):
            raise ValueError(f"파일과 읽기 전용 마운트를 확인하세요 · {file}")

    snapshots = Path("/run/hermes-job/snapshots")
    checkout = "/tmp/repo"
    subprocess.run(["git", "init", "-q", "--separate-git-dir", snapshots / "check.git", checkout], check=True)
    git = ["git", "-C", checkout]
    subprocess.run([*git, "-c", "user.name=Observer", "-c", "user.email=observer@localhost",
                    "commit", "-qm", "mount check", "--allow-empty"], check=True)
    rev = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    (snapshots / "commits.json").write_text(json.dumps({"check": rev}))
    gate = ["/usr/bin/python3", "-I", "-B", str(root / ".observer/gate.py")]
    for config in (root / ".claude/settings.json", codex / "hooks.json"):
        hooks = json.loads(config.read_text())["hooks"]
        receipt = Path("/tmp") / (config.name + ".receipt")
        env = dict(os.environ, HERMES_OBSERVER_RECEIPT=str(receipt))
        for event in ("UserPromptSubmit", "PreToolUse"):
            commands = [shlex.split(h["command"]) for group in hooks[event] for h in group["hooks"]
                        if h["type"] == "command"]
            if gate not in commands:
                raise ValueError(f"{event} 훅의 실행 경로를 확인하세요 · {config}")
            request = {"session_id": config.name, "hook_event_name": event, "tool_name": "Write"}
            output = subprocess.check_output(gate, input=json.dumps(request), env=env, text=True)
            if event == "PreToolUse" and json.loads(output)["hookSpecificOutput"]["permissionDecision"] != "deny":
                raise ValueError(f"쓰기 도구 차단을 확인하세요 · {config}")
        if json.loads(receipt.read_text()) != {"session_id": config.name, "repositories": {"check": rev}}:
            raise ValueError(f"스냅샷 확인 기록이 일치하지 않습니다 · {config}")


if __name__ == "__main__":
    try:
        check_mount()
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Observer 마운트 점검 실패 · {exc}") from None
