"""Run inside the disposable CLI container. Only this run's files are mounted."""
import hashlib
import fcntl
import os
from pathlib import Path
import shutil
import sys
import tempfile

io = Path("/run/hermes-job")
engine = sys.argv[1]
home = Path.home() / ("." + engine)
home.mkdir(mode=0o700, exist_ok=True)
if preferences := os.environ.pop("HERMES_CLI_MODEL_SETTINGS", ""):
    config = home / ("config.toml" if engine == "codex" else "settings.json")
    stamp = home / ".controller-settings"
    with (home / ".settings.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Only refresh a file still identical to the last controller copy.
        if not config.exists() or (stamp.is_file() and hashlib.sha256(config.read_bytes()).hexdigest() == stamp.read_text()):
            with tempfile.NamedTemporaryFile(dir=home, mode="w", delete=False) as f:
                f.write(preferences)
            os.replace(f.name, config)
            stamp.write_text(hashlib.sha256(preferences.encode()).hexdigest())
auth = io / "auth"
if auth.is_file():
    fingerprint = hashlib.sha256(auth.read_bytes()).hexdigest()
    stamp = home / ".controller-auth"
    target = home / ("auth.json" if engine == "codex" else ".credentials.json")
    if not target.is_file() or not stamp.is_file() or stamp.read_text() != fingerprint:
        with tempfile.NamedTemporaryFile(dir=home, delete=False) as f:
            with auth.open("rb") as source:
                shutil.copyfileobj(source, f)
        os.replace(f.name, target)
        stamp.write_text(fingerprint)
if (io / "legacy").is_file():
    target = home / os.environ["HERMES_LEGACY_PATH"]
    if not target.exists():
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(io / "legacy", target)
os.execvp(sys.argv[2], sys.argv[2:])
