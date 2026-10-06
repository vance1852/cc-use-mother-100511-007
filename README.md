# 安全评估证据谱系服务

本项目提供人工智能治理协作的服务端基础能力，用于登记主体、任务、权限、风险事件和结构化证据，并通过角色权限、请求幂等、SQLite 事务与审计链保持业务状态一致。

在此之上，服务把安全评估证据组织成可追溯的版本链：

```
测试数据集（多版本，supersedes 串联）→ 运行记录（运行参数 + 结果摘要，钉在具体数据集版本）
→ 人工判定（钉在运行记录上）→ 报告结论（引用运行记录与人工判定）
```

## 核心规则

- **版本链可追溯**：数据集每次变更生成新版本并标记被替换的旧版本为 `superseded`；运行记录永远钉在导入时的数据集版本上，旧版本与结论之间的关系不会丢失。
- **撤回 / 过期只影响未定稿结论**：证据（数据集版本、运行记录、人工判定）被撤回（`retracted`）或标记过期（`expired`）时，引用它的未定稿结论会被实时标记为受影响并阻止发布，修订引用后才能定稿。
- **已发布结论保留原始依据**：发布时固化 `basis` 依据快照（证据编号、状态、内容哈希）；此后证据被撤回或过期不会改动已发布结论，而是自动生成一条幂等的影响说明（`impact_statements`）。
- **重复导入幂等**：相同内容的数据集版本、相同业务键且内容一致的运行记录 / 人工判定，重复导入返回既有记录；所有写接口同时支持 `request_id` 请求级幂等。
- **分级字段视图**：不同角色读取同一证据时字段范围不同（见下表）；非管理员只能访问本组织的证据。
- **按结论反查**：`GET /conclusions/{id}/runs` 返回结论直接引用及经人工判定传递引用的全部运行记录，并标注每条记录当前是否影响该结论。

## 角色与字段范围

| 角色 | 可执行动作 | 运行记录字段范围 |
| --- | --- | --- |
| admin | 全部动作；发布结论；撤回 / 过期证据；跨组织访问 | 全部字段 |
| operator | 登记数据集与版本、导入运行记录 | 全部字段（含 `internal_notes`） |
| reviewer | 登记人工判定、起草 / 修订结论 | 不含 `internal_notes` |
| auditor | 只读 | 仅标识、状态、结果摘要与内容哈希（不含 `parameters`、`internal_notes`） |

## 主要接口

写接口（均需 `X-Actor-Id` 头与 `request_id`）：

- `POST /datasets`、`POST /datasets/{id}/versions`
- `POST /runs`、`POST /judgments`
- `POST /conclusions`、`POST /conclusions/{id}/revise`、`POST /conclusions/{id}/publish`
- `POST /datasets/{id}/versions/{v}/status`、`POST /runs/{id}/status`、`POST /judgments/{id}/status`（body 中 `status` 为 `retracted` 或 `expired`）

读接口：

- `GET /datasets/{id}/versions`：数据集版本链
- `GET /runs/{id}`：按角色裁剪的运行记录视图
- `GET /conclusions/{id}`：结论详情（引用现状、受影响证据、发布依据快照、影响说明）
- `GET /conclusions/{id}/runs?affected_only=true`：按结论反查运行记录，可只返回当前影响结论的记录

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.acceptance
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.api --database ai_governance.sqlite3 --host 127.0.0.1 --port 8080
```

服务提供健康检查和带操作者身份的业务请求，重启后 SQLite 中的状态与审计历史继续保留。
