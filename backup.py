"""Back up all user data into one dated archive, keeping the newest few.

  python backup.py              -> backups/data-YYYYmmdd-HHMMSS.tar.gz
  python backup.py --keep 14    keep the 14 newest archives (default 7)

SQLite databases are copied with SQLite's own online backup, so a backup taken while people are using the service
is still consistent; every other file (recordings, knowledge files, settings) is copied as it is.
On the server: docker compose exec -T app python backup.py --keep 14   (from cron, e.g. every night)
"""
import argparse
import contextlib
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def backup(data=DATA, target=None, keep=7, stamp=None):
    target = Path(target or os.getenv("VOICE_BACKUP_DIR") or ROOT / "backups")
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f"data-{stamp or time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    partial = archive.with_suffix(".partial")
    with tempfile.TemporaryDirectory() as scratch, tarfile.open(partial, "w:gz") as tar:
        for path in sorted(data.rglob("*")):
            if not path.is_file() or path.name.endswith(("-wal", "-shm", "-journal")) or path.name == ".healthz":
                continue
            name = path.relative_to(data).as_posix()
            if path.suffix == ".db":
                copy = Path(scratch) / name.replace("/", "__")
                # closing(), not the connection's own "with": that only commits, and an open file cannot be removed.
                with contextlib.closing(sqlite3.connect(path)) as source, contextlib.closing(sqlite3.connect(copy)) as destination:
                    source.backup(destination)
                tar.add(copy, arcname=name)
            else:
                tar.add(path, arcname=name)
    partial.replace(archive)
    for old in sorted(target.glob("data-*.tar.gz"))[:-keep]:
        old.unlink()
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=int, default=7)
    made = backup(keep=parser.parse_args().keep)
    print(f"{made} ({made.stat().st_size // 1024} KB)")
