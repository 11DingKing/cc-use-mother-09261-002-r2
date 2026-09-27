"""跨学科知识单元编排：事件校验、审核门控与快照冻结。

多个教研组在不同时区补录知识单元事件。规则：
1. 事件入册前校验自身完整性（缺时区、缺材料一律拒收，不留半成品）；
2. 提交审核前校验单元完整性，被拒绝的提交逐项说明缺什么，状态不变；
3. 校验通过才进入审核，并原子冻结当期快照；
4. 快照只增不改，之后补录的事件只进入下一版快照，不改写已冻结内容。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone

UNIT_TRANSITIONS = {
    "collecting": {"reviewing"},
    "reviewing": {"approved", "rejected"},
    "rejected": {"collecting"},
    "approved": {"archived"},
    "archived": set(),
}

# 进入审核前单元必须具备的基本信息：(字段名, 中文名)
REQUIRED_UNIT_FIELDS = (("title", "单元标题"), ("lead", "负责人"))


class Conflict(Exception):
    """当前状态不允许该操作。"""


class SubmissionRejected(Exception):
    """提交审核未通过校验；issues 逐项说明缺什么。"""

    def __init__(self, issues):
        super().__init__("提交被拒绝：%d 项缺失" % len(issues))
        self.issues = issues


class InvalidEvent(Exception):
    """事件自身不完整（如缺时区、缺材料），拒绝入册。"""

    def __init__(self, issues):
        super().__init__("事件不完整：%d 项问题" % len(issues))
        self.issues = issues


def _now():
    return datetime.now(timezone.utc).isoformat()


def _issue(code, message, **extra):
    return {"code": code, "message": message, **extra}


def parse_occurred_at(raw):
    """把带时区偏移的 ISO 8601 时间归一化为 UTC。

    返回 (utc_iso, None)；无法解析或缺时区时返回 (None, issue)。
    不同时区报来的时间只有归一到同一基准，排序才可信。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, _issue("missing_occurred_at", "缺少发生时间 occurred_at", field="occurred_at")
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1] in "Zz" else text)
    except ValueError:
        return None, _issue("invalid_occurred_at",
                            "occurred_at 不是合法的 ISO 8601 时间: %r" % raw,
                            field="occurred_at")
    if dt.tzinfo is None:
        return None, _issue("missing_timezone",
                            "occurred_at 缺少时区偏移，无法跨时区排序: %r" % raw,
                            field="occurred_at")
    return dt.astimezone(timezone.utc).isoformat(), None


def validate_event_shape(raw):
    """入册前校验事件本身是否完整可信，返回 (清洗后的字段, issues)。"""
    if not isinstance(raw, dict):
        return None, [_issue("invalid_event", "事件必须是对象")]
    issues = []

    def _text(name, label):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            issues.append(_issue("missing_" + name, "缺少%s" % label, field=name))
            return None
        return value.strip()

    event_id = _text("id", "事件 id")
    group = _text("group", "教研组 group")
    kind = _text("kind", "事件类型 kind")
    occurred_at_utc, problem = parse_occurred_at(raw.get("occurred_at"))
    if problem:
        issues.append(problem)
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not any(
            v is not None and v != "" and v != [] and v != {} for v in payload.values()):
        issues.append(_issue("missing_material", "缺少材料内容 payload", field="payload"))
    if issues:
        return None, issues
    return {
        "id": event_id, "group": group, "kind": kind,
        "occurred_at": raw["occurred_at"].strip(),
        "occurred_at_utc": occurred_at_utc,
        "payload": copy.deepcopy(payload),
    }, []


@dataclass(frozen=True)
class Event:
    id: str
    unit_id: str
    group: str               # 补录的教研组
    kind: str                # 材料类型，如 objective / assessment
    occurred_at: str         # 教研组提交的原始字符串（含时区）
    occurred_at_utc: str     # 归一化后的 UTC，用于排序
    payload: dict            # 材料内容
    seq: int                 # 服务端到达顺序，同一时刻并列时按它定序
    recorded_at: str

    def view(self, order=None):
        data = {
            "id": self.id, "unit_id": self.unit_id, "group": self.group,
            "kind": self.kind, "occurred_at": self.occurred_at,
            "occurred_at_utc": self.occurred_at_utc,
            "payload": copy.deepcopy(self.payload),
            "seq": self.seq, "recorded_at": self.recorded_at,
        }
        if order is not None:
            data["order"] = order
        return data


@dataclass(frozen=True)
class Unit:
    id: str
    title: str | None
    lead: str | None
    required_kinds: tuple    # 审核前必须齐套的材料类型
    state: str = "collecting"
    version: int = 1
    reviews: tuple = ()

    def view(self):
        return {
            "id": self.id, "title": self.title, "lead": self.lead,
            "required_kinds": list(self.required_kinds),
            "state": self.state, "version": self.version,
            "reviews": [dict(r) for r in self.reviews],
        }


