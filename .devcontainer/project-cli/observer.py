"""Project-owned source policies and fresh, immutable inputs for observer jobs."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from .board import EnvironmentActionRequired, path_owners
from . import git_auth

JOB_ROOT = "/opt/hermes-observer"
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}")


def remote_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or
            not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._~/-]+", value)
            or any(part in ("", ".", "..") for part in parsed.path[1:].split("/"))):
        raise ValueError("저장소는 인증 정보가 없는 HTTPS Git URL로 지정하세요.")
    return value


def creation_sources(path, folders, requested, *, remote_checkouts=None):
    sources = {}
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve()
    paths = dict.fromkeys([path, *(Path(f).expanduser().resolve() for f in folders)])
    if any(folder == root or not folder.is_relative_to(root) for folder in paths):
        raise ValueError("관찰 폴더는 작업 공간 안에 있어야 합니다.")
    if requested:
        if not isinstance(requested, (list, tuple)) or not all(isinstance(item, str) for item in requested):
            raise ValueError("관찰 소스는 이름=HTTPS_URL 목록이어야 합니다.")
        for item in requested:
            name, sep, remote = item.partition("=")
            if not sep or not NAME.fullmatch(name) or name in sources:
                raise ValueError("--source에는 고유한 이름=HTTPS_URL을 지정하세요.")
            sources[name] = remote_url(remote)
    else:
        for index, folder in enumerate(paths):
            name = folder.name if NAME.fullmatch(folder.name) else f"source-{index + 1}"
            if name in sources:
                raise ValueError("폴더 이름이 겹칩니다. --source로 저장소 이름을 지정하세요.")
            remote = ""
            if (folder / ".git").exists():
                try:
                    remote = configured_origin(["git", "-C", str(folder)], git_auth.git_env({}, os.devnull),
                                               required_if_ambiguous=True) or ""
                except EnvironmentActionRequired as exc:
                    raise ValueError(f"{folder} · {exc}") from exc
            sources[name] = remote_url(remote.rstrip("/")) if remote.startswith("https://") else str(folder)
            if remote and sources[name] == str(folder) and remote_checkouts is not None:
                remote_checkouts.add(str(folder))
    return sources


def required_origins(db, project_id):
    from hermes_cli import projects_db as pdb
    return set(json.loads(pdb._get_meta(db, f"project-cli.remote-checkouts.{project_id}") or "[]"))


def remember_origins(db, project_id, paths):
    """An explicitly registered clone must not revert to stale HEAD if its remote is removed."""
    if paths:
        from hermes_cli import projects_db as pdb
        previous = required_origins(db, project_id)
        if paths - previous:
            pdb._upsert_meta_locked(db, f"project-cli.remote-checkouts.{project_id}", json.dumps(sorted(previous | paths)))


def project_folders(project):
    workspace = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve()
    folders = {Path(f.path) for f in project.folders}
    return {p for p in folders if workspace in p.parents and p.resolve() == p}


def find_clones(project, owners):
    """Read only this project's checkout metadata, outside the registry write transaction."""
    folders = project_folders(project)
    clones = {}
    env = dict(git_auth.git_env({}, os.devnull), GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1")
    for folder in sorted(folders):
        if owners(folder) != {project.id}:
            continue
        for base, dirs, files in os.walk(folder):
            path = Path(base)
            marker = path / ".git"
            if ".git" in files:
                dirs.clear()
                continue
            if ".git" in dirs:
                config = marker / "config"
                if (marker.is_symlink() or config.is_symlink() or not config.is_file()
                        or os.path.lexists(marker / "commondir") or is_submodule(path, env)):
                    dirs.clear()
                    continue
                if origin := configured_origin(["git", "--git-dir", str(marker)], env):
                    clones[path] = origin
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "vendor", "venv", "__pycache__")
                       and path / d not in folders and owners(path / d) == {project.id}]
    return clones


def is_submodule(path, env):
    # Older submodule layouts have a real .git directory, even when nested clones are present.
    parent = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(path), "rev-parse", "--show-superproject-working-tree"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return parent.returncode == 0 and bool(parent.stdout.strip())


def configured_origin(git, env, *, required_if_ambiguous=False):
    config = subprocess.run([*git, "config", "--local", "--no-includes", "--null", "--get-regexp", r"^remote\..+\.url$"],
                            env=env, capture_output=True)
    if config.returncode == 1:
        return None
    if config.returncode:
        if required_if_ambiguous:
            raise EnvironmentActionRequired("Git 원격 설정을 읽을 수 없습니다. 저장소 설정을 확인하세요.")
        return None
    # With --null, Git separates key from value with LF and terminates each pair with NUL.
    remotes = {}
    for item in config.stdout.split(b"\0"):
        key, _, value = item.partition(b"\n")
        if value:
            remotes.setdefault(key, value)
    origin = remotes.get(b"remote.origin.url")
    if not origin and len(remotes) == 1:  # git clone --origin <name>
        origin = next(iter(remotes.values()))
    if not origin and remotes and required_if_ambiguous:
        raise EnvironmentActionRequired("Git 원격이 여러 개입니다. 최신화할 원격을 origin으로 지정하세요.")
    return os.fsdecode(origin) if origin else None


