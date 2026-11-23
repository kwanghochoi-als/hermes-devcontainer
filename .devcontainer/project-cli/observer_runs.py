"""Bind Kanban executions to immutable, project-owned Git snapshots."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

from .board import EnvironmentActionRequired
from . import observer


@dataclass(frozen=True)
class Snapshot:
    directory: Path
    commits: dict
    local_sources: list
    reference: dict


def slot(runs, card, generation):
    if not re.fullmatch(r"t_[a-zA-Z0-9_-]+", card) or not re.fullmatch(r"(?:[1-9][0-9]*|split(?:-[0-9a-f]{32})?)", generation):
        raise EnvironmentActionRequired("상위 카드의 스냅샷 식별자를 확인하세요.")
    path = runs / card / generation
    if path.resolve() != path:
        raise EnvironmentActionRequired("상위 카드의 스냅샷 저장 경로를 확인하세요.")
    return path


def load_snapshot(runs, project, ref, *, publishing=False):
    try:
        path = slot(runs, ref["card"], ref["generation"]) / "sources"
        if path.resolve(strict=True) != path or (path / "project").read_text() != project.id:
            raise ValueError("project")
        commits = json.loads((path / "commits.json").read_text())
        local = json.loads((path / "local-sources.json").read_text())
        if (not isinstance(commits, dict) or not commits or not isinstance(local, list) or not set(local) <= commits.keys()
                or any(not observer.NAME.fullmatch(n) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
                       or (path / (n + ".git")).resolve(strict=True) != path / (n + ".git") for n, sha in commits.items())):
            raise ValueError("manifest")
        data = json.dumps([project.id, commits, sorted(local)], sort_keys=True).encode()
        digest = hashlib.sha256(data).hexdigest()
        if not publishing and ref.get("digest") != digest:
            raise ValueError("digest")
        return Snapshot(path, commits, local, {**ref, "digest": digest})
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise EnvironmentActionRequired("상위 카드의 고정 Git 스냅샷을 확인할 수 없습니다. 해당 실행의 스냅샷을 복구하세요.") from exc


def capture(runs, project, card, generation):
    folder = slot(runs, card, generation)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    ref = {"card": card, "generation": generation}
    # Publish only after every repository was fetched and its commit was recorded.
    with (folder / ".capture.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (folder / "sources").exists():
            with tempfile.TemporaryDirectory(dir=folder) as temp:
                staging = Path(temp) / "sources"
                staging.mkdir()
                observer.prepare(project, staging)
                (staging / "project").write_text(project.id)
                staging.rename(folder / "sources")
        return load_snapshot(runs, project, ref, publishing=True)


def parent_snapshot(conn, runs, project, card):
    parent = kb.get_task(conn, card)
    if not parent or (parent.project_id and parent.project_id != project.id):
        raise EnvironmentActionRequired(f"{card} · 같은 프로젝트의 상위 카드를 지정하세요.")
    run = None
    if parent.status == "running" and parent.current_run_id:
        run = kb.get_run(conn, parent.current_run_id)
    elif parent.status in ("done", "archived"):
        completed = kb.list_runs(conn, card, state_type="outcome", state_name="completed")
        run = completed[-1] if completed else None
    if not run or run.task_id != card:
        raise EnvironmentActionRequired(f"{card} · 상위 카드에서 Git 스냅샷을 먼저 확정하세요.")
    folder = slot(runs, card, str(run.id))
    pointer = folder / "snapshot-ref.json"
    meta = run.metadata or {}
    if pointer.exists() or "source_snapshot" in meta:
        try:
            ref = json.loads(pointer.read_text()) if pointer.exists() else meta["source_snapshot"]
            snapshot = load_snapshot(runs, project, ref)
            if (meta.get("source_snapshot", ref) != ref or meta.get("source_commits", snapshot.commits) != snapshot.commits):
                raise ValueError("receipt")
            return snapshot
        except (OSError, ValueError) as exc:
            raise EnvironmentActionRequired(f"{card} · 상위 실행의 스냅샷 기록을 확인하세요.") from exc
    raise EnvironmentActionRequired(f"{card} · 상위 실행의 고정 Git 스냅샷이 없습니다. 상위 카드를 다시 실행하세요.")


def same_snapshot(snapshots):
    first = snapshots[0]
    if any(s.reference["digest"] != first.reference["digest"] for s in snapshots[1:]):
        raise EnvironmentActionRequired("상위 카드들의 저장소 또는 커밋이 서로 다릅니다. 같은 스냅샷을 사용한 상위 카드로 연결하세요.")
    return first


def parents_of(conn, card, excluding=()):
    parents = set(kb.parent_ids(conn, card))
    row = conn.execute("SELECT json_extract(payload,'$.creator_task_id') FROM task_events "
                       "WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1", (card,)).fetchone()
    if row and row[0]:
        parents.add(row[0])
    return sorted(parents - set(excluding))


def linked_snapshot(conn, runs, project, card, parents):
    # A retry after an operator answer belongs to its original build. A
    # deliberate dashboard rebuild invalidates descendants in the same txn.
    prior = conn.execute("""
        SELECT kind, payload FROM task_events WHERE task_id=? AND (kind='descendant_invalidated'
          OR (kind='cli_checkpoint' AND json_extract(payload,'$.step')='source_pin'))
        ORDER BY id DESC LIMIT 1
    """, (card,)).fetchone()
    snapshots = []
    if prior and prior["kind"] == "cli_checkpoint":
        checkpoint = json.loads(prior["payload"])
        ref = checkpoint["source_snapshot"]
        if ref["card"] != card:
            snapshots.append(load_snapshot(runs, project, ref))
            parents = set(parents) - set(checkpoint.get("source_parents", []))
    snapshots.extend(parent_snapshot(conn, runs, project, p) for p in sorted(parents))
    return same_snapshot(snapshots)


def split_event(conn, card):
    # In native decomposition every member is a dependency OF the final root.
    # The durable decomposed event records the ownership in the other direction.
    return conn.execute("""
        SELECT id, task_id, payload FROM task_events WHERE kind='decomposed' AND
          (task_id=? OR EXISTS (SELECT 1 FROM json_each(task_events.payload,'$.child_ids') WHERE value=?))
        ORDER BY id DESC LIMIT 1
    """, (card, card)).fetchone()


def for_task(conn, runs, project, card, generation, *, parents=None):
    event = split_event(conn, card)
    if not event:
        parents = parents_of(conn, card) if parents is None else parents
        if parents:
            return linked_snapshot(conn, runs, project, card, parents)
        return capture(runs, observer.refresh_sources(project), card, generation)
    group = json.loads(event["payload"])
    if "source_snapshot" not in group:
        group = inherit_deferred_split(conn, runs, project, event)
    snapshot = load_snapshot(runs, project, group["source_snapshot"])
    members = {event["task_id"], *group["child_ids"]}
    external = set(parents_of(conn, event["task_id"], excluding=members))
    if event["task_id"] != card:
        external.update(parents_of(conn, card, excluding=members))
    external.difference_update(group.get("snapshot_parents", ()))
    if external:
        same_snapshot([snapshot, *(parent_snapshot(conn, runs, project, p) for p in sorted(external))])
    return snapshot


def inherit_deferred_split(conn, runs, project, event):
    # The planner can split a task while its upstream build is still queued.
    root = event["task_id"]
    folder = slot(runs, root, "split")
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".group.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        group = json.loads(conn.execute("SELECT payload FROM task_events WHERE id=?", (event["id"],)).fetchone()[0])
        if "source_snapshot" in group:
            return group
        if not group.get("source_parents"):
            raise EnvironmentActionRequired(f"{root} · 분해 시 확정한 Git 스냅샷이 없습니다. 새 상위 작업에서 분해를 시작하세요.")
        parents = set(group["source_parents"]) | set(parents_of(conn, root, excluding=group["child_ids"]))
        snapshot = same_snapshot([parent_snapshot(conn, runs, project, p) for p in sorted(parents)])
        group["source_snapshot"] = snapshot.reference
        group["snapshot_parents"] = sorted(parents)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE id=?",
                         (json.dumps(group), event["id"]))
        return group


@contextmanager
def decomposition_sources(conn, card):
    """Pin before the native fan-out transaction makes any children visible."""
    from hermes_cli import projects_db as pdb
    from .attempts import log_board
    slug = log_board(conn)
    meta = kb.read_board_metadata(slug) if slug else {}
    if not meta.get("project_id"):
        yield {}
        return
    with pdb.connect_closing() as db:
        project = pdb.get_project(db, meta["project_id"])
    if not project or not project.readonly:
        yield {}
        return
    if project.archived or project.board_slug != slug or meta.get("archived"):
        raise EnvironmentActionRequired("읽기 전용 프로젝트의 보드 연결을 확인하세요.")
    runs = get_hermes_home() / "cli-workers" / "boards" / slug / "runs"
    folder = slot(runs, card, "split")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder / ".group.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        task = kb.get_task(conn, card)
        if (not task or task.status != "triage" or conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='decomposed'", (card,)).fetchone()):
            yield {}
            return
        if task.project_id and task.project_id != project.id:
            raise EnvironmentActionRequired("분해할 상위 카드의 소유 프로젝트를 확인하세요.")
        waiting = kb.unsatisfied_parents(conn, card)
        if waiting:
            # Planning may precede the upstream build. Gate the new members on those
            # same prerequisites so they inherit a real pin when their first worker starts.
            details = {"source_parents": [pid for pid, _status in waiting]}
            enclosing = split_event(conn, card)
            if enclosing and (ref := json.loads(enclosing["payload"]).get("source_snapshot")):
                details["source_snapshot"] = load_snapshot(runs, project, ref).reference
            yield details
            return
        generation = "split-" + uuid4().hex
        try:
            try:
                parents = parents_of(conn, card)
                snapshot = for_task(conn, runs, project, card, generation, parents=parents)
            except EnvironmentActionRequired as exc:
                with kb.write_txn(conn):
                    changed = conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' "
                                           "WHERE id=? AND status='triage'", (card,)).rowcount
                    if changed:
                        kb._append_event(conn, card, "blocked", {"kind": "capability", "request_kind": "environment",
                                         "reason": str(exc), "resume_status": "triage"})
                if changed:
                    kb._fire_task_hook("kanban_task_blocked", kb.get_task(conn, card), card, None, reason=str(exc))
                raise ValueError(f"Git 스냅샷 환경 조치 후 상위 카드를 다시 분해하세요 · {exc}") from exc
            yield {"source_snapshot": snapshot.reference, "snapshot_parents": parents}
        finally:
            # Native fan-out may lose a race, roll back, or be re-triaged later.
            # Only the directory allocated by this invocation is eligible for cleanup.
            created = slot(runs, card, generation)
            if created.exists() and not conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='decomposed' "
                    "AND json_extract(payload,'$.source_snapshot.generation')=?", (card, generation)).fetchone():
                shutil.rmtree(created)
