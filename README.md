# 跨学科知识单元编排

纯 Python 服务端基础项目，提供版本化状态、幂等命令、SQLite 持久化和 JSON API 边界。

## 业务规则

多个教研组在不同时区补录知识单元事件，审核前必须先过校验门：

1. **事件入册即校验**：缺时区偏移、缺材料内容的事件直接拒收（400），不留半成品。
2. **提交审核先校验完整性**：单元标题、负责人、必需材料类型齐全且至少有一个事件，
   否则 422 拒绝并逐项说明缺什么，单元状态不变、不产生快照。
3. **校验通过才冻结**：进入审核的同时原子冻结当期快照（版本递增）。
4. **快照只增不改**：之后补录的事件只进入下一版快照，
   接口读到的永远是冻结时的原始顺序和内容，不被后来补录的数据改写。

事件排序规则：先按归一化后的 UTC 时刻，同一时刻并列再按服务端到达顺序，
从根本上避免跨时区报时造成的顺序错乱。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/units` | 创建单元（title/lead/required_kinds，支持 idempotency_key） |
| GET | `/units` | 单元列表 |
| GET | `/units/{id}` | 单元详情：当前可信顺序的事件、快照版本、待补缺口 |
| PATCH | `/units/{id}` | 收集阶段补充单元信息 |
| POST | `/units/{id}/events` | 补录事件（group/kind/occurred_at 带时区/payload） |
| POST | `/units/{id}/submit` | 校验并提交审核，成功即冻结快照（可带 period） |
| POST | `/units/{id}/review` | 审核结论（approved/rejected，驳回必须给 reason） |
| POST | `/units/{id}/reopen` | 驳回后重新进入收集 |
| POST | `/units/{id}/archive` | 通过后归档 |
| GET | `/units/{id}/snapshot` | 最新冻结快照（原始顺序 + 冻结内容） |
| GET | `/units/{id}/snapshots/{version}` | 指定版本的冻结快照 |

状态机：`collecting → reviewing → approved → archived`，
`reviewing → rejected → collecting`（可补录后再次提交，生成新版快照）。

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_002 tests
