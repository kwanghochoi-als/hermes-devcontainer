"""Run one CLI in a sibling container using the controller's actual daemon-side mounts."""
from contextlib import contextmanager
import csv
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

from hermes_cli import projects_db
from hermes_constants import get_hermes_home

from .board import project_for_checkout
from .layout import BASE, OBSERVER, POLICY_FILES

CONTROLLER = os.environ.get("HERMES_CLI_CONTROLLER", "agent-cli")
LABEL = "io.hermes.project-worker"
IO = "/run/hermes-job"


def docker(*args, check=True):
    proc = subprocess.run(["docker", *map(str, args)], capture_output=True, text=True, timeout=90)
    if check and proc.returncode:
        raise RuntimeError(f"Docker {args[0]} 실행 실패 · {proc.stderr.strip()[:500]}")
    return proc


def name_for(cwd):
    key = hashlib.sha256(f"{CONTROLLER}/{get_hermes_home()}/{cwd}".encode()).hexdigest()[:24]
    return f"hermes-worker-{key}"


def reap_checkout(cwd):
    name = name_for(cwd)
    ids = docker("ps", "-aq", "--filter", f"name=^/{name}$", "--filter", f"label={LABEL}={CONTROLLER}").stdout.split()
    for cid in ids:
        docker("rm", "-f", cid)


def toolchain(engine):
    binary = shutil.which(engine)
    if not binary:
        raise RuntimeError(f"{engine} 실행 파일이 없습니다.")
    source = Path(binary).resolve(strict=True)
    vendor = None
    if engine == "codex" and source.suffix == ".js":
        package = source.parent.parent
        vendors = [*package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"),
                   *package.glob("vendor/*/bin/codex")]
        if len(vendors) != 1:
            raise RuntimeError("설치된 Codex 플랫폼 바이너리를 확인하세요.")
        source = vendors[0]
        vendor = source.parents[1]
    stamp = hashlib.sha256(f"{source}/{source.stat().st_size}/{source.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    base = get_hermes_home() / "cli-workers/toolchains"
    dest = base / f"{engine}-{stamp}"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not dest.is_dir():
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            staged = Path(tmp) / "toolchain"
            if vendor:
                shutil.copytree(vendor, staged)
            else:
                (staged / "bin").mkdir(parents=True)
                shutil.copy2(source, staged / "bin" / engine)
            try:
                staged.rename(dest)
            except OSError:
                if not (dest / "bin" / engine).is_file():
                    raise
    return dest


class Mounts:
    def __init__(self, info):
        self.sources = sorted(info["Mounts"], key=lambda m: len(m["Destination"]), reverse=True)
        self.args = []

    def add(self, path, target=None, *, writable=False):
        path = Path(path).resolve(strict=True)
        target = str(target or path)
        for mount in self.sources:
            base = Path(mount["Destination"])
            if path.is_relative_to(base):
                relative = path.relative_to(base)
                if mount["Type"] == "bind":
                    source = Path(mount["Source"]) / relative
                    fields = ["type=bind", f"src={source}"]
                elif mount["Type"] == "volume":
                    fields = ["type=volume", f"src={mount['Name']}", "volume-nocopy"]
                    if relative.parts:
                        fields.append(f"volume-subpath={relative}")
                else:
                    break
                fields.append(f"dst={target}")
                if not writable:
                    fields.append("readonly")
                buf = StringIO()
                csv.writer(buf, lineterminator="").writerow(fields)
                self.args.extend(["--mount", buf.getvalue()])
                return
        raise ValueError(f"Docker에 연결된 경로가 필요합니다 · {path}")


def checkout_mounts(mounts, cwd, state):
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve()
    if cwd == root:
        raise ValueError("개별 프로젝트 또는 운영 저장소의 별도 Git worktree에서 위임하세요.")
    mounts.add(cwd, writable=True)
    common = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir", "--absolute-git-dir"],
                            capture_output=True, text=True)
    if common.returncode == 0:
        gitdir, admin = (Path(p).resolve(strict=True) for p in common.stdout.splitlines())
        if not gitdir.is_relative_to(cwd) and (cwd / ".git").is_file():
            owner = project_for_checkout(cwd)
            source = project_for_checkout(gitdir.parent)
            if (gitdir.name != ".git" or not gitdir.is_relative_to(root)
                    or (cwd / ".git").is_symlink() or admin.parent != gitdir / "worktrees"
                    or Path((admin / "gitdir").read_text().strip()).resolve() != cwd / ".git"
                    or (source and (not owner or source.id != owner.id))):
                raise ValueError("Git worktree의 공통 저장소가 프로젝트와 일치하지 않습니다.")
            mounts.add(gitdir, writable=True)
    # A registered subproject can sit inside the selected checkout.
    empty = state / "empty"
    empty.mkdir(mode=0o700, exist_ok=True)
    with projects_db.connect_closing() as db:
        owner = project_for_checkout(cwd)
        children = sorted({Path(f.path).resolve() for p in projects_db.list_projects(db, include_archived=True)
                           if not owner or p.id != owner.id for f in p.folders
                           if Path(f.path).is_dir() and Path(f.path).resolve().is_relative_to(cwd)
                           and Path(f.path).resolve() != cwd}, key=lambda p: len(p.parts))
    worktrees = cwd / ".worktrees"
    if worktrees.is_dir() and not worktrees.is_symlink():
        children.append(worktrees)
    covered = []
    for child in sorted(children, key=lambda p: len(p.parts)):
        if not any(child.is_relative_to(parent) for parent in covered):
            mounts.add(empty, child)
            covered.append(child)