class Workflow:
    """知识单元编排工作流；store 为 None 时纯内存运行。"""

    def __init__(self, store=None):
        self.store = store
        self.units = {}       # unit_id -> Unit
        self.events = {}      # unit_id -> [Event]，按到达顺序
        self.event_ids = {}   # event_id -> Event
        self.snapshots = {}   # unit_id -> [冻结快照]，只增不改
        self.keys = {}        # 幂等键 -> ("unit"|"event", 对象id)
        self._seq = 0

    # ---- 单元 ----

    def create_unit(self, id, actor, title=None, lead=None, required_kinds=(), key=None):
        if key is not None and key in self.keys:
            kind, ref = self.keys[key]
            if kind != "unit":
                raise ValueError("幂等键被其他类型占用: %s" % key)
            return self.units[ref]
        if id in self.units:
            raise ValueError("单元已存在: %s" % id)
        unit = Unit(id=id, title=title or None, lead=lead or None,
                    required_kinds=tuple(required_kinds or ()))
        self.units[id] = unit
        self.events[id] = []
        self.snapshots[id] = []
        if key:
            self._remember_key(key, "unit", id)
        self._save_unit(unit)
        return unit

    def update_unit(self, unit_id, actor, title=None, lead=None, required_kinds=None):
        unit = self._unit(unit_id)
        self._ensure_state(unit, "collecting", "只有在收集阶段才能修改单元信息")
        unit = replace(
            unit,
            title=unit.title if title is None else (title or None),
            lead=unit.lead if lead is None else (lead or None),
            required_kinds=unit.required_kinds if required_kinds is None else tuple(required_kinds),
            version=unit.version + 1,
        )
        self.units[unit_id] = unit
        self._save_unit(unit)
        return unit

    # ---- 事件 ----

    def append_event(self, unit_id, raw, key=None):
        unit = self._unit(unit_id)
        if key is not None and key in self.keys:
            kind, ref = self.keys[key]
            if kind != "event":
                raise ValueError("幂等键被其他类型占用: %s" % key)
            return self.event_ids[ref]
        self._ensure_state(unit, "collecting", "单元正在审核或已结案，补录请等下一期")
        cleaned, issues = validate_event_shape(raw)
        if issues:
            raise InvalidEvent(issues)
        if cleaned["id"] in self.event_ids:
            raise ValueError("事件已存在: %s" % cleaned["id"])
        self._seq += 1
        event = Event(unit_id=unit_id, seq=self._seq, recorded_at=_now(), **cleaned)
        self.events[unit_id].append(event)
        self.event_ids[event.id] = event
        if key:
            self._remember_key(key, "event", event.id)
        if self.store:
            self.store.append_event(unit_id, event.view())
        return event

    def canonical_order(self, unit_id):
        """可信顺序：先按归一化 UTC 时刻，并列再按服务端到达顺序。"""
        return sorted(self.events[unit_id],
                      key=lambda e: (datetime.fromisoformat(e.occurred_at_utc), e.seq))

    # ---- 校验与冻结 ----

    def validate(self, unit_id):
        """完整性校验，返回 issues 列表（空表示可提交）。"""
        unit = self._unit(unit_id)
        issues = []
        for name, label in REQUIRED_UNIT_FIELDS:
            if not getattr(unit, name):
                issues.append(_issue("missing_" + name, "缺少%s" % label, field=name))
        events = self.events[unit_id]
        if not events:
            issues.append(_issue("no_events", "单元没有任何事件，无法审核"))
        covered = {e.kind for e in events}
        for kind in unit.required_kinds:
            if kind not in covered:
                issues.append(_issue("missing_required_kind",
                                     "缺少必需材料类型: %s" % kind, kind=kind))
        return issues

    def submit(self, unit_id, actor, period=None):
        """校验通过才进入审核并冻结当期快照；失败则什么都不改变。"""
        unit = self._unit(unit_id)
        self._ensure_state(unit, "collecting", "只有收集中的单元才能提交审核")
        issues = self.validate(unit_id)
        if issues:
            raise SubmissionRejected(issues)
        snapshot = self._freeze(unit, period)
        self.snapshots[unit_id].append(snapshot)
        if self.store:
            self.store.append_snapshot(unit_id, snapshot["version"], snapshot)
        unit = replace(unit, state="reviewing", version=unit.version + 1)
        self.units[unit_id] = unit
        self._save_unit(unit)
        return copy.deepcopy(snapshot)

    def _freeze(self, unit, period):
        ordered = self.canonical_order(unit.id)
        now = _now()
        return {
            "unit_id": unit.id,
            "version": len(self.snapshots[unit.id]) + 1,
            "period": period,
            "frozen_at": now,
            "unit": {"id": unit.id, "title": unit.title, "lead": unit.lead,
                     "required_kinds": list(unit.required_kinds)},
            "order": [e.id for e in ordered],
            "events": [e.view(order=i) for i, e in enumerate(ordered)],
            "validation": {"result": "passed", "checked_at": now,
                           "rules": ["event_shape", "required_unit_fields",
                                     "required_kinds"]},
        }

    def get_snapshot(self, unit_id, version=None):
        """读取冻结快照（深拷贝），version 缺省为最新一版。"""
        self._unit(unit_id)
        snaps = self.snapshots[unit_id]
        if not snaps:
            raise KeyError("单元 %s 尚未冻结快照" % unit_id)
        if version is None:
            snap = snaps[-1]
        else:
            snap = next((s for s in snaps if s["version"] == version), None)
            if snap is None:
                raise KeyError("单元 %s 没有第 %s 版快照" % (unit_id, version))
        return copy.deepcopy(snap)

    # ---- 审核 ----

    def review(self, unit_id, actor, decision, reason=None):
        unit = self._unit(unit_id)
        if decision not in ("approved", "rejected"):
            raise ValueError("decision 必须是 approved 或 rejected")
        self._ensure_state(unit, "reviewing", "只有审核中的单元才能给出结论")
        if decision == "rejected" and not (reason and reason.strip()):
            raise ValueError("驳回必须说明原因 reason")
        record = {"decision": decision, "actor": actor,
                  "reason": reason, "at": _now()}
        unit = replace(unit, state=decision, version=unit.version + 1,
                       reviews=unit.reviews + (record,))
        self.units[unit_id] = unit
        self._save_unit(unit)
        return unit

    def reopen(self, unit_id, actor):
        unit = self._unit(unit_id)
        self._ensure_state(unit, "rejected", "只有被驳回的单元才能重新补录")
        unit = replace(unit, state="collecting", version=unit.version + 1)
        self.units[unit_id] = unit
        self._save_unit(unit)
        return unit

    def archive(self, unit_id, actor):
        unit = self._unit(unit_id)
        self._ensure_state(unit, "approved", "只有已通过的单元才能归档")
        unit = replace(unit, state="archived", version=unit.version + 1)
        self.units[unit_id] = unit
        self._save_unit(unit)
        return unit

    # ---- 查询 ----

    def list_units(self):
        return [self.units[k].view() for k in sorted(self.units)]

    def unit_detail(self, unit_id):
        unit = self._unit(unit_id)
        ordered = self.canonical_order(unit_id)
        return {
            "unit": unit.view(),
            "events": [e.view(order=i) for i, e in enumerate(ordered)],
            "snapshot_versions": [s["version"] for s in self.snapshots[unit_id]],
            "pending_issues": self.validate(unit_id) if unit.state == "collecting" else [],
        }

    # ---- 持久化 ----

    @classmethod
    def load(cls, store):
        flow = cls(store)
        data = store.load_all()
        for u in data["units"]:
            unit = Unit(id=u["id"], title=u["title"], lead=u["lead"],
                        required_kinds=tuple(u["required_kinds"]), state=u["state"],
                        version=u["version"], reviews=tuple(u.get("reviews", ())))
            flow.units[unit.id] = unit
            flow.events[unit.id] = []
            flow.snapshots[unit.id] = []
        for e in data["events"]:
            event = Event(id=e["id"], unit_id=e["unit_id"], group=e["group"],
                          kind=e["kind"], occurred_at=e["occurred_at"],
                          occurred_at_utc=e["occurred_at_utc"], payload=e["payload"],
                          seq=e["seq"], recorded_at=e["recorded_at"])
            flow.events[event.unit_id].append(event)
            flow.event_ids[event.id] = event
            flow._seq = max(flow._seq, event.seq)
        for s in data["snapshots"]:
            flow.snapshots[s["unit_id"]].append(s)
        flow.keys = dict(data["keys"])
        return flow

    def _unit(self, unit_id):
        try:
            return self.units[unit_id]
        except KeyError:
            raise KeyError("单元不存在: %s" % unit_id)

    @staticmethod
    def _ensure_state(unit, expected, hint):
        if unit.state != expected:
            raise Conflict("单元 %s 当前为 %s：%s" % (unit.id, unit.state, hint))

    def _remember_key(self, key, kind, ref):
        self.keys[key] = (kind, ref)
        if self.store:
            self.store.save_key(key, kind, ref)

    def _save_unit(self, unit):
        if self.store:
            self.store.save_unit(unit.view())
