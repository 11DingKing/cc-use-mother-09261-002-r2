"""SQLite 快照仓储。

审核通过的单元冻结为独立 JSON 行；之后工作区里的任何补录都只会改动内存中的
当前态，无法回写已冻结的当期快照。
"""
import json
import sqlite3


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS snapshots("
            "case_id TEXT PRIMARY KEY, "
            "version INTEGER NOT NULL, "
            "frozen_at TEXT NOT NULL, "
            "body TEXT NOT NULL)")
        self.db.commit()

    def save_snapshot(self, case_id, version, frozen_at, value):
        # 主键即单元：审核只有一次，重复冻结直接报错而不是覆盖旧快照。
        self.db.execute(
            "INSERT INTO snapshots(case_id, version, frozen_at, body) "
            "VALUES(?,?,?,?)",
            (case_id, version, frozen_at,
             json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def get_snapshot(self, case_id):
        row = self.db.execute(
            "SELECT body FROM snapshots WHERE case_id=?", (case_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None
