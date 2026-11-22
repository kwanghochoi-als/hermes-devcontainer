"""Delete a project's owned Hermes resources while preserving its Git checkouts."""

from contextlib import closing, ExitStack
import fcntl
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time

from hermes_cli import kanban_db as kb, projects_db as pdb
from hermes_cli.kanban_db_connect import connect
from hermes_constants import get_hermes_home

from .board import BOARD, LANES
from .knowledge import collection_lock, collection_name, index_collection


def default_board_for_new_project(conn, slug):
    name = slug
    generation = 1
    while pdb._get_meta(conn, f"project-cli.retired-board.{name}") or (generation > 1 and kb.board_exists(name)):
        generation += 1
        suffix = f"-{generation}"
        name = slug[:64 - len(suffix)] + suffix
    return name


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


def session_plan(conn, project, home, sessions, owners):
    from tui_gateway.git_probe import common_repo_root

    starter = pdb._get_meta(conn, f"project-cli.session.{project.id}")
    others = {r[0] for r in conn.execute("SELECT value FROM project_meta WHERE key GLOB 'project-cli.session.*' AND key!=?",
                                          (f"project-cli.session.{project.id}",))}
    with closing(sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT id,cwd,git_repo_root FROM sessions").fetchall()
        guards = {r[0] for r in db.execute(
            "SELECT conversation_id FROM session_turn_leases WHERE expires_at>? "
            "UNION SELECT session_id FROM compression_locks WHERE expires_at>?", (time.time(), time.time()))}
    claimed, protected = set(), set(others)
    for sid, cwd, repo in rows:
        scope = owners(cwd) or owners(repo)
        if not scope and cwd and not repo:
            scope = owners(common_repo_root(cwd))
        if sid not in others and (scope == {project.id} or (sid == starter and (not scope or project.id in scope))):
            claimed.add(sid)
        elif scope or cwd or repo:
            protected.add(sid)

    plan = {}
    for sid in sorted(claimed):
        targets = set(sessions.delete_targets(sid))
        if targets & protected:
            raise ValueError(f"다른 프로젝트와 연결된 대화 계보를 먼저 분리하세요 · {sid}")
        busy = targets & guards
        if busy:
            raise ValueError("실행 중인 Hermes 세션을 먼저 종료하세요 · " + ", ".join(sorted(busy)))
        plan[sid] = targets
    return plan


def delete_sessions(store, plan, home):
    from gateway.session_ingress_media import native_inputs_root
    from session_retention import after_sessions_removed

    removed = set()
    for sid, targets in plan.items():
        if sid in removed:
            continue
        result = store.sessions.delete(sid, expected_ids=sorted(targets - removed), refuse_guarded=True,
                                       media_root=str(native_inputs_root()))
        if not result and store.sessions.get(sid):
            raise ValueError(f"세션 상태가 변경되었습니다. 삭제를 다시 실행하세요 · {sid}")
        after_sessions_removed(store, home, result, reason="delete", primary=sid, sessions_dir=home / "sessions")
        removed.update(result)
    return removed


def remove_artifact(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def checked_path(root, *parts):
    path = root.joinpath(*parts)
    if path.parent.resolve() != root.resolve().joinpath(*parts[:-1]):
        raise ValueError(f"저장 경로가 관리 폴더 밖을 가리킵니다 · {path}")
    return path


def dedicated_boards(conn, project):
    other_bindings = {row[0] for row in conn.execute("SELECT board_slug FROM projects WHERE id!=?", (project.id,))}
    root = kb.boards_root()
    result = {}
    # A manually archived board has both a metadata-only tombstone and an archive.
    for meta in (*root.glob("*/board.json"), *root.glob("_archived/*/board.json")):
        bound = meta.parent == root / (project.board_slug or "")
        if meta.is_symlink() or meta.parent.resolve() != root.resolve() / meta.parent.relative_to(root):
            if bound:
                raise ValueError(f"프로젝트 보드의 저장 경로를 확인하세요 · {meta.parent}")
            continue
        try:
            data = json.loads(meta.read_text())
        except (OSError, ValueError):
            if bound:
                raise
            continue
        slug = data.get("slug")
        if data.get("project_id") == project.id and slug and slug not in (BOARD, kb.DEFAULT_BOARD):
            if slug in other_bindings:
                raise ValueError(f"다른 프로젝트도 이 전용 보드에 연결되어 있습니다 · {slug}")
            kb._require_slug(slug)
            if meta.parent.parent == root and meta.parent.name != slug:
                raise ValueError(f"보드 이름과 저장 경로가 일치하지 않습니다 · {meta.parent}")
            result[meta.parent] = data
    return result


def fence_board(cards, slug):
    active = cards.execute("SELECT id FROM tasks WHERE status='running' ORDER BY id").fetchall()
    if active:
        raise ValueError(f"실행 중인 카드를 먼저 종료하세요 · {slug} · " + ", ".join(r["id"] for r in active))


def worker_session_keys(cards, meta, owners, pid):
    from tui_gateway.git_probe import common_repo_root
    from .worker import digest

    keys = set()
    for row in cards.execute("""
        SELECT t.id, t.session_id, t.workspace_kind, t.workspace_path, t.assignee, r.id AS run,
               CASE WHEN json_valid(r.metadata) THEN json_extract(r.metadata,'$.workspace') END AS run_cwd
        FROM tasks t LEFT JOIN task_runs r ON t.id=r.task_id WHERE t.assignee IN (?,?)
    """, tuple(LANES)):
        cwd = row["workspace_path"]
        if row["workspace_kind"] == "worktree" and meta.get("default_workdir"):
            cwd = str(Path(meta["default_workdir"]) / ".worktrees" / row["id"])
        cwd = row["run_cwd"] or cwd
        if not cwd or (owners(cwd) or owners(common_repo_root(cwd))) != {pid}:
            continue
        key = digest(cwd, row["session_id"] or row["id"], LANES[row["assignee"]])
        keys.add(key)
        if row["run"] is not None:
            keys.add(digest(key, row["run"]))
    return keys


def delete_board_files(directory):
    # Native boards may store *actual Git worktrees* in workspaces/. Keep those
    # paths in place, even when the worktree contains uncommitted changes.
    for path in directory.glob("kanban.db*"):
        if path.is_symlink() or path.is_file():
            path.unlink()
    for name in ("attachments", "logs"):
        remove_artifact(directory / name)
    (directory / "board.json").unlink(missing_ok=True)
    if not any(directory.iterdir()):
        directory.rmdir()


def remove_knowledge(home, collections):
    qmd = home / "qmd"
    # Pending legacy shared-board runs must not republish the old findings.
    shared_db = kb.board_dir(BOARD) / "kanban.db"
    cutoff = 0
    if collections and shared_db.is_file():
        with closing(sqlite3.connect(shared_db.as_uri() + "?mode=ro", uri=True)) as shared:
            cutoff = shared.execute("SELECT COALESCE(MAX(id),0) FROM task_runs").fetchone()[0]
    for name, fence in collections.items():
        notes = checked_path(home, "qmd", "knowledge", name)
        if (qmd / "index.yml").exists() or (qmd / "index.sqlite").exists():
            index_collection(qmd, name, [], threading.Event(), remove=True)
        if cutoff:
            temp = fence.with_suffix(".tmp")
            temp.write_text(str(cutoff))
            temp.replace(fence)
        remove_artifact(notes)


def forget_telegram(slugs, session_ids):
    from . import telegram
    if telegram.database().is_file():
        with telegram.store(telegram.database()) as tg:
            for slug in slugs:
                tg.execute("DELETE FROM requests WHERE board=?", (slug,))
                tg.execute("DELETE FROM cursors WHERE board=?", (slug,))
            tg.executemany("DELETE FROM turn_reports WHERE session=?", [(sid,) for sid in session_ids])


def delete_project(conn, project_id):
    from persistence.core.errors import SessionWriteGuarded

    try:
        return remove_owned_data(conn, project_id)
    except SessionWriteGuarded as exc:
        raise ValueError(f"실행 중인 Hermes 세션을 종료한 뒤 삭제를 다시 실행하세요 · {exc}") from exc
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        raise ValueError(f"프로젝트 삭제가 완료되지 않았습니다 · {project_id} · {exc}\n문제를 해결한 뒤 같은 ID로 다시 실행하세요.") from exc


def remove_owned_data(conn, project_id):
    from persistence_assembly import open_persistence, profile_scope

    kb._assert_not_delegated_child_mutation()
    home = get_hermes_home().resolve()
    registry = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not registry or Path(registry).resolve() != (home / "projects.db").resolve():
        raise ValueError("프로젝트를 등록한 Hermes 프로필에서 삭제를 실행하세요.")
    qmd = home / "qmd"
    with (home / ".project-create.lock").open("a") as lock, ExitStack() as locks:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pdb.write_txn(conn):
            project = pdb.get_project(conn, project_id)
            if not project or project.id != project_id:
                return False
            owners = path_owners(conn)
            boards = dedicated_boards(conn, project)
            slugs = {meta["slug"] for meta in boards.values()}
            binding = project.board_slug
            # A retry can see only the registration. Reserve its old name, but
            # use verified board metadata for every filesystem and approval deletion.
            if (binding and binding not in (BOARD, kb.DEFAULT_BOARD) and not kb.board_exists(binding)
                    and not conn.execute("SELECT 1 FROM projects WHERE id!=? AND board_slug=?", (project.id, binding)).fetchone()):
                pdb._upsert_meta_locked(conn, f"project-cli.retired-board.{binding}", project.id)
            linked = {folder.path for folder in project.folders if owners(folder.path) == {project.id}}
            knowledge_keys = f"project-cli.knowledge.{project.id}.*"
            old_paths = (r[0] for r in conn.execute("SELECT value FROM project_meta WHERE key GLOB ?", (knowledge_keys,)))
            paths = linked | {path for path in old_paths if not owners(path) - {project.id}}
            collections = {}
            for path in sorted(paths):
                name = collection_name(path)
                if qmd.is_dir():
                    if qmd.is_symlink():
                        raise ValueError(f"QMD 저장 경로를 확인하세요 · {qmd}")
                    collections[name] = locks.enter_context(collection_lock(qmd, name))

            with open_persistence(profile_scope(home=home)) as store:
                with ExitStack() as writers:
                    opened = {}
                    keys = set()
                    for directory, meta in boards.items():
                        db = directory / "kanban.db"
                        if db.is_symlink():
                            raise ValueError(f"보드 데이터베이스 경로를 확인하세요 · {db}")
                        if db.is_file():
                            cards = writers.enter_context(closing(connect(db_path=db, board=meta["slug"])))
                            writers.enter_context(kb.write_txn(cards))
                            fence_board(cards, meta["slug"])
                            keys.update(worker_session_keys(cards, meta, owners, project.id))
                            opened[directory] = cards

                    plan = session_plan(conn, project, home, store.sessions, owners) if (home / "state.db").is_file() else {}
                    remove_knowledge(home, collections)
                    for cards in opened.values():
                        # Fence workers which already hold a handle to the old database.
                        cards.execute("UPDATE tasks SET status='archived' WHERE status NOT IN ('done','archived','cancelled')")
                    for slug in slugs:
                        if kb.board_exists(slug) and kb.read_board_metadata(slug).get("project_id") == project.id:
                            kb.write_board_metadata(slug, archived=True)

                # Telegram decisions lock their request first and their board second.
                # Release the fenced board before waiting for a concurrent decision.
                forget_telegram(slugs, set().union(*plan.values()))
                delete_sessions(store, plan, home)

            for path in paths:
                key = hashlib.sha256(path.encode()).hexdigest()
                remove_artifact(checked_path(home, "cli-workers", "homes", key))
            for key in keys:
                remove_artifact(checked_path(home, "cli-workers", "sessions", key))
            for slug in slugs:
                remove_artifact(checked_path(home, "cli-workers", "boards", slug))
                pdb._upsert_meta_locked(conn, f"project-cli.retired-board.{slug}", project.id)
                if kb.get_current_board() == slug:
                    kb.clear_current_board()
            for directory in boards:
                delete_board_files(directory)
            for path in linked:
                pdb._upsert_meta_locked(conn, "project-cli.hidden-root." + path, path)
            conn.execute("DELETE FROM project_meta WHERE key=? OR key GLOB ? OR (key='active_id' AND value=?)",
                         (f"project-cli.session.{project_id}", knowledge_keys, project_id))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return True


def hidden_project_paths():
    with pdb.connect_closing() as conn:
        roots = {Path(row[0]) for row in conn.execute("SELECT value FROM project_meta WHERE key GLOB 'project-cli.hidden-root.*'")}

    def hidden(path):
        p = Path(path)
        return bool(roots) and (p in roots or any(parent in roots for parent in p.parents))

    return hidden
