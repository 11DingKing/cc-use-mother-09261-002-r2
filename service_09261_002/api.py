"""JSON API 适配器：把 HTTP 动词+路径映射到工作流调用。

状态码约定：201 创建/冻结成功，200 查询或状态推进成功，
400 请求本身不合法（含不完整事件），404 资源不存在，
409 当前状态不允许该操作，422 提交审核未通过校验（issues 说明缺什么）。
"""
from .workflow import Conflict, InvalidEvent, SubmissionRejected


def dispatch(flow, method, path, body=None):
    body = body or {}
    parts = [p for p in path.split("/") if p]
    try:
        return _route(flow, method, parts, body)
    except SubmissionRejected as e:
        return 422, {"error": "incomplete", "issues": e.issues}
    except InvalidEvent as e:
        return 400, {"error": "invalid_event", "issues": e.issues}
    except Conflict as e:
        return 409, {"error": "invalid_state", "message": str(e)}
    except KeyError as e:
        return 404, {"error": "not_found", "message": str(e).strip("'")}
    except ValueError as e:
        return 400, {"error": "bad_request", "message": str(e)}


def _need(body, name):
    value = body.get(name)
    if value is None:
        raise ValueError("缺少必填字段: %s" % name)
    return value


def _route(flow, method, parts, body):
    if parts == ["units"]:
        if method == "POST":
            unit = flow.create_unit(_need(body, "id"), body.get("actor", "anonymous"),
                                    title=body.get("title"), lead=body.get("lead"),
                                    required_kinds=body.get("required_kinds"),
                                    key=body.get("idempotency_key"))
            return 201, unit.view()
        if method == "GET":
            return 200, {"units": flow.list_units()}
    if len(parts) >= 2 and parts[0] == "units":
        unit_id = parts[1]
        if len(parts) == 2:
            if method == "GET":
                return 200, flow.unit_detail(unit_id)
            if method == "PATCH":
                unit = flow.update_unit(unit_id, body.get("actor", "anonymous"),
                                        title=body.get("title"), lead=body.get("lead"),
                                        required_kinds=body.get("required_kinds"))
                return 200, unit.view()
        if len(parts) == 3 and method == "POST":
            if parts[2] == "events":
                event = flow.append_event(unit_id, body, key=body.get("idempotency_key"))
                return 201, event.view()
            if parts[2] == "submit":
                snapshot = flow.submit(unit_id, body.get("actor", "anonymous"),
                                       period=body.get("period"))
                return 201, {"unit": flow.units[unit_id].view(), "snapshot": snapshot}
            if parts[2] == "review":
                unit = flow.review(unit_id, body.get("actor", "anonymous"),
                                   body.get("decision"), body.get("reason"))
                return 200, unit.view()
            if parts[2] == "reopen":
                return 200, flow.reopen(unit_id, body.get("actor", "anonymous")).view()
            if parts[2] == "archive":
                return 200, flow.archive(unit_id, body.get("actor", "anonymous")).view()
        if len(parts) == 3 and parts[2] == "snapshot" and method == "GET":
            return 200, flow.get_snapshot(unit_id)
        if len(parts) == 4 and parts[2] == "snapshots" and method == "GET":
            return 200, flow.get_snapshot(unit_id, version=int(parts[3]))
    return 404, {"error": "not_found"}