def legacy_transcript(home, engine, cwd, session):
    if not session:
        return None
    if engine == "claude":
        folder = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
        paths = [home / "projects" / folder / (session + ".jsonl")]
    else:
        paths = (home / "sessions").glob(f"*/*/*/*-{session}.jsonl")
    return next((p for p in paths if p.is_file() and p.resolve().is_relative_to(home.resolve())), None)


def model_preferences(engine, original):
    if engine == "codex":
        file = original / "config.toml"
        settings = tomllib.loads(file.read_text()) if file.is_file() else {}
        return "".join(f"{k} = {json.dumps(settings[k], ensure_ascii=False)}\n"
                       for k in ("model", "model_reasoning_effort") if isinstance(settings.get(k), str))
    file = original / "settings.json"
    settings = json.loads(file.read_text()) if file.is_file() else {}
    selected = {k: settings[k] for k in ("model", "effortLevel", "language") if isinstance(settings.get(k), str)}
    return json.dumps(selected, ensure_ascii=False) if selected else ""


class Container:
    def __init__(self, cid):
        self.cid = cid
        self.wait = subprocess.Popen(["docker", "wait", cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def poll(self):
        if self.wait.poll() is None:
            return None
        output, error = self.wait.communicate()
        if self.wait.returncode or not output.strip().isdecimal():
            raise RuntimeError(f"Docker 작업 종료 상태 확인 실패 · {error.strip()[:300]}")
        return int(output.strip())


@contextmanager
def launch(engine, cli, cwd, project, files, prompt_text, lock, session, observer, attachments):
    info = json.loads(docker("inspect", CONTROLLER).stdout)[0]
    state = get_hermes_home() / "cli-workers"
    name = name_for(cwd)
    reap_checkout(cwd)
    mounts = Mounts(info)
    env = []
    project_key = hashlib.sha256(str(project).encode()).hexdigest()
    home = state / "homes" / project_key / engine
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    mounts.add(home, "/home/ubuntu", writable=True)
    mounts.add(toolchain(engine), "/opt/hermes-cli")
    mounts.add(BASE / "project-cli/container_entry.py", IO + "/entry.py")
    mounts.add(BASE / "project-cli/result.schema.json", IO + "/schema.json")
    original = Path(os.environ.get("CODEX_HOME" if engine == "codex" else "CLAUDE_CONFIG_DIR", Path.home() / ("." + engine)))
    prefs = model_preferences(engine, original)
    if prefs:
        env += ["-e", f"HERMES_CLI_MODEL_SETTINGS={prefs}"]
    auth = original / ("auth.json" if engine == "codex" else ".credentials.json")
    if auth.is_file():
        mounts.add(auth, IO + "/auth")
    legacy = legacy_transcript(original, engine, cwd, session)
    if legacy:
        mounts.add(legacy, IO + "/legacy")
        env += ["-e", f"HERMES_LEGACY_PATH={legacy.relative_to(original)}"]
    if observer:
        for filename in POLICY_FILES:
            mounts.add(OBSERVER / filename, cwd / filename)
        if engine == "codex":
            mounts.add(OBSERVER / ".codex/hooks.json", "/etc/codex/hooks.json")
            mounts.add(OBSERVER / ".codex/requirements.toml", "/etc/codex/requirements.toml")
        authdir = Path.home() / ".hermes/git-auth"
        mounts.add(authdir / "pharmmate.token", IO + "/pharmmate.token")
        mounts.add(authdir / "pharmmate.gitconfig", IO + "/gitconfig")
        mounts.add(BASE / "project-cli/git_auth.py")
        receipt = files / "revisions.json"
        receipt.touch(mode=0o600)
        mounts.add(receipt, IO + "/revisions.json", writable=True)
        env += ["-e", "PHARMMATE_OBSERVER_RECEIPT=" + IO + "/revisions.json", "-e", "GIT_CONFIG_GLOBAL=" + IO + "/gitconfig"]
    else:
        checkout_mounts(mounts, cwd, state)
    for source, target in attachments.items():
        mounts.add(source, target)
    for name_in_env in (("OPENAI_API_KEY", "CODEX_API_KEY") if engine == "codex" else ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")):
        if os.environ.get(name_in_env):
            env += ["-e", name_in_env]
    image_path = next((s[5:] for s in info["Config"]["Env"] if s.startswith("PATH=")), "/usr/local/bin:/usr/bin:/bin")
    env += ["-e", "PATH=/opt/hermes-cli/bin:/opt/hermes-cli/codex-path:" + image_path,
            "-e", "HOME=/home/ubuntu", "-e", "CLAUDE_CONFIG_DIR=/home/ubuntu/.claude", "-e", "CODEX_HOME=/home/ubuntu/.codex"]
    for filename in ("events.jsonl", "stderr.log"):
        file = files / filename
        file.touch(mode=0o600)
        mounts.add(file, IO + "/" + filename, writable=True)
    with tempfile.NamedTemporaryFile(dir=files, mode="w", encoding="utf8") as prompt:
        prompt.write(prompt_text)
        prompt.flush()
        mounts.add(prompt.name, IO + "/prompt")
        shell = 'exec /usr/bin/python3 -I -B /run/hermes-job/entry.py "$@" </run/hermes-job/prompt >/run/hermes-job/events.jsonl 2>/run/hermes-job/stderr.log'
        args = ["run", "-d", "--pull=never", "--init", "--name", name, "--label", f"{LABEL}={CONTROLLER}",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--tmpfs", "/run:rw,nosuid,nodev",
                "--workdir", str(cwd), "--entrypoint", "/bin/sh", *env, *mounts.args,
                info["Image"], "-c", shell, "hermes-job", engine, *cli]
        cid = docker(*args).stdout.strip()
        job = Container(cid)
        try:
            yield job
        finally:
            docker("rm", "-f", cid)
            job.wait.communicate(timeout=15)