def origin_key(origin, checkout=None):
    # Standard SSH and HTTPS spellings of the same remote share an observer entry.
    match = re.fullmatch(r"(?:https://([^/]+)/|ssh://git@([^/]+)/|git@([^/:]+):)(.+)", origin.rstrip("/"))
    if match:
        https, ssh, scp, path = match.groups()
        host = https.removesuffix(":443") if https else (ssh or scp).removesuffix(":22")
        return (host.lower(), path.removesuffix(".git"))
    if origin.startswith("file:///"):
        return os.path.normpath(unquote(origin.removeprefix("file://")))
    if checkout is not None and ":" not in origin:
        return os.path.normpath(checkout / origin)
    return origin


def add_clones(sources, clones):
    # A formerly local-only checkout may now have a private HTTPS origin. Keep
    # its alias and append the URL so the project's credential host stays stable.
    for name, source in list(sources.items()):
        origin = clones.get(Path(source)) if source.startswith("/") else None
        if origin and origin.startswith("https://"):
            try:
                url = remote_url(origin.rstrip("/"))
            except ValueError as exc:
                raise EnvironmentActionRequired(f"{name} · 인증 정보 없는 HTTPS origin을 설정하세요.") from exc
            del sources[name]
            sources[name] = url
    registered = set(sources.values())
    known = {origin_key(s) for s in registered}
    known.update(origin_key(origin, path) for path, origin in clones.items() if str(path) in registered)
    # Prefer remote main when both an SSH and an HTTPS copy are new.
    for path, origin in sorted(clones.items(), key=lambda item: (not item[1].startswith("https://"), item[0])):
        key = origin_key(origin, path)
        if str(path) in registered or key in known:
            continue
        source = str(path)
        if origin.startswith("https://"):
            try:
                source = remote_url(origin.rstrip("/"))
            except ValueError:
                raise EnvironmentActionRequired(f"{path} · origin에 인증 정보 없는 HTTPS Git URL을 설정하세요.") from None
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.name).lstrip(".-")[:80] or "source"
        name = base
        suffix = 1
        while name in sources:
            suffix += 1
            tail = f"-{suffix}"
            name = base[:80 - len(tail)] + tail
        sources[name] = source
        known.add(key)


def refresh_sources(project):
    """Append new clone sources at card start and return the policy for this attempt."""
    from hermes_cli import projects_db as pdb
    with pdb.connect_closing() as db:
        clones = find_clones(project, path_owners(db))
        with pdb.write_txn(db):
            current = pdb.get_project(db, project.id)
            if not current or current.archived or not current.readonly:
                raise EnvironmentActionRequired("읽기 전용 프로젝트의 등록 상태를 확인하세요.")
            owners = path_owners(db)
            folders = project_folders(current)
            clones = {p: url for p, url in clones.items() if owners(p) == {current.id}
                      and any(p.is_relative_to(f) for f in folders) and p.resolve() == p}
            original = json.loads(current.observer_sources)
            sources = original.copy()
            add_clones(sources, clones)
            remember_origins(db, current.id, {str(p) for p in folders & clones.keys() if str(p) in sources.values()})
            if sources != original:
                current.observer_sources = json.dumps(sources)
                db.execute("UPDATE projects SET observer_sources=? WHERE id=?", (current.observer_sources, current.id))
            return current


def local_source_paths(project, sources):
    from hermes_cli import projects_db as pdb
    registered = {Path(f.path).resolve() for f in project.folders}
    local = {Path(s) for s in sources.values() if s.startswith("/")}
    if not local:
        return set(), set()
    with pdb.connect_closing() as db:
        owners = path_owners(db)
        standalone = registered - {Path(s) for s in required_origins(db, project.id)}
    folders = project_folders(project)
    nested = {p for p in local - registered if any(f in p.parents for f in folders) and owners(p) == {project.id}
              and (p / ".git").is_dir() and not (p / ".git").is_symlink()}
    return registered | nested, standalone


def git_common_dir(path, workspace, env, *, bare=False):
    """Validate an actual checkout/bare root, including the backlink for an explicit worktree."""
    if workspace not in path.parents or path.resolve() != path or workspace / ".git" in (path, *path.parents):
        return None
    marker = path / ".git"
    git = ["git", "-C", str(path)]
    if marker.is_symlink():
        return None
    if not marker.exists():
        if not bare:
            return None
        git = ["git", "--git-dir", str(path)]
    result = subprocess.run([*git, "rev-parse", "--path-format=absolute", "--git-common-dir"], env=env,
                            capture_output=True, text=True)
    if result.returncode:
        return None
    common = Path(result.stdout.strip()).resolve()
    if common == marker or (bare and common == path):
        return common
    if marker.is_file() and common.is_relative_to(workspace) and common.parent != workspace and common.name == ".git":
        admin = subprocess.run([*git, "rev-parse", "--absolute-git-dir"], env=env, capture_output=True, text=True)
        directory = Path(admin.stdout.strip()).resolve()
        backlink = directory / "gitdir"
        if (not admin.returncode and directory.parent == common / "worktrees" and backlink.is_file()
                and Path(backlink.read_text().strip()).resolve() == marker):
            return common
    return None


