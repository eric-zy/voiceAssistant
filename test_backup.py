import contextlib
import sqlite3
import tarfile

import backup


def test_a_backup_holds_every_file_and_a_consistent_copy_of_each_database(tmp_path):
    data = tmp_path / "data"
    (data / "users" / "1" / "english_audio").mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(data / "users" / "1" / "agent.db")) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE notes (text TEXT)")
        db.execute("INSERT INTO notes VALUES ('记住我')")
    (data / "users" / "1" / "english_audio" / "1-1.ogg").write_bytes(b"OggS...")
    (data / "users" / "1" / "agent.db-wal").write_bytes(b"leftover")
    (data / ".healthz").write_text("1")
    archive = backup.backup(data, tmp_path / "backups", stamp="20261108-120000")
    assert archive.name == "data-20261108-120000.tar.gz"
    with tarfile.open(archive) as tar:
        names = sorted(tar.getnames())
        assert names == ["users/1/agent.db", "users/1/english_audio/1-1.ogg"]
        tar.extractall(tmp_path / "restored", filter="data")
    with contextlib.closing(sqlite3.connect(tmp_path / "restored" / "users" / "1" / "agent.db")) as db:
        assert db.execute("SELECT text FROM notes").fetchall() == [("记住我",)]


def test_only_the_newest_archives_are_kept(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text("{}")
    for stamp in ("20261101-000000", "20261102-000000", "20261103-000000"):
        backup.backup(data, tmp_path / "backups", keep=2, stamp=stamp)
    assert sorted(path.name for path in (tmp_path / "backups").iterdir()) == ["data-20261102-000000.tar.gz", "data-20261103-000000.tar.gz"]
