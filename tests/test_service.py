import unittest

from service_09261_002.api import dispatch
from service_09261_002.store import SQLiteStore
from service_09261_002.workflow import Workflow, ValidationError


def ev(eid, actor, title, ts, name, uri):
    return {"id": eid, "actor": actor, "title": title, "timestamp": ts,
            "material": {"name": name, "uri": uri}}


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.flow = Workflow(self.store)
        self.flow.create("c1", "a", "k")

    def test_create_idempotent(self):
        self.assertEqual(self.flow.create("c1", "a", "k").state, "draft")
        self.assertEqual(self.flow.create("c1", "a", "k").version, 1)

    def test_cross_timezone_order_validated(self):
        # 东京 10:00(+09:00)=01:00Z；柏林 02:30(+02:00)=00:30Z，后到的更早 → 顺序错乱
        with self.assertRaises(ValidationError) as cm:
            self.flow.add_event("c1", ev(
                "e1", "tokyo", "开题", "2026-09-01T10:00:00+09:00", "大纲", "u1"))
            self.flow.add_event("c1", ev(
                "e2", "berlin", "补充", "2026-09-01T02:30:00+02:00", "讲义", "u2"))
        self.assertTrue(any("顺序错乱" in r for r in cm.exception.reasons))

    def test_naive_timestamp_and_missing_material(self):
        with self.assertRaises(ValidationError) as cm:
            self.flow.add_event("c1", {
                "id": "e1", "actor": "a", "title": "t",
                "timestamp": "2026-09-01 10:00:00",
                "material": {"name": "只有名字"}})
        reasons = cm.exception.reasons
        self.assertTrue(any("timestamp" in r for r in reasons))
        self.assertTrue(any("material" in r for r in reasons))
        # 半成品不得留在单元里
        self.assertEqual(self.flow.rows["c1"].events, ())

    def test_submit_incomplete_is_rejected_with_reasons(self):
        status, body = dispatch(self.flow, "POST", "/cases/c1/submit",
                                {"actor": "lead"})
        self.assertEqual(status, 422)
        self.assertTrue(body["reasons"])
        self.assertEqual(self.flow.rows["c1"].state, "draft")

    def test_full_review_cycle_freezes_snapshot(self):
        # 不同时区按 UTC 严格有序补录
        self.flow.add_event("c1", ev(
            "e1", "berlin", "开题", "2026-09-01T02:30:00+02:00", "讲义", "u1"))
        self.flow.add_event("c1", ev(
            "e2", "tokyo", "补充", "2026-09-01T10:00:00+09:00", "实验单", "u2"))

        # 审核驳回：必须说明缺什么
        self.flow.submit("c1", "lead")
        row = self.flow.reject("c1", "reviewer", ["events[1].material.uri: 链接失效"])
        self.assertEqual(row.state, "rejected")
        self.assertIn("链接失效", row.last_rejection["reasons"][0])

        # 打回草稿、补齐、重新送审通过
        self.flow.move("c1", "draft", "lead")
        self.assertEqual(dispatch(self.flow, "POST", "/cases/c1/submit",
                                  {"actor": "lead"})[0], 200)
        status, body = dispatch(self.flow, "POST", "/cases/c1/approve",
                                {"actor": "reviewer"})
        self.assertEqual(status, 200)
        frozen = body["frozen"]
        self.assertEqual([e["id"] for e in frozen["events"]], ["e1", "e2"])
        self.assertEqual(frozen["events"][0]["timestamp_utc"],
                         "2026-09-01T00:30:00+00:00")

        # 冻结后补录一律加锁
        status, body = dispatch(self.flow, "POST", "/cases/c1/events",
                                {"event": ev("e3", "x", "晚到的补录",
                                             "2026-10-01T00:00:00Z", "m", "u3")})
        self.assertEqual(status, 409)

        # 接口查到的仍是审核时的原始顺序和内容
        status, got = dispatch(self.flow, "GET", "/cases/c1/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(got, frozen)
        self.assertEqual([e["id"] for e in got["events"]], ["e1", "e2"])

    def test_snapshot_survives_new_workspace(self):
        self.flow.add_event("c1", ev(
            "e1", "a", "t", "2026-09-01T00:00:00Z", "m", "u"))
        self.flow.submit("c1", "lead")
        _, frozen = self.flow.approve("c1", "reviewer")

        # 即使后来另起工作区补录，仓储里的当期快照不变
        fresh = Workflow(self.store)
        self.assertEqual(fresh.frozen_snapshot("c1"), frozen)

    def test_event_idempotency_key(self):
        payload = {"event": ev("e1", "a", "t", "2026-09-01T00:00:00Z", "m", "u"),
                   "idempotency_key": "ev-k"}
        self.assertEqual(dispatch(self.flow, "POST", "/cases/c1/events", payload)[0], 200)
        # 重复键不产生重复事件
        self.assertEqual(dispatch(self.flow, "POST", "/cases/c1/events", payload)[0], 200)
        self.assertEqual(len(self.flow.rows["c1"].events), 1)


if __name__ == "__main__":
    unittest.main()
