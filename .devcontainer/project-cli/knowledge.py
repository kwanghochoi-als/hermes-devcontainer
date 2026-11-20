"""Publish worker-reported project findings from committed Kanban runs to QMD."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

from .board import LANES, board


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


def write_note(directory, row, meta):
    card, run = row["task_id"], row["id"]
    filename = f"{quote(card, safe='')}-r{run}.md"
    completed = datetime.fromtimestamp(row["ended_at"], timezone.utc).isoformat()
    lines = [f"# {Path(meta['workspace']).name} · {card}", "",
             f"- 프로젝트 · {meta['workspace']}", f"- 완료 · {completed}",
             f"- 카드 · {card} / 실행 {run} / {LANES[row['profile']]}"]
    if meta.get("head"):
        lines.append(f"- Git HEAD · {meta['head']}")
    lines += ["", "## 확인한 사실", ""]
    for item in meta["knowledge"]:
        lines.extend([item["fact"], "", f"근거 · {item['evidence']}", ""])
    content = "\n".join(lines)
    path = directory / filename
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        # There is one indexer per dispatcher lock. Only the finished .md is visible to QMD.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return filename


def index_collection(qmd_dir, name, filenames, stopping):
    script = Path(__file__).with_name("index-knowledge.mjs")
    command = ["setpriv", "--pdeathsig", "TERM", "--", "node", str(script), str(qmd_dir), name]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as manifest:
        json.dump(filenames, manifest)
        manifest.seek(0)
        with subprocess.Popen(command, stdin=manifest, env=dict(os.environ, XDG_CACHE_HOME=str(qmd_dir.parent))) as proc:
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


def sync_pending(conn, qmd_dir, retry_after, stopping):
    # The findings and outcome share the native completion transaction. A crash anywhere
    # before the acknowledgement leaves the run eligible, even across a container restart.
    rows = conn.execute("""
        SELECT r.id, r.task_id, r.profile, r.ended_at, r.metadata FROM task_runs r
        WHERE r.outcome = 'completed' AND r.profile IN (?, ?)
          AND CASE WHEN json_valid(r.metadata) THEN json_array_length(r.metadata, '$.knowledge') END > 0
          AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id
                          AND e.task_id = r.task_id AND e.kind = 'knowledge_indexed')
        ORDER BY r.id
    """, tuple(LANES)).fetchall()
    projects = defaultdict(list)
    for row in rows:
        meta = json.loads(row["metadata"])
        projects[collection_name(meta["workspace"])].append((row, meta))

    for name in retry_after.keys() - projects.keys():
        del retry_after[name]
    for name, runs in projects.items():
        if stopping.is_set():
            return
        try:
            directory = qmd_dir / "knowledge" / name
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            notes = [(row, write_note(directory, row, meta)) for row, meta in runs]
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
    retry_after = {}
    while not stopping.is_set():
        try:
            with board() as conn:
                previous_version = None
                while not stopping.is_set():
                    version = conn.execute("PRAGMA data_version").fetchone()[0]
                    if version != previous_version or any(t <= time.monotonic() for t in retry_after.values()):
                        sync_pending(conn, get_hermes_home() / "qmd", retry_after, stopping)
                        previous_version = version
                    stopping.wait(2)
        except Exception as exc:
            print(f"QMD 완료 카드 읽기 재시도 대기 · {exc}", file=sys.stderr, flush=True)
            stopping.wait(10)
