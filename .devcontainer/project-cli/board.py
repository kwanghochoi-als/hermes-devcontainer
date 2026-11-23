import os
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path
import subprocess

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

BOARD = "cli-workers"
ENGINES = ("codex", "claude")
LANES = {f"cli:{engine}": engine for engine in ENGINES}


class EnvironmentActionRequired(RuntimeError):
    """This card can resume after the operator restores an external prerequisite."""


def board(slug=BOARD):
    if slug == BOARD and not kb.kanban_db_path(board=BOARD).exists():
        kb.create_board(BOARD, name="Project CLI workers")
    return closing(connect(board=slug))


@contextmanager
def worker_boards():
    """Keep a SQLite connection per live board, including boards created while serving."""
    opened = {}

    def refresh():
        names = {BOARD}
        names.update(meta["slug"] for meta in kb.list_boards(include_archived=False) if meta.get("project_id"))
        for slug in opened.keys() - names:
            opened.pop(slug).close()
        for slug in sorted(names - opened.keys()):
            try:
                opened[slug] = connect(board=slug)
            except ValueError:
                # A project can be deleted between listing and opening its board.
                if slug == BOARD or (kb.board_exists(slug) and not kb.read_board_metadata(slug).get("archived")):
                    raise
        return opened

    try:
        yield refresh
    finally:
        for conn in opened.values():
            conn.close()


def project_for_checkout(path):
    from hermes_cli import projects_db as pdb
    from tui_gateway.git_probe import common_repo_root

    with pdb.connect_closing() as conn:
        project = pdb.project_for_path(conn, str(path), include_archived=True)
        common = common_repo_root(str(path))
        if common and Path(common) != path:
            primary = pdb.project_for_path(conn, common, include_archived=True)
            if primary and project and primary.id != project.id:
                raise ValueError("Git worktree와 실제 저장소가 서로 다른 프로젝트에 등록되어 있습니다.")
            project = project or primary
        return project


def path_owners(conn):
    folders = {}
    for path, pid in conn.execute("SELECT path, project_id FROM project_folders"):
        folders.setdefault(Path(path), set()).add(pid)

    @lru_cache(maxsize=512)
    def owners(path):
        if path:
            p = Path(path)
            for parent in (p, *p.parents):
                if parent in folders:
                    return folders[parent]
        return set()

    return owners


def knowledge_root(path):
    project = project_for_checkout(path)
    return Path(project.primary_path) if project and project.primary_path else path


def board_for_checkout(path):
    project = project_for_checkout(path)
    if not project:
        return BOARD
    if project.archived or not project.board_slug or project.board_slug == BOARD:
        raise ValueError(f"프로젝트 {project.name}의 전용 보드 연결을 확인하세요.")
    meta = kb.read_board_metadata(project.board_slug)
    if meta.get("archived") or meta.get("project_id") != project.id:
        raise ValueError(f"프로젝트 {project.name}의 Kanban 보드 연결을 확인하세요.")
    return project.board_slug


def project_dir(path):
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve(strict=True)
    cwd = Path(path).resolve(strict=True)
    if not cwd.is_dir() or not cwd.is_relative_to(root):
        raise ValueError(f"프로젝트 폴더는 {root} 안에 있어야 합니다.")
    git = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False)
    if git.returncode == 0:
        checkout = Path(git.stdout.strip()).resolve()
        # /workspaces itself can be the operations repo. A new project beneath
        # it must not widen its working directory to every other project.
        if checkout.is_relative_to(root) and (checkout != root or cwd == root):
            if checkout != cwd:
                owner = project_for_checkout(cwd)
                enclosing = project_for_checkout(checkout)
                if owner and (not enclosing or owner.id != enclosing.id):
                    raise ValueError("등록된 프로젝트 밖의 Git 저장소로 작업 폴더를 확장할 수 없습니다.")
            return checkout
    return cwd


def enqueue(engine, prompt, cwd, session_id=None, *, key=None, parents=()):
    if engine not in ENGINES or not prompt.strip():
        raise ValueError("작업자와 작업 지시가 필요합니다.")
    if not isinstance(parents, (list, tuple)) or not all(isinstance(p, str) and p for p in parents):
        raise ValueError("상위 카드 ID 목록을 지정하세요.")
    path = project_dir(cwd)
    slug = board_for_checkout(path)
    with kb.scoped_current_board(slug), board(slug) as conn:
        card = kb.create_task(
            conn, board=slug, title=prompt.strip().splitlines()[0][:120], body=prompt,
            assignee=f"cli:{engine}", created_by="project-cli",
            workspace_kind="dir", workspace_path=str(path), session_id=session_id or None,
            max_retries=1, max_runtime_seconds=runtime_limit(), idempotency_key=key, parents=tuple(parents),
        )
        if session_id:
            # Follow Hermes' own notification settings and authenticated calling route.
            from tools.kanban_tools import _maybe_auto_subscribe
            _maybe_auto_subscribe(conn, card)
        return slug, card


def runtime_limit():
    return max(0, int(os.environ.get("HERMES_CLI_MAX_RUNTIME_SECONDS", "86400")))
