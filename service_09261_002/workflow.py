"""版本化业务工作流。

单元（Case）由不同时区教研组补录的事件（Event）组成。进入审核前先校验：
事件非空、字段齐全、材料齐备、按 UTC 时间严格有序。校验不通过的提交原样
退回并逐条列出缺项；审核通过时把事件的原始顺序与内容冻结为当期快照，
此后工作区的补录无法改写快照。
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def parse_ts(raw):
    """解析带时区的时间字符串，失败或为朴素时间时返回 None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _norm_event(raw, seq):
    """把提交体归一成内部事件，时间字段无法解析时记为错误。"""
    raw = raw if isinstance(raw, dict) else {}
    ts = parse_ts(raw.get("timestamp"))
    return {
        "seq": seq,
        "id": raw.get("id"),
        "actor": raw.get("actor"),
        "title": raw.get("title"),
        "timestamp": ts.isoformat() if ts else raw.get("timestamp"),
        "timestamp_utc": ts.astimezone(timezone.utc).isoformat() if ts else None,
        "material": raw.get("material"),
        "_ts": ts,
    }


def validate_events(raw_events):
    """返回 (归一化事件, 缺项说明)；说明为空即校验通过。"""
    reasons = []
    if not raw_events:
        return [], ["events: 单元至少需要一个事件"]

    events = [_norm_event(raw, seq) for seq, raw in enumerate(raw_events)]
    for ev in events:
        where = "events[%d]" % ev["seq"]
        if not ev["id"]:
            reasons.append(where + ".id: 缺少事件标识")
        if not ev["actor"]:
            reasons.append(where + ".actor: 缺少提交教研组/提交人")
        if ev["_ts"] is None:
            reasons.append(where + ".timestamp: 缺少或无法解析的带时区时间")
        if not isinstance(ev["title"], str) or not ev["title"].strip():
            reasons.append(where + ".title: 缺少事件标题")
        mat = ev["material"]
        if not isinstance(mat, dict) or not str(mat.get("name", "")).strip() \
                or not str(mat.get("uri", "")).strip():
            reasons.append(where + ".material: 缺少完整材料（需含 name 与 uri）")

    # 只比较时间本身可解析的事件，顺序错乱与时区无关，一律换算到 UTC 比较。
    prev = None
    for ev in events:
        if ev["_ts"] is None:
            continue
        if prev is not None and ev["_ts"] < prev["_ts"]:
            reasons.append(
                "events[%d] 时间早于 events[%d]（UTC %s < %s），顺序错乱"
                % (ev["seq"], prev["seq"],
                   ev["timestamp_utc"], prev["timestamp_utc"]))
        prev = ev

    for ev in events:
        ev.pop("_ts", None)
    return events, reasons


@dataclass(frozen=True)
class Case:
    id: str
    actor: str
    state: str
    version: int = 1
    events: tuple = ()
    event_keys: tuple = ()
    last_rejection: object = None
    frozen_at: object = None

    def move(self, state, actor):
        allowed = {
            "draft": {"reviewing", "cancelled"},
            "reviewing": {"approved", "rejected"},
            "rejected": {"draft"},
            "approved": {"archived"},
        }
        if state not in allowed.get(self.state, set()):
            raise ValueError("invalid transition")
        return Case(self.id, actor, state, self.version + 1,
                    self.events, self.event_keys,
                    self.last_rejection, self.frozen_at)


class ValidationError(ValueError):
    """提交校验未通过，reasons 逐条列出缺项。"""

    def __init__(self, reasons):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class Workflow:
    def __init__(self, store=None):
        self.rows = {}
        self.keys = {}
        self.store = store

    def create(self, id, actor, key=None):
        if key in self.keys:
            return self.rows[self.keys[key]]
        if id in self.rows:
            raise ValueError("duplicate")
        row = Case(id, actor, "draft")
        self.rows[id] = row
        if key:
            self.keys[key] = id
        return row

    def add_event(self, id, raw, key=None):
        row = self.rows[id]
        # 只有草稿态可补录；审核中或已冻结单元的补录一律拒绝，保证快照不被改写。
        if row.state != "draft":
            raise ValueError("events are locked in state %s" % row.state)
        if key and key in row.event_keys:
            return row
        events, reasons = validate_events(list(row.events) + [raw])
        if reasons:
            raise ValidationError(reasons)
        row = Case(row.id, row.actor, row.state, row.version + 1,
                   tuple(events),
                   tuple(row.event_keys) + ((key,) if key else ()),
                   None, row.frozen_at)
        self.rows[id] = row
        return row

    def submit(self, id, actor):
        """提交审核：完整才进入 reviewing，否则原样退回并说明缺什么。"""
        row = self.rows[id]
        if row.state != "draft":
            raise ValueError("invalid transition")
        _, reasons = validate_events(list(row.events))
        if reasons:
            row = Case(row.id, row.actor, row.state, row.version + 1,
                       row.events, row.event_keys,
                       {"stage": "submit", "reasons": reasons}, row.frozen_at)
            self.rows[id] = row
            raise ValidationError(reasons)
        return self.move(id, "reviewing", actor)

    def reject(self, id, actor, reasons):
        if not isinstance(reasons, list) or not reasons:
            raise ValueError("rejection must list what is missing")
        row = self.move(id, "rejected", actor)
        row = Case(row.id, row.actor, row.state, row.version,
                   row.events, row.event_keys,
                   {"stage": "review", "reasons": reasons}, row.frozen_at)
        self.rows[id] = row
        return row

    def approve(self, id, actor):
        row = self.move(id, "approved", actor)
        frozen_at = datetime.now(timezone.utc).isoformat()
        frozen = {
            "case_id": row.id,
            "version": row.version,
            "frozen_at": frozen_at,
            # 冻结的是审核时刻的原始顺序与内容。
            "events": list(row.events),
        }
        if self.store is not None:
            self.store.save_snapshot(row.id, row.version, frozen_at, frozen)
        row = Case(row.id, row.actor, row.state, row.version,
                   row.events, row.event_keys,
                   row.last_rejection, frozen_at)
        self.rows[id] = row
        return row, frozen

    def move(self, id, state, actor):
        self.rows[id] = self.rows[id].move(state, actor)
        return self.rows[id]

    def snapshot(self):
        return [asdict(self.rows[k]) for k in sorted(self.rows)]

    def frozen_snapshot(self, id):
        return self.store.get_snapshot(id) if self.store is not None else None
