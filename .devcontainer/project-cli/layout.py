"""Restore an explicitly selected project preset and its optional checkout layout."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

BASE = Path(__file__).resolve().parents[1]
PROJECTS = json.loads((BASE / "projects.json").read_text())
OBSERVER = BASE / "observer"
PRESETS = json.loads((BASE / "observer-presets.json").read_text())


def restore(name, clone):
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve(strict=True)
    args = ["hermes", "project", "create", name, str(root / name)]
    if name in PRESETS:
        args.append("--readonly")
        for key, url in PRESETS[name].items():
            args.extend(["--source", f"{key}={url}"])
    subprocess.run(args, check=True)
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
