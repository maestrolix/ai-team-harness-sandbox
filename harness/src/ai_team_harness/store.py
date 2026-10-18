"""Постоянная очередь SQLite; соединением владеет поток диспетчера."""

import json
import sqlite3
import time

from .workspaces import resource_keys


class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""CREATE TABLE IF NOT EXISTS jobs(
          key TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          error TEXT, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY,value TEXT,version INTEGER);""")

    def enqueue(self, event):
        self.db.execute(
            """INSERT INTO jobs(key,payload,created) VALUES(?,?,?)
          ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,status='queued',error=NULL,created=excluded.created
          WHERE jobs.status='superseded'""",
            (event["key"], json.dumps(event, ensure_ascii=False), time.time()),
        )

    def claimed_feedback(self, pr, groups):
        current = {f["key"] for group in groups.values() for f in group}
        claimed = set()
        for row in self.db.execute(
            "SELECT key,payload,status FROM jobs WHERE status!='superseded'"
        ).fetchall():
            event = json.loads(row["payload"])
            if event.get("pr") != pr["number"]:
                continue
            keys = {f["key"] for f in event.get("feedback", [])} or {event["key"]}
            # Migrate queued legacy comments into batches; retain completed/blocked
            # versions. Rebuild stale queued batches without losing unchanged items.
            if row["status"] == "queued" and ("feedback" not in event or not keys <= current):
                self.db.execute("UPDATE jobs SET status='superseded' WHERE key=?", (row["key"],))
            else:
                claimed.update(keys)
        return claimed

    def transition(self, key, value):
        old = self.db.execute(
            "SELECT value,version FROM observations WHERE key=?", (key,)
        ).fetchone()
        if old and old["value"] == value:
            return None
        version = old["version"] + 1 if old else 1
        self.db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?)", (key, value, version))
        return version

    def finish(self, key, status, error=None):
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,error=? WHERE key=?", (status, error, key))

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='blocked',error='Runner interrupted; inspect branch and retry explicitly' WHERE status='running'"
            )

    def reserved(self, statuses=("running", "blocked"), *, exclude_key=None) -> set[str]:
        """Даже новый комментарий не обходит незавершённую/аварийную задачу."""
        keys = set()
        for row in self.db.execute("SELECT key,payload,status FROM jobs"):
            if row["status"] in statuses and row["key"] != exclude_key:
                keys.update(resource_keys(json.loads(row["payload"])))
        return keys

    def claim(self) -> dict | None:
        """Захватить ближайшее независимое событие в транзакции диспетчера."""
        with self.db:
            reserved = self.reserved()
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created,key"
            ).fetchall():
                event = json.loads(row["payload"])
                if not resource_keys(event).isdisjoint(reserved):
                    continue
                self.db.execute(
                    "UPDATE jobs SET status='running',error=NULL WHERE key=?", (row["key"],)
                )
                return event
        return None

    def save(self, event: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET payload=? WHERE key=?",
                (json.dumps(event, ensure_ascii=False), event["key"]),
            )

    def retry(self, key: str) -> None:
        row = self.db.execute("SELECT status,payload FROM jobs WHERE key=?", (key,)).fetchone()
        if not row or row["status"] != "blocked":
            raise ValueError("Only a blocked job can be explicitly retried")
        event = json.loads(row["payload"])
        with self.db:
            self.db.execute("UPDATE jobs SET status='queued',error=NULL WHERE key=?", (key,))
            if event["kind"] == "issue":
                self.transition(f"issue:{event['issue']}", "ready")

    def close(self) -> None:
        self.db.close()
