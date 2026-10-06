# 安全评估证据谱系服务

本项目提供人工智能治理协作的服务端基础能力，用于登记主体、任务、权限、风险事件和结构化证据，
并在其之上构建**证据谱系（evidence lineage）**：把测试数据集、运行参数、结果摘要、人工判定和
报告结论组织成可追溯的版本链，通过角色权限、请求幂等、SQLite 事务与哈希审计链保持业务状态一致。

## 核心模型与语义

- **证据（Evidence）**：四类 `test_dataset` / `run_parameters` / `result_summary` / `manual_judgment`。
  同一 `evidence_key` 下内容变更形成自增版本链（`supersedes` / `replaced_by`）；旧版本自动置为
  `expired`，也可由审查员 `retracted`（撤回，必须填写原因），或通过 `expires_at` 到期扫描过期。
- **运行（Run）**：一次“数据集 + 参数 (+结果摘要)”的模型运行。已失效的证据不能用于新运行。
- **结论（Conclusion）**：报告结论以一组证据为依据（basis，定格时记录证据摘要快照）。
  生命周期为 `draft → published`，草稿可修订出新版本；依据失效时草稿转为 `invalidated`。
- **定稿保护**：证据撤回/过期只影响**尚未定稿**的结论（草稿失效）；**已发布报告保留原始依据快照**，
  结论状态不变，并生成一条 `published_preserved` 影响说明。
- **影响说明（ImpactStatement）**：每次证据状态变更联动生成三种作用域的说明：
  `draft_invalidated`（草稿失效）、`published_preserved`（发布稿保留并提示复核）、
  `run_affected`（历史运行保留原始输入但被标记受影响）。
- **按结论反查运行**：`GET /conclusions/{id}/affected-runs` 沿结论依据经“运行图”
  （数据集↔参数↔结果）反向扩散，返回该结论涉及的全部运行记录与证据，即使证据已被撤回也不丢失。
- **幂等**：所有写操作携带 `request_id`，重复请求重放同一回执；相同证据内容重复导入（即使
  `request_id` 不同）做业务层自然去重，返回既有版本而不产生新版本。
- **字段级权限**：审查员（reviewer）、操作员（operator）、管理员（admin）、审计员（auditor）
  看到的证据载荷字段范围不同（如 `storage_location`、`endpoint`、`raw_artifact` 等内部字段
  对审查员遮蔽）；结论正文中 `internal_` 前缀字段仅 admin/auditor 可见；auditor 可跨组织只读调阅。

## HTTP 接口

身份通过 `X-Actor-Id` 请求头传递（先经基础接口登记组织、操作者与场所）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/evidence` | 导入/自然去重证据；可带 `supersedes`、`expires_at` |
| POST | `/evidence/{id}/retract` | 撤回证据（需 `reason`） |
| POST | `/evidence/{id}/expire` | 手工标记过期 |
| POST | `/evidence/sweep-expired` | 批量扫描到期证据 |
| GET | `/evidence?site_id=&evidence_key=&evidence_type=&status=` | 列证据（按角色裁剪字段） |
| GET | `/evidence/{id}` | 取单条证据 |
| GET | `/evidence/{id}/lineage` | 版本链、关联运行与结论 |
| POST | `/runs` | 登记运行（`client_run_key` 业务幂等） |
| POST | `/runs/{id}/result` | 为运行补挂结果摘要 |
| GET | `/runs?site_id=` / `/runs/{id}` | 查询运行 |
| POST | `/conclusions` | 创建草稿结论（含 `basis`） |
| POST | `/conclusions/revise` | 依据新版本证据开修订版 |
| POST | `/conclusions/{id}/publish` | 定稿（依据存在失效证据时拒绝） |
| GET | `/conclusions?site_id=` / `/conclusions/{id}` | 查询结论 |
| GET | `/conclusions/{id}/affected-runs` | **按结论反查全部受影响运行** |
| GET | `/impact-statements?conclusion_id=&evidence_id=&run_id=` | 查询影响说明 |

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

验收覆盖：证据重复导入幂等、数据集新版本替换、草稿失效、已发布报告保留原始依据、
三类影响说明、按结论反查运行，以及审查员/管理员的字段裁剪差异。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.api --database ai_governance.sqlite3 --host 127.0.0.1 --port 8080
```

服务提供健康检查和带操作者身份的业务请求，重启后 SQLite 中的状态、版本链与审计历史继续保留。