SSH_REMOTE = re.compile(r"(?:(?:[A-Za-z0-9_][\w.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*:"
                        r"|ssh://(?:[A-Za-z0-9_][\w.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?/)"
                        r"[A-Za-z0-9_~/][A-Za-z0-9._~/-]*")


def upstream_url(origin, checkout, workspace, project_id, env):
    if origin.startswith("https://"):
        return remote_url(origin.rstrip("/"))
    if SSH_REMOTE.fullmatch(origin) and "://" not in origin.removeprefix("ssh://"):
        return origin
    if origin.startswith("file://"):
        url = urlsplit(origin)
        if url.netloc not in ("", "localhost") or url.query or url.fragment:
            raise ValueError("로컬 원격 Git 경로를 확인하세요.")
        origin = unquote(url.path)
    elif ":" in origin:
        raise ValueError("원격을 HTTPS, SSH 또는 작업 공간의 Git 경로로 지정하세요.")
    path = (checkout / origin).resolve()
    from hermes_cli import projects_db as pdb
    with pdb.connect_closing() as db:
        owners = path_owners(db)
    if workspace in path.parents and not owners(path) - {project_id}:
        common = git_common_dir(path, workspace, env, bare=True)
        if common and not owners(common) - {project_id}:
            return str(path)
    raise ValueError("다른 프로젝트와 겹치지 않는 작업 공간의 Git 원격 경로를 지정하세요.")


def prepare(project, target):
    """Capture fresh upstream main, or the authoritative HEAD of an originless checkout."""
    sources = json.loads(project.observer_sources)
    if not isinstance(sources, dict) or not sources:
        raise ValueError("읽기 전용 프로젝트의 관찰 소스를 확인하세요.")
    env = git_auth.git_env(git_auth.credential_sources(sources), git_auth.token_path(project.id))
    registered, standalone = local_source_paths(project, sources)
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve()
    revisions = {}
    local_heads = []
    for name, source in sources.items():
        if not NAME.fullmatch(name):
            raise ValueError("잘못된 관찰 저장소 이름입니다.")
        repo = target / (name + ".git")
        ref = "refs/heads/main"
        git_env = env
        try:
            if source.startswith("/"):
                path = Path(source)
                if path not in registered or not git_common_dir(path, root, env):
                    raise EnvironmentActionRequired(f"{name} · 등록한 로컬 Git 저장소와 경로를 확인하세요.")
                origin = configured_origin(["git", "-C", source], env, required_if_ambiguous=True)
                if origin:
                    source = upstream_url(origin, path, root, project.id, env)
                elif path in standalone:
                    ref = "HEAD"
                    local_heads.append(name)
                else:
                    raise EnvironmentActionRequired(f"{name} · 등록한 복제본의 Git 원격을 복구하세요.")
            else:
                remote_url(source)
            if not source.startswith(("/", "https://")):
                git_env = dict(env, GIT_ALLOW_PROTOCOL=env["GIT_ALLOW_PROTOCOL"] + ":ssh",
                               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no")
                if os.environ.get("SSH_AUTH_SOCK"):
                    git_env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
            subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], env=env, check=True, capture_output=True)
            git = ["git", "--git-dir", str(repo)]
            subprocess.run([*git, "-c", "http.followRedirects=false", "fetch", "--no-tags", "--no-recurse-submodules",
                            "--depth=1", "--", source, ref + ":refs/heads/snapshot"], stdin=subprocess.DEVNULL, cwd=target, env=git_env,
                           capture_output=True, check=True, timeout=90)
            revisions[name] = subprocess.check_output([*git, "rev-parse", "--verify", "refs/heads/snapshot^{commit}"],
                                                      env=env, text=True).strip()
        except ValueError as exc:
            raise EnvironmentActionRequired(f"{name} · {exc}") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise EnvironmentActionRequired(f"{name} · {ref} 스냅샷 준비 실패. 저장소, 읽기 권한과 연결을 확인하세요.") from exc
    (target / "commits.json").write_text(json.dumps(revisions))
    (target / "local-sources.json").write_text(json.dumps(local_heads))
    return revisions


def requires_observer(conn, card_id, board):
    """Native profile workers cannot enforce the CLI snapshot policy."""
    from hermes_cli import kanban_db as kb, projects_db as pdb
    from .board import project_for_checkout
    task = kb.get_task(conn, card_id)
    if not task:
        return False
    meta = kb.read_board_metadata(board or kb.get_current_board())
    with pdb.connect_closing() as projects:
        for pid in (task.project_id, meta.get("project_id")):
            project = pid and pdb.get_project(projects, pid)
            if project and project.readonly:
                return True
    for path in (task.workspace_path, meta.get("default_workdir")):
        if path and Path(path).is_absolute():
            project = project_for_checkout(Path(path).resolve())
            if project and project.readonly:
                return True
    return False
