# 跨学科知识单元编排

纯 Python 服务端基础项目，提供版本化状态、幂等命令、SQLite 持久化和 JSON API 边界。

## 规则

- 单元（Case）由各时区教研组补录的事件（Event）组成；事件时间必须带时区，
  服务端统一换算 UTC 后校验严格时间顺序，朴素时间戳直接判缺项。
- 事件必须含 `id / actor / title / timestamp`，且材料完整（`material.name`、`material.uri`）。
- 送审时信息不全：HTTP 422，响应体 `reasons` 逐条列出缺什么，不产生半成品事件，状态不变。
- 审核驳回同样必须给出 `reasons`；补齐后可重新送审。
- 审核通过：把审核时刻的事件原始顺序与内容冻结为当期快照存入 SQLite，
  之后工作区补录加锁；`GET /cases/<id>/snapshot` 永远返回冻结内容，不被后补数据改写。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 创建单元（支持 `idempotency_key`） |
| POST | `/cases/<id>/events` | 补录事件（支持 `idempotency_key`，校验失败 422） |
| POST | `/cases/<id>/submit` | 提交审核 |
| POST | `/cases/<id>/reject` | 驳回（body.`reasons` 必填） |
| POST | `/cases/<id>/approve` | 通过并冻结快照 |
| GET | `/cases` | 当前工作区状态 |
| GET | `/cases/<id>` | 单元当前态（含最近缺项说明） |
| GET | `/cases/<id>/snapshot` | 审核时冻结的原始顺序与内容 |

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_002 tests
