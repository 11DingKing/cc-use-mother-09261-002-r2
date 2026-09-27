import unittest

from service_09261_002.api import dispatch
from service_09261_002.store import SQLiteStore
from service_09261_002.workflow import (
    Conflict, InvalidEvent, SubmissionRejected, Workflow,
)


def make_flow():
    flow = Workflow()
    flow.create_unit("u1", "creator", title="细胞与能量", lead="王老师",
                     required_kinds=["objective", "assessment"])
    return flow


def add_event(flow, unit="u1", **over):
    raw = {"id": "e1", "group": "生物组", "kind": "objective",
           "occurred_at": "2026-09-20T09:00:00+08:00",
           "payload": {"text": "教学目标"}}
    raw.update(over)
    return flow.append_event(unit, raw)


class TestOrdering(unittest.TestCase):
    def test_cross_timezone_ordering_and_tie_break(self):
        flow = make_flow()
        # 到达顺序与真实发生顺序相反：后补录的反而更早发生
        add_event(flow, id="e-late-arrival", kind="assessment",
                  occurred_at="2026-09-19T21:30:00-04:00", payload={"text": "量规"})  # 01:30Z
        add_event(flow, id="e-early",
                  occurred_at="2026-09-20T09:00:00+08:00")  # 01:00Z
        # 同一时刻、不同时区报来：并列时按服务端到达顺序定序
        add_event(flow, id="e-tie-bj",
                  occurred_at="2026-09-20T09:00:00+08:00")  # 01:00Z, seq 3
        add_event(flow, id="e-tie-utc",
                  occurred_at="2026-09-20T01:00:00Z")       # 01:00Z, seq 4
        order = [e.id for e in flow.canonical_order("u1")]
        self.assertEqual(order, ["e-early", "e-tie-bj", "e-tie-utc", "e-late-arrival"])


class TestEventIntake(unittest.TestCase):
    def test_naive_timestamp_rejected(self):
        flow = make_flow()
        with self.assertRaises(InvalidEvent) as ctx:
            add_event(flow, occurred_at="2026-09-20 09:00:00")
        self.assertIn("missing_timezone", [i["code"] for i in ctx.exception.issues])
        self.assertEqual(flow.events["u1"], [])  # 拒收，不留半成品

    def test_empty_payload_rejected(self):
        flow = make_flow()
        with self.assertRaises(InvalidEvent) as ctx:
            add_event(flow, payload={})
        self.assertIn("missing_material", [i["code"] for i in ctx.exception.issues])
        self.assertEqual(flow.events["u1"], [])


class TestSubmitGate(unittest.TestCase):
    def test_incomplete_submission_reports_gaps(self):
        flow = Workflow()
        flow.create_unit("u2", "creator", required_kinds=["assessment"])
        with self.assertRaises(SubmissionRejected) as ctx:
            flow.submit("u2", "负责人")
        codes = [i["code"] for i in ctx.exception.issues]
        self.assertEqual(codes, ["missing_title", "missing_lead",
                                 "no_events", "missing_required_kind"])
        # 被拒绝后不留似是而非的半成品：状态不变、无快照
        self.assertEqual(flow.units["u2"].state, "collecting")
        self.assertEqual(flow.snapshots["u2"], [])

    def test_missing_required_kind_named(self):
        flow = make_flow()
        add_event(flow, id="e1")  # 只有 objective，缺 assessment
        with self.assertRaises(SubmissionRejected) as ctx:
            flow.submit("u1", "负责人")
        kinds = [i.get("kind") for i in ctx.exception.issues
                 if i["code"] == "missing_required_kind"]
        self.assertEqual(kinds, ["assessment"])


class TestFreezeAndReview(unittest.TestCase):
    def test_freeze_review_reopen_resubmit(self):
        flow = make_flow()
        add_event(flow, id="e1")
        add_event(flow, id="e2", kind="assessment",
                  occurred_at="2026-09-21T09:00:00+08:00", payload={"text": "量规"})
        snap1 = flow.submit("u1", "负责人", period="2026-秋")
        self.assertEqual(flow.units["u1"].state, "reviewing")
        self.assertEqual((snap1["version"], snap1["period"]), (1, "2026-秋"))
        self.assertEqual(snap1["order"], ["e1", "e2"])
        # 审核期间禁止补录，评审看到的永远是冻结内容
        with self.assertRaises(Conflict):
            add_event(flow, id="e3")
        # 驳回必须说明原因
        with self.assertRaises(ValueError):
            flow.review("u1", "审核员", "rejected")
        unit = flow.review("u1", "审核员", "rejected", reason="缺少实验安全说明")
        self.assertEqual(unit.state, "rejected")
        flow.reopen("u1", "负责人")
        # 驳回后补录新事件，再次提交生成 v2；v1 不被改写
        add_event(flow, id="e3", kind="assessment",
                  occurred_at="2026-09-22T09:00:00+08:00", payload={"text": "安全说明"})
        snap2 = flow.submit("u1", "负责人", period="2026-秋")
        self.assertEqual(snap2["version"], 2)
        self.assertEqual(snap2["order"], ["e1", "e2", "e3"])
        again1 = flow.get_snapshot("u1", version=1)
        self.assertEqual(again1["order"], ["e1", "e2"])
        self.assertEqual(again1, snap1)

    def test_snapshot_is_immutable_for_callers(self):
        flow = make_flow()
        add_event(flow, id="e1")
        add_event(flow, id="e2", kind="assessment", payload={"text": "量规"})
        snap = flow.submit("u1", "负责人")
        snap["events"][0]["payload"]["text"] = "篡改"
        snap["order"].reverse()
        fresh = flow.get_snapshot("u1")
        self.assertEqual(fresh["events"][0]["payload"]["text"], "教学目标")
        self.assertEqual(fresh["order"], ["e1", "e2"])


