"""Publish verified worker findings and retrieve project references for new runs."""

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

from .board import BOARD, LANES, knowledge_root, project_dir, worker_boards


def findings(items):
    if not isinstance(items, list):
        return []
    valid = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fact, evidence = item.get("fact"), item.get("evidence")
        if isinstance(fact, str) and fact.strip() and isinstance(evidence, str) and evidence.strip():
            finding = {"fact": fact.strip(), "evidence": evidence.strip()}
            if finding not in valid:
                valid.append(finding)
    return valid


def collection_name(workspace):
    path = Path(workspace)
    label = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")[:48] or "project"
    suffix = hashlib.sha256(os.fsencode(path)).hexdigest()[:12]
    return f"learned-{label}-{suffix}"


def recall(project, title, body):
    """Find past findings in the same collection used when publishing this project's runs."""
    name = collection_name(project)
    qmd = get_hermes_home() / "qmd"
    if not (qmd / "index.sqlite").is_file() or not (qmd / "knowledge" / name).is_dir():
        return []
    query = (body or "").strip()
    if title and not query.startswith(title):
        query = f"{title} {query}"
    query = " ".join(query[:1000].split())[:600]
    if not query:
        return []
    script = Path(__file__).with_name("index-knowledge.mjs")
    try:
        result = subprocess.run(
            ["node", str(script), str(qmd), name, "--recall"], input=json.dumps(query),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True, timeout=8,
            env=dict(os.environ, XDG_CACHE_HOME=str(qmd.parent), QMD_CONFIG_DIR=str(qmd)),
        )
        refs = json.loads(result.stdout)
        if not isinstance(refs, list):
            raise ValueError("Invalid QMD reference list")
        return [ref for ref in refs if isinstance(ref, dict) and isinstance(ref.get("document"), str)
                and ref["document"].startswith(f"qmd://{name}/") and (ref.get("excerpt") or ref.get("source"))][:3]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"QMD {name} 참고 자료 없이 작업 시작 · {type(exc).__name__}", file=sys.stderr, flush=True)
        return []


def search_scope(cwd=None, *, all_projects=False):
    if all_projects:
        import hermes_yaml as yaml
        config = yaml.safe_load((get_hermes_home() / "qmd/index.yml").read_text())
        return sorted(config.get("collections", {})) or ["hermes-workspace"]
    root = Path(os.environ.get("HERMES_DASHBOARD_FILES_ROOT", "/workspaces")).resolve()
    if not cwd or Path(cwd).resolve() == root or not Path(cwd).resolve().is_relative_to(root):
        return ["hermes-workspace"]
    return [collection_name(knowledge_root(project_dir(cwd)))]


def scope_query(tool_name="", args=None, session_id="", cwd=None, **kwargs):
    if tool_name != "mcp__qmd__query":
        return
    selected = (args or {}).get("collections")
    if selected and selected != ["*"]:
        return
    return {"action": "modify", "args": {"collections": search_scope(cwd, all_projects=selected == ["*"])}}


def write_note(directory, row, meta, slug):
    card, run = row["task_id"], row["id"]
    prefix = "" if slug == BOARD else f"{slug}-"
    filename = f"{prefix}{quote(card, safe='')}-r{run}.md"
    completed = datetime.fromtimestamp(row["ended_at"], timezone.utc).isoformat()
    lines = [f"# {Path(meta['workspace']).name} · {card}", "",
             f"- 프로젝트 · {meta['workspace']}", f"- 완료 · {completed}",
             f"- 보드 · {slug}", f"- 카드 · {card} / 실행 {run} / {LANES[row['profile']]}"]
    if meta.get("head"):
        lines.append(f"- Git HEAD · {meta['head']}")
    pin = meta.get("source_snapshot")
    if isinstance(pin, dict) and re.fullmatch(r"t_[a-zA-Z0-9_-]+", str(pin.get("card", ""))):
        lines.append(f"- 상위 카드 · {pin['card']}")
    for repo, sha in sorted(meta.get("source_commits", {}).items()):
        label = "로컬 HEAD" if repo in meta.get("local_sources", []) else "원격 main"
        lines.append(f"- {label} · {repo} · {sha}")
    if meta.get("knowledge"):
        lines += ["", "## 확인한 사실", ""]
    for item in meta.get("knowledge", []):
        lines.extend([item["fact"], "", f"근거 · {item['evidence']}", ""])
    content = "\n".join(lines)
    path = directory / filename
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        # There is one indexer per dispatcher lock. Only the finished .md is visible to QMD.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return filename


def read_revisions(path, session, expected):
    receipt = json.loads(path.read_text())
    commits = receipt.get("repositories", {})
    if (not session or receipt.get("session_id") != session or not isinstance(commits, dict) or not expected or commits != expected
            or not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", v)
                       for v in commits.values())):
        raise ValueError("관찰 세션의 커밋 증명을 확인할 수 없습니다.")
    return commits


