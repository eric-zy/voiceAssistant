"""Account administration from the terminal.

  python manage.py create-admin you@example.com      create an administrator; the password is typed, hidden
  python manage.py import-legacy you@example.com     copy the single-user data into that account
  python manage.py migrate-to-postgres               copy data/*.db and every user's agent.db into PostgreSQL

import-legacy copies, never moves: data/agent.db, knowledge/ and voices/ stay where they are, and nothing
already in the account is overwritten. The CosyVoice warm-up still reads voices/settings.json.
migrate-to-postgres needs VOICE_DATABASE_URL and an empty database; the SQLite files are left in place.
"""
import contextlib
import getpass
import shutil
import sqlite3
import sys
from pathlib import Path

from accounts import AccountStore
import db
import pg_migrate

ROOT = Path(__file__).resolve().parent
ACCOUNTS = ROOT / "data" / "accounts.db"
USERS = ROOT / "data" / "users"


def create_admin(store, email):
    password = getpass.getpass("密码（至少 8 位，输入时不显示）：")
    if password != getpass.getpass("再输入一次："):
        raise SystemExit("两次输入不一致，没有创建账号")
    user = store.register(email, password, is_admin=True)
    print(f"已创建管理员 {user['email']}，编号 {user['id']}")


def has_rows(database):
    """Whether an account database already holds memories or reminders worth keeping."""
    try:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return any(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("memories", "reminders") if table in tables)
    except sqlite3.Error:
        return True


def merge_folder(source, destination, skip=()):
    """Copy entries the destination lacks; never replace one it has. Returns how many were copied."""
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in source.iterdir():
        if entry.name in skip or (destination / entry.name).exists():
            continue
        if entry.is_dir():
            shutil.copytree(entry, destination / entry.name)
        else:
            shutil.copy2(entry, destination / entry.name)
        count += 1
    return count


def import_legacy(store, email, root=ROOT, users=USERS):
    """Return what was copied. Nothing the account already holds is replaced."""
    user = store.find(email)
    if user is None:
        raise SystemExit(f"没有这个账号：{email}。先用 create-admin 或页面注册创建")
    target = users / str(user["id"])
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    legacy_db, account_db = root / "data" / "agent.db", target / "agent.db"
    # Accounts in PostgreSQL mean the user's rows live there too; the store, not a global setting, says which.
    if legacy_db.is_file() and isinstance(store.path, db.Scope):
        scope = db.Scope(store.path.pool, user["id"])
        if pg_migrate.has_user_rows(scope):
            print("跳过记忆和提醒：账号里已经有了，不覆盖")
        else:
            pg_migrate.copy_user_db(legacy_db, scope)
            copied.append("记忆、提醒和操作日志")
    # Signing in creates an empty database, which must not block the import; one with data is kept.
    elif legacy_db.is_file():
        if account_db.exists() and has_rows(account_db):
            print("跳过记忆和提醒：账号里已经有了，不覆盖")
        else:
            if account_db.exists():
                account_db.replace(target / "agent.db.before-import")
            shutil.copy2(legacy_db, account_db)
            copied.append("记忆、提醒和操作日志")
    # Folders merge: voice profiles have unique ids, and a document already in the account wins.
    for folder, label, skip in (("knowledge", "知识库文件", ()), ("voices", "参考音色", ("settings.json", "settings.tmp"))):
        if (root / folder).is_dir():
            count = merge_folder(root / folder, target / folder, skip)
            if count:
                copied.append(f"{label} {count} 项")
    # The old voice choice becomes this account's personal setting, so the clone stays theirs alone.
    legacy_choice = root / "voices" / "settings.json"
    settings = target / "settings.json"
    if legacy_choice.is_file() and not settings.exists():
        shutil.copy2(legacy_choice, settings)
        copied.append("音色选择")
    return copied


def main():
    if sys.argv[1:] == ["migrate-to-postgres"]:
        if not db.configured():
            raise SystemExit("先设置 VOICE_DATABASE_URL（密码放在 PGPASSWORD 里）")
        pg_migrate.migrate(ROOT / "data", db.pool())
        print("导入完成。SQLite 文件还在原处，确认无误后可以自己移走")
        return 0
    if len(sys.argv) != 3 or sys.argv[1] not in {"create-admin", "import-legacy"}:
        print(__doc__)
        return 1
    if db.configured():
        db.migrate()
    store = AccountStore(db.target(ACCOUNTS))
    if sys.argv[1] == "create-admin":
        create_admin(store, sys.argv[2])
    else:
        copied = import_legacy(store, sys.argv[2])
        print("已复制：" + ("、".join(copied) if copied else "没有需要复制的内容"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
