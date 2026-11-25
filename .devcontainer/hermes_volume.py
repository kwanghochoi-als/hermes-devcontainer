"""Select the devcontainer's persistent Compose volume and fence offline cutovers."""

import argparse
from contextlib import ExitStack, closing
import fcntl
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
HOLD = ".devcontainer-release"
LOCK = ".devcontainer-services.lock"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
MOUNT = '{{range .Mounts}}{{if eq .Destination "/home/ubuntu/.hermes"}}{{.Name}}{{end}}{{end}}'


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def volume_name(value):
    if not NAME.fullmatch(value):
        raise ValueError("Hermes에 연결할 Docker named volume 이름을 확인하세요.")
    return value


def entry(key):
    return re.compile(r"^[ \t]*(?:export[ \t]+)?" + key + r"[ \t]*[=:].*$", re.M)


def selection():
    path = HERE / ".env"
    content = path.read_text() if path.exists() else ""
    for key in ("HERMES_SELECTED_HOME_VOLUME", "HERMES_HOME_VOLUME"):
        entries = entry(key).findall(content)
        if len(entries) > 1:
            raise ValueError(f"{path}의 {key} 항목이 중복되었습니다.")
        if entries:
            value = re.split("[=:]", entries[0], maxsplit=1)[1].strip()
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if value[:1] in ("'", '"') and value.endswith(value[0]):
                value = value[1:-1]
            return content, volume_name(value)
    return content, None


