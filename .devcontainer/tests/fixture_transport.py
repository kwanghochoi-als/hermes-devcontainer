"""Local process boundary for Hermeses using temporary directories and fixture CLIs."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import tempfile

IO = "/run/hermes-job"


def install(profile):
    plugin = Path(__file__).resolve().parents[1] / "project-cli"
    directory = profile / "plugins/project-cli"
    directory.mkdir()
    for file in plugin.iterdir():
        if file.is_file():
            (directory / file.name).symlink_to(Path(__file__) if file.name == "isolation.py" else file)


def reap_checkout(cwd):
    pass


@contextmanager
def launch(engine, cli, cwd, project, files, prompt_text, lock, session, observer, attachments):
    assert not observer
    with tempfile.TemporaryFile() as prompt, (files / "events.jsonl").open("wb") as out, (files / "stderr.log").open("wb") as err:
        prompt.write(prompt_text.encode())
        prompt.seek(0)
        proc = subprocess.Popen(["setpriv", "--pdeathsig", "TERM", "--", *cli], cwd=cwd, stdin=prompt, stdout=out,
                                stderr=err, start_new_session=True, pass_fds=(lock.fileno(),))
        try:
            yield proc
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
