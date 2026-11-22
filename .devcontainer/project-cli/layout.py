"""Install the versioned project policy and restore the checkout layout on a new host."""

import json
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

BASE = Path(__file__).resolve().parents[1]
PROJECTS = json.loads((BASE / "projects.json").read_text())
OBSERVER = BASE / "observer"
POLICY_FILES = ("AGENTS.md", "CLAUDE.md", ".claude/settings.json", ".codex/config.toml",
                ".codex/hooks.json", ".observer/start.py", ".observer/gate.py", ".observer/sources.json")
PREVIOUS = {
    ".observer/start.py": "ca43ba226b8a4dc876612952341f98c6ab9bee93d4af98ddf74656a01f2f08a8",
    ".observer/gate.py": "13783d7f4ae916cf77ab39783c1c48c878cbd3d067d0775df2976a0a95e239ac",
}


def install_observer(root):
    for name in POLICY_FILES:
        source = (OBSERVER / name).resolve(strict=True)
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"관찰 정책 설치 폴더를 확인하세요 · {target.parent}")
        if target.is_symlink() and target.resolve() == source:
            continue
        if target.exists() or target.is_symlink():
            data = target.read_bytes() if target.is_file() and not target.is_symlink() else None
            if data is None or (data != source.read_bytes() and hashlib.sha256(data).hexdigest() != PREVIOUS.get(name)):
                raise ValueError(f"로컬 관찰 정책 변경을 먼저 확인하세요 · {target}")
        with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
            link = Path(tmp) / "policy"
            link.symlink_to(os.path.relpath(source, target.parent))
            link.replace(target)


def restore(name, clone):
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve(strict=True)
    subprocess.run(["hermes", "project", "create", name, str(root / name)], check=True)
    if clone:
        for relative, remote in PROJECTS[name].items():
            target = root / name / relative
            if target.resolve() != target:
                raise ValueError(f"예상과 다른 저장소 경로 · {target}")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
                    checkout = Path(tmp) / "checkout"
                    subprocess.run(["git", "clone", "--", remote, str(checkout)], check=True)
                    try:
                        checkout.rename(target)
                    except OSError:
                        if not target.is_dir():
                            raise
            # Compare the configured origin even when a host uses url.*.insteadOf.
            result = subprocess.run(["git", "-C", str(target), "config", "--local", "--get", "remote.origin.url"],
                                    text=True, capture_output=True)
            if not (target / ".git").exists() or result.stdout.strip() != remote:
                raise ValueError(f"기존 저장소 위치 또는 origin을 확인하세요 · {target}")


if __name__ == "__main__":
    folder = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve() / "pharmmate"
    if (folder / ".git").is_dir():
        if folder.resolve() != folder:
            raise ValueError(f"관찰 정책의 프로젝트 경로를 확인하세요 · {folder}")
        install_observer(folder)
