"""SQLite 持久化：单元当前状态、事件流水、冻结快照、幂等键。

events 与 snapshots 均为只增表：冻结内容落库后没有任何更新路径，
之后补录的数据只会追加新行，不会改写历史快照。
"""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS units(
  id TEXT PRIMARY KEY,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  unit_id TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots(
  unit_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL
);
"""


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def save_unit(self, unit):
        self.db.execute(
            "INSERT INTO units(id, body) VALUES(?, ?) "
            "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (unit["id"], _dump(unit)))
        self.db.commit()

    def append_event(self, unit_id, event):
        self.db.execute("INSERT INTO events(unit_id, body) VALUES(?, ?)",
                        (unit_id, _dump(event)))
        self.db.commit()

    def append_snapshot(self, unit_id, version, snapshot):
        self.db.execute(
            "INSERT INTO snapshots(unit_id, version, body) VALUES(?, ?, ?)",
            (unit_id, version, _dump(snapshot)))
        self.db.commit()

    def save_key(self, key, kind, ref):
        self.db.execute("INSERT OR IGNORE INTO keys(key, kind, ref) VALUES(?, ?, ?)",
                        (key, kind, ref))
        self.db.commit()

    def load_all(self):
        units = [json.loads(b) for (b,) in
                 self.db.execute("SELECT body FROM units ORDER BY id")]
        events = [json.loads(b) for (b,) in
                  self.db.execute("SELECT body FROM events ORDER BY rowid")]
        snapshots = [json.loads(b) for (b,) in
                     self.db.execute("SELECT body FROM snapshots ORDER BY unit_id, version")]
        keys = {k: (kind, ref) for k, kind, ref in
                self.db.execute("SELECT key, kind, ref FROM keys")}
        return {"units": units, "events": events, "snapshots": snapshots, "keys": keys}
