"""JSON API 适配器。

路由：
  POST /cases                      创建单元（支持 idempotency_key）
  POST /cases/<id>/events          补录一个事件（支持 idempotency_key）
  POST /cases/<id>/submit          提交审核，信息不全返回 422 及缺项
  POST /cases/<id>/reject          审核驳回，body.reasons 必须逐条列出
  POST /cases/<id>/approve         审核通过并冻结当期快照
  GET  /cases                      当前工作区状态
  GET  /cases/<id>                 单元当前态（含最近一次缺项说明）
  GET  /cases/<id>/snapshot        审核时冻结的原始顺序与内容
"""
from dataclasses import asdict

from .workflow import ValidationError


def dispatch(flow, method, path, body=None):
    body = body or {}
    parts = [p for p in path.split("/") if p]

    if method == "POST" and path == "/cases":
        try:
            row = flow.create(body["id"], body["actor"],
                              body.get("idempotency_key"))
        except KeyError:
            return 400, {"error": "missing_field", "field": "id|actor"}
        except ValueError:
            return 409, {"error": "duplicate"}
        return 201, asdict(row)

    if len(parts) == 3 and parts[0] == "cases" and method == "POST":
        cid, action = parts[1], parts[2]
        if cid not in getattr(flow, "rows", {}):
            return 404, {"error": "not_found"}
        try:
            if action == "events":
                row = flow.add_event(
                    cid, body.get("event", body),
                    body.get("idempotency_key"))
                return 200, asdict(row)
            if action == "submit":
                row = flow.submit(cid, body["actor"])
                return 200, asdict(row)
            if action == "reject":
                row = flow.reject(cid, body["actor"], body.get("reasons"))
                return 200, asdict(row)
            if action == "approve":
                row, frozen = flow.approve(cid, body["actor"])
                return 200, {"case": asdict(row), "frozen": frozen}
        except KeyError:
            return 400, {"error": "missing_field", "field": "actor"}
        except ValidationError as exc:
            # 半成品不得进入审核：逐条说明缺什么，状态保持原样。
            return 422, {"error": "validation_failed", "reasons": exc.reasons}
        except ValueError as exc:
            return 409, {"error": "conflict", "message": str(exc)}
        return 404, {"error": "not_found"}

    if method == "GET" and path == "/cases":
        return 200, flow.snapshot()

    if len(parts) == 2 and parts[0] == "cases" and method == "GET":
        row = flow.rows.get(parts[1])
        if row is None:
            return 404, {"error": "not_found"}
        return 200, asdict(row)

    if (len(parts) == 3 and parts[0] == "cases"
            and parts[2] == "snapshot" and method == "GET"):
        frozen = flow.frozen_snapshot(parts[1])
        if frozen is None:
            return 404, {"error": "snapshot_not_found"}
        return 200, frozen

    return 404, {"error": "not_found"}