class TestIdempotency(unittest.TestCase):
    def test_create_and_append_are_idempotent(self):
        flow = Workflow()
        u1 = flow.create_unit("u1", "a", title="t", lead="l", key="K1")
        u2 = flow.create_unit("u1", "a", title="t", lead="l", key="K1")  # 重试
        self.assertIs(u1, u2)
        with self.assertRaises(ValueError):
            flow.create_unit("u1", "a")  # 无幂等键的重复 id
        raw = {"id": "e1", "group": "g", "kind": "k",
               "occurred_at": "2026-09-20T09:00:00+08:00", "payload": {"text": "x"}}
        e1 = flow.append_event("u1", raw, key="K2")
        e2 = flow.append_event("u1", raw, key="K2")
        self.assertIs(e1, e2)
        self.assertEqual(len(flow.events["u1"]), 1)


class TestApi(unittest.TestCase):
    def test_end_to_end(self):
        flow = Workflow()
        status, _ = dispatch(flow, "POST", "/units",
                             {"id": "u1", "actor": "a", "title": "细胞与能量",
                              "lead": "王老师",
                              "required_kinds": ["objective", "assessment"]})
        self.assertEqual(status, 201)
        # 缺时区的事件直接 400，说明问题
        status, resp = dispatch(flow, "POST", "/units/u1/events",
                                {"id": "e0", "group": "g", "kind": "objective",
                                 "occurred_at": "2026-09-20 09:00",
                                 "payload": {"text": "x"}})
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"], "invalid_event")
        self.assertIn("missing_timezone", [i["code"] for i in resp["issues"]])
        # 材料不齐时提交被 422 拒绝并说明缺什么
        dispatch(flow, "POST", "/units/u1/events",
                 {"id": "e1", "group": "生物组", "kind": "objective",
                  "occurred_at": "2026-09-20T09:00:00+08:00",
                  "payload": {"text": "目标"}})
        status, resp = dispatch(flow, "POST", "/units/u1/submit", {"actor": "负责人"})
        self.assertEqual(status, 422)
        self.assertEqual(resp["error"], "incomplete")
        self.assertIn("missing_required_kind", [i["code"] for i in resp["issues"]])
        # 补齐材料后提交成功并冻结；顺序按 UTC 归一化而非到达顺序
        status, _ = dispatch(flow, "POST", "/units/u1/events",
                             {"id": "e2", "group": "化学组", "kind": "assessment",
                              "occurred_at": "2026-09-19T21:30:00-04:00",
                              "payload": {"text": "量规"}})
        self.assertEqual(status, 201)
        status, resp = dispatch(flow, "POST", "/units/u1/submit",
                                {"actor": "负责人", "period": "2026-秋"})
        self.assertEqual(status, 201)
        self.assertEqual(resp["snapshot"]["order"], ["e1", "e2"])  # e1=01:00Z < e2=01:30Z
        # 审核中补录 → 409
        status, _ = dispatch(flow, "POST", "/units/u1/events",
                             {"id": "e3", "group": "g", "kind": "objective",
                              "occurred_at": "2026-09-22T09:00:00+08:00",
                              "payload": {"text": "x"}})
        self.assertEqual(status, 409)
        # 审核通过后，接口读到的仍是冻结时的原始顺序与内容
        status, _ = dispatch(flow, "POST", "/units/u1/review",
                             {"actor": "审核员", "decision": "approved"})
        self.assertEqual(status, 200)
        status, snap = dispatch(flow, "GET", "/units/u1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snap["events"][0]["occurred_at"], "2026-09-20T09:00:00+08:00")
        self.assertEqual(snap["events"][0]["occurred_at_utc"], "2026-09-20T01:00:00+00:00")
        status, snap_v1 = dispatch(flow, "GET", "/units/u1/snapshots/1")
        self.assertEqual((status, snap_v1), (200, snap))
        status, _ = dispatch(flow, "GET", "/units/nope")
        self.assertEqual(status, 404)
        status, listing = dispatch(flow, "GET", "/units")
        self.assertEqual([u["id"] for u in listing["units"]], ["u1"])


class TestStore(unittest.TestCase):
    def test_roundtrip_preserves_frozen_snapshot(self):
        store = SQLiteStore(":memory:")
        flow = Workflow(store)
        flow.create_unit("u1", "a", title="t", lead="l",
                         required_kinds=["objective"], key="K1")
        flow.append_event("u1", {"id": "e1", "group": "g", "kind": "objective",
                                 "occurred_at": "2026-09-20T09:00:00+08:00",
                                 "payload": {"text": "目标"}}, key="K2")
        snap = flow.submit("u1", "a")
        restored = Workflow.load(store)
        self.assertEqual(restored.get_snapshot("u1"), snap)
        self.assertEqual(restored.units["u1"].state, "reviewing")
        # 幂等键随库恢复，重试不会产生重复数据
        again = restored.create_unit("u1", "a", key="K1")
        self.assertEqual(again.id, "u1")
        # 恢复后继续走驳回-补录流程，事件序号不与历史冲突
        restored.review("u1", "r", "rejected", reason="缺材料")
        restored.reopen("u1", "a")
        ev = restored.append_event("u1", {"id": "e2", "group": "g", "kind": "objective",
                                          "occurred_at": "2026-09-21T09:00:00+08:00",
                                          "payload": {"text": "补充"}})
        self.assertEqual(ev.seq, 2)
        self.assertEqual(restored.get_snapshot("u1", version=1)["order"], ["e1"])


if __name__ == "__main__":
    unittest.main()