def atomic_text(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(value)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def save_selection(content, name):
    updated = content
    for key, value in (("HERMES_SELECTED_HOME_VOLUME", name), ("HERMES_HOME_VOLUME_EXTERNAL", "true")):
        field, line = entry(key), key + "=" + value
        if field.search(updated):
            updated = field.sub(line, updated)
        else:
            updated += ("\n" if updated and not updated.endswith("\n") else "") + line + "\n"
    if updated != content:
        atomic_text(HERE / ".env", updated)


def check_mount(remember):
    # Inspect this container, not another controller with a familiar name.
    actual = volume_name(docker("inspect", "--type", "container", "--format", MOUNT, socket.gethostname()))
    with ExitStack() as stack:
        if remember:
            lock = stack.enter_context((HERE / ".hermes-volume.lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX)
        content, selected = selection()
        expected = selected or os.environ.get("HERMES_HOME_VOLUME") or actual
        if actual != expected:
            raise ValueError(f"Hermes 볼륨 불일치 · 선택 {expected} · 실제 마운트 {actual}. Compose 설정으로 리빌드하세요.")
        if remember:
            save_selection(content, actual)
    return actual


# Compare replay-relevant state without printing prompts, responses or credentials.
KANBAN = {
    "tasks": ("id,status,current_run_id", "id"),
    "task_runs": ("task_id,id,profile,status,started_at,ended_at,outcome", "task_id,id"),
    "task_events": ("task_id,id,run_id,kind,payload,created_at", "task_id,id"),
}
TELEGRAM = {
    "binding": ("id,generation,bot,owner", "id"),
    "requests": ("ref,route,kind,expires,response,board,card,event,waiter", "ref"),
    "cursors": ("board,route,event", "board,route"),
    "turn_reports": ("session,turn,route,project,outcome", "session,turn,route"),
}


def read_database(stack, path):
    if not path.is_file():
        return None
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        # A cold snapshot can have committed pages only in WAL. Rebuild the SHM
        # index in a private copy without checkpointing either deployment volume.
        copy = Path(stack.enter_context(tempfile.TemporaryDirectory())) / path.name
        shutil.copyfile(path, copy)
        shutil.copyfile(wal, str(copy) + "-wal")
        uri = copy.as_uri() + "?mode=ro"
    else:
        uri = path.as_uri() + "?immutable=1"
    return stack.enter_context(closing(sqlite3.connect(uri, uri=True)))


def compare(previous, target):
    boards = {p.relative_to(root) for root in (previous, target) for p in root.glob("kanban/boards/*/kanban.db")}
    telegram = Path("cli-workers/telegram.sqlite3")
    differences = []
    for path in [Path("kanban.db"), *sorted(boards), telegram]:
        with ExitStack() as stack:
            dbs = [read_database(stack, root / path) for root in (previous, target)]
            tables = [{row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                      if db else set() for db in dbs]
            for table, (fields, order) in (TELEGRAM if path == telegram else KANBAN).items():
                query = f"SELECT {fields} FROM {table} ORDER BY {order}"
                rows = [db.execute(query) if table in schema else () for db, schema in zip(dbs, tables)]
                if any(a != b for a, b in zip_longest(*rows)):
                    differences.append(f"{path} · {table}")
    if differences:
        raise ValueError("볼륨의 카드 실행·Telegram 기록이 다릅니다. 최근 운영 데이터를 복제해 복구하세요.\n"
                         + "\n".join(differences))


def fence(target_name, homes):
    target = homes[target_name]
    sources = [path for name, path in homes.items() if name != target_name]
    with ExitStack() as stack:
        needed = set()
        for home in homes.values():
            lock = stack.enter_context((home / LOCK).open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = home / HOLD
            if marker.exists():
                state = marker.read_text().strip()
                if state.startswith("standby "):
                    needed.add(volume_name(state.removeprefix("standby ")))
                elif state != "verify":
                    raise ValueError(f"알 수 없는 Hermes 전환 상태 · {home}")
        if missing := needed - homes.keys():
            return sorted(missing)
        for home in sources:
            compare(home, target)
        for home in sources:
            atomic_text(home / HOLD, f"standby {target_name}\n")
        atomic_text(target / HOLD, "verify\n")
    return []


def reserve(home, runtime):
    # The Linux gateway and project-cli both take these exclusive flocks before
    # dispatching. Reserve them even when a dashboard or CLI tries a direct start.
    workers = home / "cli-workers"
    workers.mkdir(mode=0o700, exist_ok=True)
    with ExitStack() as stack:
        for path in (home / "gateway.lock", workers / "dispatcher.lock", runtime / "maintenance.lock"):
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            stack.callback(os.close, fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"서비스 잠금 파일을 확인하세요 · {path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(f"유지보수 대기 중 서비스가 이미 실행 중입니다 · {path}") from None
        while (home / HOLD).exists():
            time.sleep(0.25)


def select(target, controller):
    target = volume_name(target)
    with (HERE / ".hermes-volume.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        content, previous = selection()
        mounted, image, running = docker("inspect", "--type", "container", "--format",
                                         MOUNT + "|{{.Image}}|{{.State.Running}}", controller).split("|")
        if running == "true":
            raise ValueError(f"{controller}의 작업을 마치고 컨테이너를 먼저 중지하세요.")
        mounted = volume_name(mounted)
        # Between selection and rebuild, the stopped controller can still point at the old release.
        volumes = list(dict.fromkeys((target, mounted, previous or mounted)))
        if docker("ps", "-q", "--filter", "label=io.hermes.project-worker=" + controller):
            raise ValueError(f"{controller}의 실행 중인 CLI 작업을 먼저 마치세요.")
        # A standby remembers its successor even if this is a fresh checkout or a
        # formerly stopped controller. Include the latest history before reactivation.
        while True:
            args = ["docker", "run", "--rm", "-i", "--pull=never", "--network=none", "--user", "ubuntu"]
            for i, name in enumerate(volumes):
                docker("volume", "inspect", "--format", "{{.Name}}", name)
                if docker("ps", "-q", "--filter", "volume=" + name):
                    raise ValueError(f"{name} 볼륨을 사용하는 컨테이너를 먼저 중지하세요.")
                path = "/home/ubuntu/.hermes" if i == 0 else f"/previous-{i}"
                args += ["--mount", f"type=volume,source={name},target={path}"]
            args += ["--entrypoint", "python3", image, "-", "_fence", *volumes]
            missing = json.loads(subprocess.check_output(args, input=Path(__file__).read_bytes()))
            if not missing:
                break
            volumes.extend(volume_name(v) for v in missing)
        save_selection(content, target)
        print(f"선택한 Hermes 볼륨 · {target} · 검증 대기")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check the mounted volume")
    commands.add_parser("pin", help="Remember the current mount for future rebuilds")
    choose = commands.add_parser("select", help="Compare two offline volumes and stage the selected one")
    choose.add_argument("volume")
    choose.add_argument("--controller", default=os.environ.get("HERMES_CLI_CONTROLLER", "agent-cli"))
    # The helper is streamed to the existing controller image, also with a remote Docker daemon.
    offline = commands.add_parser("_fence")
    offline.add_argument("volumes", nargs="+")
    guard = commands.add_parser("_reserve")
    guard.add_argument("home", type=Path)
    guard.add_argument("runtime", type=Path)
    args = parser.parse_args()
    if args.command == "_fence":
        homes = {v: Path("/home/ubuntu/.hermes" if i == 0 else f"/previous-{i}") for i, v in enumerate(args.volumes)}
        print(json.dumps(fence(args.volumes[0], homes)))
    elif args.command == "_reserve":
        reserve(args.home, args.runtime)
    elif args.command == "select":
        select(args.volume, args.controller)
    else:
        print(check_mount(args.command == "pin"))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
