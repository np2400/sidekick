"""Rehearse the pending migrations on a COPY of a database and prove they're safe, before production gets them.

    python -m app.db.rehearse /rehearsal/sidekick.db      # e.g. a copy Litestream restored from the S3 backup

It never touches the given file: it migrates a second copy next to it, checks it, and deletes it. Checks:
  - every table that existed before is identical (schema, indexes, rows, content): the previous version of
    Sidekick still works on the migrated database, so a code-only rollback is safe
  - one account per person, every login points to an existing account
  - isolation: each account reaches exactly the rows its Telegram user owned before, and no row is shared or lost
  - saved messages moved into conversations of their own account, word for word, and stored encrypted
  - SQLite's integrity and foreign-key checks pass, and migrating twice changes nothing
Prints counts and the last 4 digits of ids only, never anything anyone wrote. Exit code 1 if a check fails.
Without SECRETS_KEY it encrypts the copy with a throwaway key: that proves the round trip without the real key.
"""
import hashlib
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from app import db, memory, users, vault
from app.account import PERSONAL
from app.config import DB_PATH

NEW_TABLES = {"accounts", "identities", "conversations", "chat_messages",  # 009, 010
              "sessions", "link_codes", "login_states", "pending_links",  # 011
              "jobs", "job_effects"}  # 012: the rest must not change


def open_db(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def snapshot(conn) -> dict:
    """Each table's schema, indexes, row count and a fingerprint of every row."""
    out = {}
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                        "AND name NOT LIKE 'sqlite_%' AND name != 'schema_version' ORDER BY name")]
    for table in names:
        digest, rows = hashlib.sha256(), 0
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            digest.update(repr(tuple(row)).encode())
            rows += 1
        out[table] = {
            "schema": conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                                   (table,)).fetchone()[0],
            "indexes": sorted(r[0] or "" for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,))),
            "rows": rows, "hash": digest.hexdigest()}
    return out


def owned_rows(conn, tables: dict) -> dict:
    """{table: {person id: rows}} for every per-person table that existed before 009, plus deliveries via their
    reminder. The None key holds rows that belong to nobody (e.g. an invite nobody used yet)."""
    out = {}
    for table, column in PERSONAL.items():
        if table in tables and table not in NEW_TABLES:
            out[table] = dict(conn.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}").fetchall())
    if "deliveries" in tables:
        out["deliveries"] = dict(conn.execute("SELECT r.chat_id, COUNT(*) FROM deliveries d "
                                              "JOIN reminders r ON r.id = d.reminder_id GROUP BY r.chat_id").fetchall())
    return out


def mask(person) -> str:
    return f"…{str(person)[-4:]}"


def same_message(old, copy) -> bool:
    """An old-table message and its copy: same account, conversation of that account, same words and flags."""
    if copy is None or not (copy["account_id"] == copy["owner"] == old["chat_id"]):
        return False
    try:
        words = vault.unlock_text(copy["text_encrypted"])
    except vault.VaultError:
        return False
    return (words, copy["role"], copy["sensitive"], copy["created_at"]) == (
        old["text"], old["role"], old["sensitive"], old["created_at"])


