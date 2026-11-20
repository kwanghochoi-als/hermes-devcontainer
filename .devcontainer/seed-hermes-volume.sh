#!/usr/bin/env bash
# Copy the legacy container layer before the first rebuild. Run again to refresh
# this container's seed while the target volume is still unused.
set -euo pipefail
source_container=hermes-agent-cli
source_id=$(docker inspect -f '{{.Id}}' "$source_container")
project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$source_container")
[[ -n "$project" && "$project" != '<no value>' ]]
volume="${project}_hermes_home"
mounted=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/ubuntu/.hermes"}}{{.Type}}:{{index . "Name"}}{{end}}{{end}}' "$source_container")
if [[ "$mounted" == "volume:$volume" ]]; then
  echo "이미 연결된 볼륨 · $volume"
  exit 0
fi
if [[ -n "$mounted" || -n "$(docker ps -aq --filter "volume=$volume")" ]]; then
  echo "기존 마운트 또는 사용 중인 볼륨을 확인하세요 · $volume" >&2
  exit 1
fi
docker volume create --label "com.docker.compose.project=$project" \
  --label "com.docker.compose.volume=hermes_home" "$volume" >/dev/null
image=$(docker inspect -f '{{.Image}}' "$source_container")
helper=$(docker run --rm -d --network none --user 0:0 --entrypoint /bin/sleep \
  --mount "type=volume,src=$volume,dst=/target,volume-nocopy" "$image" infinity)
stage=""
cleanup() {
  if [[ -n "$stage" ]]; then docker exec "$helper" rm -rf -- "$stage" >/dev/null || true; fi
  docker rm -f "$helper" >/dev/null || true
}
trap cleanup EXIT
stage=$(docker exec "$helper" bash -eu -c '
  if [[ -n "$(ls -A /target)" ]] && [[ "$(cat /target/.devcontainer-seed 2>/dev/null)" != "$1" ]]; then
    echo "대상 볼륨에 다른 데이터가 있습니다." >&2; exit 1
  fi
  mktemp -d /target/.seed-new.XXXXXXXX' -- "$source_id")
# Archive directory entries separately so gateway activity in the root directory
# does not invalidate a long copy. Runtime sockets have no state to restore.
docker exec "$source_container" bash -euo pipefail -c '
  cd /home/ubuntu/.hermes
  find . ! -type s -print0 | tar --no-recursion --null \
    --exclude="*-wal" --exclude="*-shm" --exclude="*-journal" -cpf - -T -
' | docker exec -i "$helper" tar --numeric-owner -C "$stage" -xpf -

# Replace mutable SQLite files with online snapshots. This includes committed
# WAL pages while the running container and all its credentials stay in place.
docker exec -i -u ubuntu "$source_container" python3 - <<'PY' | docker exec -i "$helper" tar -C "$stage" -xpf -
import os, pathlib, sqlite3, sys, tarfile, tempfile
from contextlib import closing
root = pathlib.Path("/home/ubuntu/.hermes")
with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    for directory, dirs, files in os.walk(root):
        if pathlib.Path(directory) == root:
            dirs[:] = [p for p in dirs if p not in {"hermes-agent", "installs", "tools"}]
        for name in files:
            path = pathlib.Path(directory, name)
            if path.is_symlink() or path.suffix not in {".db", ".sqlite", ".sqlite3"}:
                continue
            with path.open("rb") as f:
                if f.read(16) != b"SQLite format 3\0":
                    continue
            copy = pathlib.Path(tmp, "copy")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(copy)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError(f"SQLite snapshot failed for {path.name}")
            copy.chmod(path.stat().st_mode & 0o777)
            archive.add(copy, arcname=str(path.relative_to(root)))
            copy.unlink()
PY
docker exec "$helper" bash -eu -c '
  shopt -s dotglob nullglob
  stage=$1
  old=$(mktemp -d /target/.seed-old.XXXXXXXX)
  for p in /target/*; do
    [[ "$p" == "$stage" || "$p" == "$old" ]] || mv -- "$p" "$old/"
  done
  for p in "$stage"/*; do mv -- "$p" /target/; done
  chown --reference="$stage" /target
  chmod --reference="$stage" /target
  printf "%s\n" "$2" > /target/.devcontainer-seed
  rm -rf -- "$old"
' -- "$stage" "$source_id"
echo "Hermes 데이터 복사 및 SQLite 검증 완료 · $volume"