@contextmanager
def collection_lock(qmd_dir, name):
    locks = qmd_dir / ".locks"
    if locks.resolve() != qmd_dir.resolve() / ".locks":
        raise ValueError(f"QMD 잠금 경로를 확인하세요 · {locks}")
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locks / name).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield locks / (name + ".cutoff")


def index_collection(qmd_dir, name, filenames, stopping, *, remove=False):
    script = Path(__file__).with_name("index-knowledge.mjs")
    command = ["setpriv", "--pdeathsig", "TERM", "--", "node", str(script), str(qmd_dir), name]
    if remove:
        command.append("--remove")
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as manifest:
        json.dump(filenames, manifest)
        manifest.seek(0)
        with subprocess.Popen(command, stdin=manifest, env=dict(os.environ, XDG_CACHE_HOME=str(qmd_dir.parent),
                                                               QMD_CONFIG_DIR=str(qmd_dir))) as proc:
            while proc.poll() is None:
                if stopping.wait(0.5):
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return False
            if proc.returncode:
                raise subprocess.CalledProcessError(proc.returncode, command)
    return True


def register_collection(workspace):
    qmd_dir = get_hermes_home() / "qmd"
    name = collection_name(workspace)
    with collection_lock(qmd_dir, name):
        (qmd_dir / "knowledge" / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        # An empty manifest registers even while Ollama is offline.
        index_collection(qmd_dir, name, [], threading.Event())
    return name


def sync_pending(conn, slug, qmd_dir, retry_after, stopping):
    # The findings and outcome share the native completion transaction. A crash anywhere
    # before the acknowledgement leaves the run eligible, even across a container restart.
    rows = conn.execute("""
        SELECT r.id, r.task_id, r.profile, r.ended_at, r.metadata FROM task_runs r
        WHERE r.outcome = 'completed' AND r.profile IN (?, ?)
          AND CASE WHEN json_valid(r.metadata) THEN (json_array_length(r.metadata, '$.knowledge') > 0
                   OR json_type(r.metadata, '$.source_commits') = 'object') END
          AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id
                          AND e.task_id = r.task_id AND e.kind = 'knowledge_indexed')
        ORDER BY r.id
    """, tuple(LANES)).fetchall()
    projects = defaultdict(list)
    for row in rows:
        meta = json.loads(row["metadata"])
        projects[collection_name(meta.get("knowledge_workspace") or meta["workspace"])].append((row, meta))

    for name in retry_after.keys() - projects.keys():
        del retry_after[name]
    for name, runs in projects.items():
        if stopping.is_set():
            return
        try:
            with collection_lock(qmd_dir, name) as cutoff:
                # A watcher can hold rows read before a concurrent project deletion.
                if slug == BOARD and cutoff.exists():
                    last_removed = int(cutoff.read_text())
                    runs = [(row, meta) for row, meta in runs if row["id"] > last_removed]
                elif slug != BOARD and (not kb.board_exists(slug) or kb.read_board_metadata(slug)["archived"]):
                    continue
                if not runs:
                    retry_after.pop(name, None)
                    continue
                directory = qmd_dir / "knowledge" / name
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                notes = [(row, write_note(directory, row, meta, slug)) for row, meta in runs]
                if time.monotonic() < retry_after.get(name, 0):
                    continue
                if not index_collection(qmd_dir, name, [filename for _, filename in notes], stopping):
                    return
            with kb.write_txn(conn):
                for row, filename in notes:
                    kb._append_event(conn, row["task_id"], "knowledge_indexed",
                                     {"document": f"qmd://{name}/{filename}"}, run_id=row["id"])
            retry_after.pop(name, None)
        except (OSError, subprocess.SubprocessError) as exc:
            retry_after[name] = time.monotonic() + 30
            print(f"QMD {name} 색인 재시도 대기 · {exc}", file=sys.stderr, flush=True)


def watch(stopping):
    while not stopping.is_set():
        try:
            with worker_boards() as refresh:
                versions = {}
                retries = defaultdict(dict)
                while not stopping.is_set():
                    connections = refresh()
                    for slug in versions.keys() - connections.keys():
                        del versions[slug], retries[slug]
                    for slug, conn in connections.items():
                        version = conn.execute("PRAGMA data_version").fetchone()[0]
                        retry_after = retries[slug]
                        if version != versions.get(slug) or any(t <= time.monotonic() for t in retry_after.values()):
                            sync_pending(conn, slug, get_hermes_home() / "qmd", retry_after, stopping)
                            versions[slug] = version
                    stopping.wait(2)
        except Exception as exc:
            print(f"QMD 완료 카드 읽기 재시도 대기 · {exc}", file=sys.stderr, flush=True)
            stopping.wait(10)