def rehearse(source: Path, owner_telegram_id: str = "") -> bool:
    if source.resolve() == Path(DB_PATH).resolve():
        raise SystemExit("Refusing: that's the live database. Rehearse on a restored copy.")
    work = source.with_name(source.stem + "-rehearsal.db")
    leftovers = (work, Path(f"{work}-wal"), Path(f"{work}-shm"))
    for path in leftovers:
        path.unlink(missing_ok=True)
    with open_db(source) as original, open_db(work) as copy:
        original.backup(copy)  # a consistent copy, even if the source were in use
    results = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{f': {detail}' if detail else ''}")

    try:
        with open_db(work) as conn:
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            before = snapshot(conn)
            newest = conn.execute("SELECT MAX(created_at) FROM traces").fetchone()[0] if "traces" in before else None
            owned_before = owned_rows(conn, before)
        print(f"Copy of {source.name}: schema version {version}, {sum(t['rows'] for t in before.values())} rows "
              f"in {len(before)} tables, newest activity {newest or 'none'} UTC")

        db.init(work)  # the real migrations, exactly as production will run them, then what startup does
        owner = users.ensure_owner(int(owner_telegram_id)) if owner_telegram_id.isdigit() else None
        check(users.sync_from_legacy(owner_telegram_id) == 0, "accounts agree with the users table at startup")
        if not vault.SECRETS_KEY:
            vault.SECRETS_KEY = Fernet.generate_key().decode()
        memory.import_legacy()
        with open_db(work) as conn:
            after = snapshot(conn)
            changed = [t for t in before if after.get(t) != before[t]]
            check(not changed, "every existing table is unchanged (so the previous version still runs on it)",
                  f"changed: {changed}" if changed else f"{len(before)} tables: schema, indexes and every row")

            accounts = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM accounts")}
            logins = conn.execute("SELECT provider, subject, account_id FROM identities").fetchall()
            telegram = {int(r["subject"]): r["account_id"] for r in logins if r["provider"] == "telegram"}
            members = conn.execute("SELECT telegram_id, status FROM users").fetchall()
            wrong = [mask(m["telegram_id"]) for m in members
                     if accounts.get(telegram.get(m["telegram_id"])) != m["status"]]
            check(not wrong, "every member has an account with the same status",
                  f"wrong: {wrong}" if wrong else f"{len(members)} members")
            if owner is not None:
                check(owner in accounts and users.telegram_chat(owner) == int(owner_telegram_id),
                      "the owner has an account, reached through their Telegram login")
            dangling = [mask(r["subject"]) for r in logins if r["account_id"] not in accounts]
            check(not dangling, "every login points to an existing account",
                  f"dangling: {dangling}" if dangling else f"{len(logins)} logins")
            check(len(set(telegram.values())) == len(telegram), "no account is shared by two Telegram users")

            owned_after = owned_rows(conn, after)
            orphans, broken = {}, []
            for table, by_person in owned_before.items():
                for person, rows in by_person.items():
                    if person is None:
                        continue  # nobody's, e.g. an unused invite
                    account = telegram.get(person) if isinstance(person, int) else None
                    if account is None:
                        orphans.setdefault(person, {})[table] = rows
                    elif owned_after[table].get(account) != rows or users.telegram_chat(account) != person:
                        broken.append((table, mask(person)))
            lost = [t for t, by_account in owned_after.items() if t != "deliveries" and after[t]["rows"] != (
                sum(n for a, n in by_account.items() if a in accounts) + by_account.get(None, 0)
                + sum(o.get(t, 0) for o in orphans.values()))]
            check(not broken and not lost, "isolation: each account reaches exactly its own rows, none shared or lost",
                  f"{len(accounts)} accounts" if not (broken or lost) else f"mismatch: {broken} {lost}")
            for person, tables in orphans.items():  # unreachable rows of someone without an account: reported only
                print(f"note  rows that belong to no account ({mask(person)}): {tables}")

            old = conn.execute("SELECT id, chat_id, role, text, sensitive, created_at FROM messages").fetchall()
            copies = {r["legacy_id"]: r for r in conn.execute(
                "SELECT m.*, c.account_id AS owner FROM chat_messages m JOIN conversations c "
                "ON c.id = m.conversation_id WHERE m.legacy_id IS NOT NULL")}
            moved_wrong = [mask(m["chat_id"]) for m in old  # (rows of nobody stay behind: reported above)
                           if m["chat_id"] in accounts and not same_message(m, copies.get(m["id"]))]
            check(not moved_wrong, "every saved message moved into a conversation of its own account, word for word",
                  f"{len(old)} messages" if not moved_wrong else f"wrong for: {sorted(set(moved_wrong))}")
            strays = conn.execute("SELECT COUNT(*) FROM chat_messages m LEFT JOIN conversations c ON "
                                  "c.id = m.conversation_id WHERE c.id IS NULL OR c.account_id != m.account_id"
                                  ).fetchone()[0]
            check(not strays, "every message's conversation belongs to the same account")
            boxes = [r[0] for r in conn.execute("SELECT text_encrypted FROM chat_messages UNION ALL "
                                                "SELECT title_encrypted FROM conversations WHERE title_encrypted != ''")]
            check(all(b.startswith("gAAAAA") for b in boxes), "conversations are stored encrypted, nothing readable",
                  f"{len(boxes)} encrypted texts")
            check(memory.import_legacy() == 0, "moving messages in again changes nothing")

            check(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check")
            check(not conn.execute("PRAGMA foreign_key_check").fetchall(), "foreign keys point to existing rows")

        db.init(work)  # every restart runs init again: it must change nothing
        with open_db(work) as conn:
            check(snapshot(conn) == after, "migrating a second time changes nothing")
            for table in ("conversations", "chat_messages"):
                owned_after[table] = dict(conn.execute(
                    f"SELECT account_id, COUNT(*) FROM {table} GROUP BY account_id").fetchall())
        for account_id, status in sorted(accounts.items()):
            counts = {t: by[account_id] for t, by in owned_after.items() if by.get(account_id)}
            print(f"      account {mask(account_id)} ({'owner' if account_id == owner else status}): "
                  f"{counts or 'no data'}")
    finally:
        for path in leftovers:
            path.unlink(missing_ok=True)
    ok = all(results)
    print("REHEARSAL PASSED: safe to deploy" if ok else "REHEARSAL FAILED: do not deploy")
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        raise SystemExit("Usage: python -m app.db.rehearse /path/to/a/restored/copy.db")
    sys.exit(0 if rehearse(Path(sys.argv[1]), os.environ.get("TELEGRAM_OWNER_ID", "")) else 1)
