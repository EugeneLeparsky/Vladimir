from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from .common import SetupError


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS responses (
                key TEXT PRIMARY KEY, record TEXT NOT NULL, payload TEXT,
                status TEXT NOT NULL DEFAULT 'pending', sheet_row INTEGER UNIQUE,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def enqueue(self, record):
        previous = self.db.execute("SELECT record FROM responses WHERE key=?", (record["key"],)).fetchone()
        if previous:
            old = json.loads(previous["record"])
            if any(old[name] != record[name] for name in ("vacancy_id", "response_id", "resume_id", "received_at")):
                raise SetupError("ID отклика повторяется с другим резюме/датой. Дедупликация остановлена для этой записи.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO responses(key,record,updated_at) VALUES(?,?,?)",
                            (record["key"], json.dumps(record, ensure_ascii=False), self.now()))

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def items(self, retry=False):
        states = ("error",) if retry else ("pending", "writing")
        return self.db.execute("SELECT * FROM responses WHERE status IN (%s) ORDER BY rowid" %
                               ",".join("?" for _ in states), states).fetchall()

    def reserved_rows(self):
        return {r[0] for r in self.db.execute("SELECT sheet_row FROM responses WHERE sheet_row IS NOT NULL")}

    def reserve(self, key, row, payload):
        # This commit always precedes the remote write. A crash cannot forget its destination.
        with self.db:
            self.db.execute("UPDATE responses SET sheet_row=?,payload=?,status='writing',attempts=attempts+1,error=NULL,updated_at=? WHERE key=?",
                            (row, json.dumps(payload, ensure_ascii=False), self.now(), key))

    def saved(self, key, row):
        with self.db:
            try:
                self.db.execute("UPDATE responses SET sheet_row=?,status='saved',error=NULL,updated_at=? WHERE key=?",
                                (row, self.now(), key))
            except sqlite3.IntegrityError:
                raise SetupError("Одна строка таблицы связана с разными откликами. Нужна проверка state.") from None

    def failed(self, key, message):
        with self.db:
            self.db.execute("UPDATE responses SET status='error',error=?,updated_at=? WHERE key=?", (message, self.now(), key))

    def reconcile(self, index):
        # Sheet keys remain authoritative after an acknowledged or ambiguous write.
        with self.db:
            self.db.execute("UPDATE responses SET sheet_row=NULL WHERE status='saved'")
        for item in self.db.execute("SELECT key,status FROM responses").fetchall():
            if item["key"] in index:
                self.saved(item["key"], index[item["key"]])
            elif item["status"] == "saved":
                raise SetupError("В таблице отсутствует ранее сохранённый ключ отклика. Автоперезапись запрещена.")
