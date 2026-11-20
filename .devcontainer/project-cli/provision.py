"""Initialize a Dev Container workspace through Hermes' native project creator."""

from datetime import datetime, timezone
import fcntl
from functools import wraps
import os
from pathlib import Path
import subprocess

from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from persistence.conversation.sessions import sanitize_title

from .board import BOARD
from .knowledge import register_collection


def workspace_root():
    return Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve(strict=True)


def provision_existing(pdb, conn, project):
    """The native chat tool can resolve an existing folder before calling create_project."""
    path = Path(project.primary_path or project.folders[0].path).resolve()
    root = workspace_root()
    if path != root and path.is_relative_to(root):
        pdb.create_project(conn, name=project.name, primary_path=str(path))
        return pdb.get_project(conn, project.id)
    return project


def git_repo(path, root):
    def git(*args, check=True):
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
        if check and result.returncode:
            raise ValueError(result.stderr.strip() or f"Git 실행 실패 · {path}")
        return result

    path.mkdir(parents=True, exist_ok=True)
    checkout = git("rev-parse", "--show-toplevel", check=False)
    current = Path(checkout.stdout.strip()).resolve() if not checkout.returncode else None
    if current != path:
        if os.path.lexists(path / ".git") or current not in (root, None):
            raise ValueError(f"독립된 Git 저장소의 루트 폴더를 선택하세요 · {path}")
        git("init", "--quiet", "--initial-branch=main")
    if git("rev-parse", "--verify", "HEAD", check=False).returncode:
        # A native Kanban worktree needs a commit. --only leaves an existing index intact.
        git("-c", "user.name=Hermes", "-c", "user.email=hermes@localhost",
            "commit", "--quiet", "--allow-empty", "--only", "-m", "Initialize project")


def existing_session(conn, sessions, project, path, title):
    from hermes_cli import projects_db as pdb

    saved_id = project and pdb._get_meta(conn, f"project-cli.session.{project.id}")
    if saved_id and sessions.get_session(saved_id):
        return saved_id
    matching = sessions.resolve_session_by_title(title)
    if matching:
        row = sessions.get_session(matching)
        if not row or row.get("cwd") != str(path):
            raise ValueError(f"다른 작업 폴더에서 이미 사용하는 세션 제목입니다 · {title}")
    return matching


def starter_session(conn, sessions, project, path, title, collection, sid):
    from hermes_cli import projects_db as pdb

    # Derive a normal Hermes session ID so a retry after a partial write finds the same row.
    stamp = datetime.fromtimestamp(project.created_at, timezone.utc).strftime("%Y%m%d_%H%M%S")
    sid = sid or f"{stamp}_{project.id.removeprefix('p_')}"
    row = sessions.get_session(sid)
    if row and row.get("cwd") != str(path):
        raise ValueError(f"다른 작업 폴더에 연결된 세션입니다 · {sid}")
    if not row:
        sessions.create_session(sid, "cli", cwd=str(path), git_repo_root=str(path))
    if not row or not row.get("title"):
        sessions.set_session_title(sid, title)
    if not row or not row.get("message_count"):
        # The native project sidebar lists sessions with a transcript. Give it a real
        # local welcome message without submitting a prompt or starting a model turn.
        sessions.append_message(sid, "assistant",
            f"작업 폴더는 `{path}`이고 Kanban 보드는 `{project.board_slug}`입니다. "
            f"`/codex` 또는 `/claude`로 이 프로젝트에 작업을 맡길 수 있습니다. "
            f"완료 카드에서 확인한 지식은 QMD의 `{collection}` 컬렉션에서 검색할 수 있습니다.",
            message_uid=f"project-welcome-{project.id}")
    key = f"project-cli.session.{project.id}"
    if pdb._get_meta(conn, key) != sid:
        with pdb.write_txn(conn):
            pdb._upsert_meta_locked(conn, key, sid)


def workspace_creator(native_create):
    @wraps(native_create)
    def create(conn, *, name, folders=None, primary_path=None, **options):
        from hermes_cli import projects_db as pdb

        name = str(name or "").strip()
        if not name:
            raise ValueError("프로젝트 이름을 입력하세요.")
        folders = list(folders or ())
        root = workspace_root()
        selected = primary_path or next(iter(folders), None)
        if not selected and (name.startswith(".") or "/" in name or "\\" in name):
            raise ValueError("프로젝트 폴더에 사용할 수 있는 이름을 입력하세요.")
        path = Path(selected or root / name).expanduser().resolve()
        if path == root or not path.is_relative_to(root) or options.get("allow_duplicate_path"):
            if not selected:
                raise ValueError(f"프로젝트 폴더는 {root} 안에 있어야 합니다.")
            return native_create(conn, name=name, folders=folders, primary_path=primary_path, **options)

        kb._assert_not_delegated_child_mutation()
        if path.exists() and not path.is_dir():
            raise ValueError(f"프로젝트 경로에 같은 이름의 파일이 있습니다 · {path}")
        title = sanitize_title(f"{name}-세션")
        with (get_hermes_home() / ".project-create.lock").open("a") as lock, SessionDB() as sessions:
            fcntl.flock(lock, fcntl.LOCK_EX)
            candidate = pdb.normalize_slug(options.get("slug")) or pdb._slugify(name)
            old = pdb.find_by_primary_path(conn, str(path), include_archived=True)
            if not old:
                previous = pdb.get_project(conn, candidate)
                if previous and not previous.archived and not previous.folders and previous.name == name:
                    old = previous
            if old and (old.archived or old.name != name):
                raise ValueError(f"이미 프로젝트 {old.name}에 연결된 폴더입니다 · {path}")
            slug = old.slug if old else pdb._unique_slug(conn, candidate)
            explicit_board = pdb.normalize_slug(options.get("board_slug"))
            shared = (BOARD, kb.DEFAULT_BOARD)
            if explicit_board in shared:
                raise ValueError(f"프로젝트 전용 보드를 지정하세요 · {explicit_board}")
            binding = old.board_slug if old and old.board_slug not in shared else None
            board_name = explicit_board or binding or (f"project-{slug}" if slug in shared else slug)
            meta = kb.read_board_metadata(board_name)
            owner = meta.get("project_id")
            other_binding = binding and explicit_board and binding != explicit_board
            other_owner = owner and (not old or owner != old.id)
            existing_board = not owner and kb.board_exists(board_name) and not (explicit_board or binding)
            if other_binding or meta["archived"] or other_owner or existing_board:
                raise ValueError(f"이미 사용 중인 Kanban 보드입니다 · {board_name}")
            existing_sid = existing_session(conn, sessions, old, path, title)
            git_repo(path, root)
            try:
                collection = register_collection(path)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"QMD 컬렉션 등록에 실패했습니다. QMD 설정을 확인하고 다시 생성하세요 · {path}") from exc
            if not old:
                options.update(slug=slug, board_slug=board_name)
                pid = native_create(conn, name=name, folders=folders, primary_path=str(path), **options)
            else:
                pid = old.id
                if not old.primary_path:
                    pdb.add_folder(conn, pid, str(path), is_primary=True)
                    for folder in folders:
                        pdb.add_folder(conn, pid, folder)
                if old.board_slug != board_name:
                    pdb.update_project(conn, pid, board_slug=board_name)
            if owner != pid or meta.get("default_workdir") != str(path) or not kb.kanban_db_path(board_name).exists():
                kb.create_board(board_name, name=name, default_workdir=str(path), project_id=pid)
            starter_session(conn, sessions, pdb.get_project(conn, pid), path, title, collection, existing_sid)
            return pid
    return create
