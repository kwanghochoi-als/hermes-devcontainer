"""Reconcile only known devcontainer changes against a pinned Hermes Git tree."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
PATCHES = ("hermes-projects.patch", "hermes-kanban.patch", "hermes-telegram.patch")
BRIDGE = "hermes_cli/workspace_plugin.py"
CONTRACTS = ("apps/shared/src/gateway-contract.generated.ts", "apps/shared/src/gateway-contract.openrpc.json")


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        try:
            temp.write(data)
            temp.flush()
            os.fchmod(temp.fileno(), mode)
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def upstream_contracts(checkout, stage, revision):
    # Use this release's generator and dependencies, with the staged contract as input.
    script = stage / "scripts/gen_gateway_contracts.py"
    inputs = subprocess.check_output(["git", "archive", revision, "scripts/gen_gateway_contracts.py",
                                      "tui_gateway/__init__.py", "tui_gateway/contracts"], cwd=checkout)
    subprocess.run(["tar", "-x", "--exclude=tui_gateway/contracts/projects_pets.py", "-C", stage],
                   input=inputs, check=True)
    (stage / CONTRACTS[0]).parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(checkout))
    from hermes_cli._launchers import runtime_command
    code = f"import runpy; runpy.run_path({str(script)!r}, run_name='__main__')"
    subprocess.run(runtime_command(checkout, code=code), check=True, cwd=stage, stdout=subprocess.DEVNULL)


def reconcile(args, stage, git_dir):
    checkout = args.checkout
    receipt = git_dir / "devcontainer-project-cli.json"
    previous = json.loads(receipt.read_text()) if receipt.exists() else {}
    old_link = checkout / "hermes_cli/workspace_projects"
    if old_link.is_symlink() and old_link.resolve() != HERE / "project-cli":
        raise ValueError(f"Unknown plugin link: {old_link}")
    if old_link.exists() and not old_link.is_symlink():
        raise ValueError(f"Unknown plugin directory: {old_link}")
    legacy = {}
    if old_link.is_symlink():
        legacy = {path: value for value, path in (line.split(maxsplit=1) for line in
                  (HERE / "hermes-legacy.sha256").read_text().splitlines())}
    touched = {p for patch in PATCHES for p in re.findall(r'^\+\+\+ b/(.+)$', (HERE / patch).read_text(), re.M)}
    active = set() if args.restore_upstream else touched | {BRIDGE, *CONTRACTS}
    base = {}
    paths = sorted(active | legacy.keys() | previous.keys())
    tracked = set(subprocess.check_output(["git", "ls-tree", "-r", "--name-only", args.revision, "--", *paths],
                                          cwd=checkout, text=True).splitlines())
    for path in paths:
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"Invalid managed path: {path}")
        data = (subprocess.check_output(["git", "show", f"{args.revision}:{path}"], cwd=checkout)
                if path in tracked else None)
        base[path] = data
        if path in active and path in touched and data is not None:
            (stage / path).parent.mkdir(parents=True, exist_ok=True)
            (stage / path).write_bytes(data)
    if active:
        subprocess.run(["git", "init", "--quiet", "--template=", stage], check=True)
        for patch in PATCHES:
            subprocess.run(["git", "apply", "--whitespace=nowarn", str(HERE / patch)], cwd=stage, check=True)
        if base[BRIDGE] is not None:
            raise ValueError(f"Hermes now provides {BRIDGE}. Review the integration before upgrading.")
        (stage / BRIDGE).write_bytes((HERE / "workspace_plugin.py").read_bytes())
        upstream_contracts(checkout, stage, args.revision)
    changes, hashes = {}, {}
    for path, original in base.items():
        dest = checkout / path
        if dest.is_symlink() or dest.parent.resolve() != dest.parent:
            raise ValueError(f"Refusing to replace a symlink: {dest}")
        current = dest.read_bytes() if dest.exists() else None
        target = (stage / path).read_bytes() if path in active else original
        if path in active:
            hashes[path] = digest(target)
        if current == target:
            continue
        known = {digest(original)}
        if path in previous:
            known.add(previous[path])
        if path in legacy:
            known.add(legacy[path])
        if digest(current) not in known:
            raise ValueError(f"Unknown local change; original file preserved: {dest}")
        changes[path] = target
    if args.verify and (changes or old_link.is_symlink()):
        raise ValueError("Hermes setup required: " + ", ".join(sorted(changes) or [str(old_link)]))
    if not (args.check or args.verify):
        for path, data in changes.items():
            dest = checkout / path
            if data is None:
                dest.unlink()
            else:
                atomic_write(dest, data, dest.stat().st_mode & 0o777 if dest.exists() else 0o644)
        atomic_write(receipt, json.dumps(hashes, sort_keys=True).encode(), 0o600)
        if old_link.is_symlink():
            old_link.unlink()
    print(f"Hermes {args.revision[:12]} · {len(active & touched)} patched paths · {len(changes)} changed paths")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "hermes-agent")
    parser.add_argument("--revision", default=(HERE / "hermes-revision").read_text().strip())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Preflight without applying")
    modes.add_argument("--verify", action="store_true", help="Require the installed sources to match")
    modes.add_argument("--restore-upstream", action="store_true",
                       help="Return installer-owned code to this Git revision before switching releases")
    args = parser.parse_args()
    args.checkout = args.checkout.resolve(strict=True)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.checkout, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or actual != args.revision:
        raise ValueError(f"Hermes 버전 불일치 · 설치 {actual} · 지정 {args.revision}")
    git_dir = Path(subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=args.checkout, text=True).strip())
    with (git_dir / "devcontainer-project-cli.lock").open("a") as lock, tempfile.TemporaryDirectory() as tmp:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reconcile(args, Path(tmp), git_dir)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
