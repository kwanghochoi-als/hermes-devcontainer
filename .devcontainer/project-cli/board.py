import os
from contextlib import closing
from pathlib import Path
import subprocess

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

BOARD = "cli-workers"
ENGINES = ("codex", "claude")
LANES = {f"cli:{engine}": engine for engine in ENGINES}


def board():
    if not kb.kanban_db_path(board=BOARD).exists():
        kb.create_board(BOARD, name="Project CLI workers")
    return closing(connect(board=BOARD))


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
            return checkout
    return cwd


def enqueue(engine, prompt, cwd, session_id=None, *, key=None):
    if engine not in ENGINES or not prompt.strip():
        raise ValueError("작업자와 작업 지시가 필요합니다.")
    path = project_dir(cwd)
    with board() as conn:
        card = kb.create_task(
            conn, board=BOARD, title=prompt.strip().splitlines()[0][:120], body=prompt,
            assignee=f"cli:{engine}", created_by="project-cli",
            workspace_kind="dir", workspace_path=str(path), session_id=session_id or None,
            max_retries=1, idempotency_key=key,
        )
        if session_id:
            # Follow Hermes' own notification settings and authenticated calling route.
            from tools.kanban_tools import _maybe_auto_subscribe
            _maybe_auto_subscribe(conn, card)
        return card
