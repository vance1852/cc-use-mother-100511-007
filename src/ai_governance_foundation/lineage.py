"""提供安全评估证据谱系能力：版本链、影响传播与分级字段视图。

证据链结构：测试数据集（多版本）→ 运行记录（运行参数 + 结果摘要，钉在具体数据集版本上）
→ 人工判定（钉在运行记录上）→ 报告结论（引用运行记录与人工判定）。
证据被撤回或过期时，只影响尚未定稿的结论（阻止发布并提示修订）；
已发布的结论保留发布时的原始依据快照，并自动生成影响说明。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService


TERMINAL_STATUSES = frozenset({"retracted", "expired"})
JUDGMENT_VERDICTS = frozenset({"pass", "fail", "inconclusive"})
EVENT_LABELS = {"retracted": "撤回", "expired": "标记过期"}

RUN_FIELDS = (
    "run_id", "organization_id", "external_key", "dataset_id", "dataset_version",
    "dataset_version_status", "parameters", "result_summary", "internal_notes",
    "parameters_hash", "result_hash", "status", "status_reason",
    "created_by", "created_at", "status_changed_at",
)
RUN_ROLE_FIELDS = {
    "admin": frozenset(RUN_FIELDS),
    "operator": frozenset(RUN_FIELDS),
    "reviewer": frozenset(field for field in RUN_FIELDS if field != "internal_notes"),
    "auditor": frozenset({
        "run_id", "dataset_id", "dataset_version", "dataset_version_status",
        "status", "status_reason", "created_at", "result_summary",
        "parameters_hash", "result_hash",
    }),
}

JUDGMENT_FIELDS = (
    "judgment_id", "organization_id", "external_key", "run_id", "verdict",
    "rationale", "status", "status_reason", "created_by", "created_at", "status_changed_at",
)
JUDGMENT_ROLE_FIELDS = {
    "admin": frozenset(JUDGMENT_FIELDS),
    "operator": frozenset(JUDGMENT_FIELDS),
    "reviewer": frozenset(JUDGMENT_FIELDS),
    "auditor": frozenset({"judgment_id", "run_id", "verdict", "status", "created_at"}),
}

DATASET_VERSION_FIELDS = (
    "dataset_id", "version", "content_hash", "metadata", "supersedes_version",
    "status", "status_reason", "created_by", "created_at", "status_changed_at",
)
DATASET_VERSION_ROLE_FIELDS = {
    "admin": frozenset(DATASET_VERSION_FIELDS),
    "operator": frozenset(DATASET_VERSION_FIELDS),
    "reviewer": frozenset(DATASET_VERSION_FIELDS),
    "auditor": frozenset({
        "dataset_id", "version", "content_hash", "supersedes_version", "status", "created_at",
    }),
}


class EvidenceLineageService(DomainService):
    """在基础服务之上组织证据版本链并维护结论影响。"""

    # ---------- 内部工具 ----------

    def _dataset_row(self, connection, dataset_id: str):
        row = connection.execute(
            "SELECT * FROM datasets WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("数据集不存在")
        return row

    def _dataset_version_row(self, connection, dataset_id: str, version: int):
        row = connection.execute(
            "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?",
            (dataset_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError("数据集版本不存在")
        return row

    def _run_row(self, connection, run_id: str):
        row = connection.execute(
            "SELECT * FROM run_records WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("运行记录不存在")
        return row

    def _judgment_row(self, connection, judgment_id: str):
        row = connection.execute(
            "SELECT * FROM judgments WHERE judgment_id=?", (judgment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("人工判定不存在")
        return row

    def _conclusion_row(self, connection, conclusion_id: str):
        row = connection.execute(
            "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("结论不存在")
        return row

    def _check_org(self, actor, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织的证据")

    def _run_view(self, connection, row) -> dict[str, Any]:
        version_row = connection.execute(
            "SELECT status FROM dataset_versions WHERE dataset_id=? AND version=?",
            (row["dataset_id"], row["dataset_version"]),
        ).fetchone()
        return {
            "run_id": row["run_id"],
            "organization_id": row["organization_id"],
            "external_key": row["external_key"],
            "dataset_id": row["dataset_id"],
            "dataset_version": row["dataset_version"],
            "dataset_version_status": version_row["status"] if version_row else "missing",
            "parameters": json.loads(row["parameters_json"]),
            "result_summary": json.loads(row["result_json"]),
            "internal_notes": row["internal_notes"],
            "parameters_hash": row["parameters_hash"],
            "result_hash": row["result_hash"],
            "status": row["status"],
            "status_reason": row["status_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "status_changed_at": row["status_changed_at"],
        }

    def _judgment_view(self, row) -> dict[str, Any]:
        return {
            "judgment_id": row["judgment_id"],
            "organization_id": row["organization_id"],
            "external_key": row["external_key"],
            "run_id": row["run_id"],
            "verdict": row["verdict"],
            "rationale": row["rationale"],
            "status": row["status"],
            "status_reason": row["status_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "status_changed_at": row["status_changed_at"],
        }

    def _dataset_version_view(self, row) -> dict[str, Any]:
        return {
            "dataset_id": row["dataset_id"],
            "version": row["version"],
            "content_hash": row["content_hash"],
            "metadata": json.loads(row["metadata_json"]),
            "supersedes_version": row["supersedes_version"],
            "status": row["status"],
            "status_reason": row["status_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "status_changed_at": row["status_changed_at"],
        }

    @staticmethod
    def _scoped(view: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
        return {key: value for key, value in view.items() if key in allowed}

    def _conclusion_links(self, connection, conclusion_id: str) -> list[tuple[str, str]]:
        rows = connection.execute(
            "SELECT evidence_type, evidence_id FROM conclusion_evidence "
            "WHERE conclusion_id=? ORDER BY position",
            (conclusion_id,),
        ).fetchall()
        return [(row["evidence_type"], row["evidence_id"]) for row in rows]

    def _validate_evidence(self, connection, organization_id: str,
                           evidence: Any) -> list[tuple[str, str]]:
        """校验结论引用的证据全部存在、属于本组织且未处于撤回/过期状态。"""

        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 50:
            raise ValidationError("evidence 必须是 1 到 50 条引用的列表")
        links: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in evidence:
            if not isinstance(item, dict):
                raise ValidationError("evidence 条目必须是对象")
            evidence_type = item.get("type")
            evidence_id = str(item.get("id", "")).strip()
            if evidence_type not in ("run", "judgment") or not evidence_id:
                raise ValidationError("evidence 条目必须包含有效的 type 与 id")
            if (evidence_type, evidence_id) in seen:
                raise ValidationError("evidence 中存在重复引用")
            seen.add((evidence_type, evidence_id))
            if evidence_type == "run":
                run = self._run_row(connection, evidence_id)
                if run["organization_id"] != organization_id:
                    raise PermissionDenied("不能引用其他组织的证据")
                if run["status"] in TERMINAL_STATUSES:
                    raise ValidationError("引用的运行记录已撤回或过期")
                version_row = self._dataset_version_row(
                    connection, run["dataset_id"], run["dataset_version"])
                if version_row["status"] in TERMINAL_STATUSES:
                    raise ValidationError("引用的运行记录基于已撤回或过期的数据集版本")
            else:
                judgment = self._judgment_row(connection, evidence_id)
                if judgment["organization_id"] != organization_id:
                    raise PermissionDenied("不能引用其他组织的证据")
                if judgment["status"] in TERMINAL_STATUSES:
                    raise ValidationError("引用的人工判定已撤回或过期")
                run = self._run_row(connection, judgment["run_id"])
                if run["status"] in TERMINAL_STATUSES:
                    raise ValidationError("引用判定所属的运行记录已撤回或过期")
                version_row = self._dataset_version_row(
                    connection, run["dataset_id"], run["dataset_version"])
                if version_row["status"] in TERMINAL_STATUSES:
                    raise ValidationError("引用判定所属的运行记录基于已撤回或过期的数据集版本")
            links.append((evidence_type, evidence_id))
        return links

    def _affecting_evidence(self, connection, conclusion_id: str) -> list[dict[str, Any]]:
        """实时计算结论引用链上已撤回或过期的证据。"""

        affecting: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(evidence_type: str, evidence_id: str, status: str, reason: str | None) -> None:
            key = (evidence_type, evidence_id)
            if key not in seen:
                seen.add(key)
                affecting.append({
                    "evidence_type": evidence_type,
                    "evidence_id": evidence_id,
                    "status": status,
                    "reason": reason,
                })

        def check_run(run_id: str) -> None:
            run = self._run_row(connection, run_id)
            if run["status"] in TERMINAL_STATUSES:
                add("run", run_id, run["status"], run["status_reason"])
            version_row = self._dataset_version_row(
                connection, run["dataset_id"], run["dataset_version"])
            if version_row["status"] in TERMINAL_STATUSES:
                add("dataset_version", f"{run['dataset_id']}:{run['dataset_version']}",
                    version_row["status"], version_row["status_reason"])

        for evidence_type, evidence_id in self._conclusion_links(connection, conclusion_id):
            if evidence_type == "run":
                check_run(evidence_id)
            else:
                judgment = self._judgment_row(connection, evidence_id)
                if judgment["status"] in TERMINAL_STATUSES:
                    add("judgment", evidence_id, judgment["status"], judgment["status_reason"])
                check_run(judgment["run_id"])
        return affecting

    def _basis_snapshot(self, connection, conclusion_id: str) -> list[dict[str, Any]]:
        """在发布时固化结论引用的原始依据。"""

        basis: list[dict[str, Any]] = []
        for evidence_type, evidence_id in self._conclusion_links(connection, conclusion_id):
            if evidence_type == "run":
                run = self._run_row(connection, evidence_id)
                version_row = self._dataset_version_row(
                    connection, run["dataset_id"], run["dataset_version"])
                basis.append({
                    "type": "run",
                    "run_id": run["run_id"],
                    "dataset_id": run["dataset_id"],
                    "dataset_version": run["dataset_version"],
                    "dataset_version_status": version_row["status"],
                    "run_status": run["status"],
                    "parameters_hash": run["parameters_hash"],
                    "result_hash": run["result_hash"],
                })
            else:
                judgment = self._judgment_row(connection, evidence_id)
                basis.append({
                    "type": "judgment",
                    "judgment_id": judgment["judgment_id"],
                    "run_id": judgment["run_id"],
                    "verdict": judgment["verdict"],
                    "judgment_status": judgment["status"],
                })
        return basis

    def _propagate(self, connection, evidence_type: str, evidence_id: Any,
                   event: str, reason: str, now: str) -> dict[str, Any]:
        """把撤回/过期事件传播到引用该证据的结论。

        已发布的结论生成幂等的影响说明；未定稿的结论不写入任何记录，
        其受影响状态在读取与发布时实时计算。
        """

        run_ids: set[str] = set()
        judgment_ids: set[str] = set()
        if evidence_type == "dataset_version":
            dataset_id, version = evidence_id
            rows = connection.execute(
                "SELECT run_id FROM run_records WHERE dataset_id=? AND dataset_version=?",
                (dataset_id, version),
            ).fetchall()
            run_ids = {row["run_id"] for row in rows}
            judgment_ids = self._judgments_on_runs(connection, run_ids)
            evidence_key = f"{dataset_id}:{version}"
        elif evidence_type == "run":
            run_ids = {evidence_id}
            judgment_ids = self._judgments_on_runs(connection, run_ids)
            evidence_key = evidence_id
        else:
            judgment_ids = {evidence_id}
            evidence_key = evidence_id

        conclusion_ids: set[str] = set()
        for run_id in run_ids:
            rows = connection.execute(
                "SELECT conclusion_id FROM conclusion_evidence "
                "WHERE evidence_type='run' AND evidence_id=?",
                (run_id,),
            ).fetchall()
            conclusion_ids.update(row["conclusion_id"] for row in rows)
        for judgment_id in judgment_ids:
            rows = connection.execute(
                "SELECT conclusion_id FROM conclusion_evidence "
                "WHERE evidence_type='judgment' AND evidence_id=?",
                (judgment_id,),
            ).fetchall()
            conclusion_ids.update(row["conclusion_id"] for row in rows)

        published: list[str] = []
        drafts: list[str] = []
        impact_ids: list[str] = []
        for conclusion_id in sorted(conclusion_ids):
            conclusion = self._conclusion_row(connection, conclusion_id)
            if conclusion["status"] != "published":
                drafts.append(conclusion_id)
                continue
            summary = self._impact_summary(
                connection, conclusion, evidence_type, evidence_id, event, reason, now, run_ids)
            impact_id = uuid.uuid4().hex
            cursor = connection.execute(
                "INSERT OR IGNORE INTO impact_statements(impact_id,conclusion_id,evidence_type,"
                "evidence_id,event,reason,summary,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (impact_id, conclusion_id, evidence_type, evidence_key, event, reason, summary, now),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT impact_id FROM impact_statements WHERE conclusion_id=? "
                    "AND evidence_type=? AND evidence_id=? AND event=?",
                    (conclusion_id, evidence_type, evidence_key, event),
                ).fetchone()
                impact_id = row["impact_id"]
            impact_ids.append(impact_id)
            published.append(conclusion_id)
        return {"evidence_key": evidence_key, "impact_ids": impact_ids,
                "published_conclusions": published, "draft_conclusions": drafts}

    @staticmethod
    def _judgments_on_runs(connection, run_ids: set[str]) -> set[str]:
        judgment_ids: set[str] = set()
        for run_id in run_ids:
            rows = connection.execute(
                "SELECT judgment_id FROM judgments WHERE run_id=?", (run_id,)
            ).fetchall()
            judgment_ids.update(row["judgment_id"] for row in rows)
        return judgment_ids

    def _impact_summary(self, connection, conclusion, evidence_type: str, evidence_id: Any,
                        event: str, reason: str, now: str, run_ids: set[str]) -> str:
        label = EVENT_LABELS[event]
        published_at = conclusion["published_at"]
        if evidence_type == "dataset_version":
            dataset_id, version = evidence_id
            rows = connection.execute(
                "SELECT evidence_id FROM conclusion_evidence "
                "WHERE conclusion_id=? AND evidence_type='run'",
                (conclusion["conclusion_id"],),
            ).fetchall()
            cited = sorted({row["evidence_id"] for row in rows} & run_ids)
            return (f"数据集 {dataset_id} 版本 {version} 于 {now} 被{label}（原因：{reason}）；"
                    f"该结论引用的运行记录 {('、'.join(cited)) or '无'} 基于该版本。"
                    f"结论发布于 {published_at}，原始依据保留不变，本说明用于后续复核。")
        if evidence_type == "run":
            run = self._run_row(connection, evidence_id)
            return (f"运行记录 {evidence_id}（数据集 {run['dataset_id']} 版本 "
                    f"{run['dataset_version']}）于 {now} 被{label}（原因：{reason}）。"
                    f"结论发布于 {published_at}，原始依据保留不变，本说明用于后续复核。")
        judgment = self._judgment_row(connection, evidence_id)
        return (f"人工判定 {evidence_id}（运行记录 {judgment['run_id']}）于 {now} "
                f"被{label}（原因：{reason}）。"
                f"结论发布于 {published_at}，原始依据保留不变，本说明用于后续复核。")

    # ---------- 数据集与版本链 ----------

    def register_dataset(self, *, request_id: str, actor_id: str, dataset_id: str,
                         organization_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "dataset_id": dataset_id,
                   "organization_id": organization_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            dataset_id = self._identifier(dataset_id, "dataset_id")
            name = self._text(name, "name")
            if connection.execute(
                    "SELECT 1 FROM organizations WHERE organization_id=?",
                    (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            self._check_org(actor, organization_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO datasets(dataset_id,organization_id,name,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (dataset_id, organization_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("数据集编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="dataset.registered",
                             resource_type="dataset", resource_id=dataset_id,
                             detail={"organization_id": organization_id, "name": name},
                             occurred_at=self._now())
                return "dataset", dataset_id, {"dataset_id": dataset_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_dataset", payload=payload, create=create)

    def register_dataset_version(self, *, request_id: str, actor_id: str, dataset_id: str,
                                 content: Any, metadata: dict[str, Any] | None = None,
                                 supersedes_version: int | None = None) -> WriteReceipt:
        metadata = metadata if metadata is not None else {}
        if not isinstance(metadata, dict):
            raise ValidationError("metadata 必须是对象")
        try:
            content_hash = digest(content)
        except (TypeError, ValueError) as exc:
            raise ValidationError("content 必须是可 JSON 序列化的内容") from exc
        if supersedes_version is not None and (
                not isinstance(supersedes_version, int) or supersedes_version < 1):
            raise ValidationError("supersedes_version 必须是正整数")
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "content": content,
                   "metadata": metadata, "supersedes_version": supersedes_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            dataset_id = self._identifier(dataset_id, "dataset_id")
            dataset = self._dataset_row(connection, dataset_id)
            self._check_org(actor, dataset["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
                    (dataset_id,),
                ).fetchone()
                if (latest is not None and supersedes_version is None
                        and latest["content_hash"] == content_hash
                        and json.loads(latest["metadata_json"]) == metadata):
                    # 相同内容的重复导入保持幂等，直接返回现有最新版本
                    version = latest["version"]
                    return "dataset_version", f"{dataset_id}:{version}", {
                        "dataset_id": dataset_id, "version": version}
                new_version = (latest["version"] if latest else 0) + 1
                supersedes = supersedes_version if supersedes_version is not None else (
                    latest["version"] if latest else None)
                if supersedes is not None:
                    if supersedes >= new_version:
                        raise ValidationError("supersedes_version 必须小于新版本号")
                    self._dataset_version_row(connection, dataset_id, supersedes)
                now = self._now()
                connection.execute(
                    "INSERT INTO dataset_versions(dataset_id,version,content_hash,metadata_json,"
                    "supersedes_version,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (dataset_id, new_version, content_hash, canonical_json(metadata),
                     supersedes, "active", actor_id, now),
                )
                if supersedes is not None:
                    connection.execute(
                        "UPDATE dataset_versions SET status='superseded', status_changed_at=? "
                        "WHERE dataset_id=? AND version=? AND status='active'",
                        (now, dataset_id, supersedes),
                    )
                append_event(connection, actor_id=actor_id, action="dataset_version.registered",
                             resource_type="dataset_version",
                             resource_id=f"{dataset_id}:{new_version}",
                             detail={"dataset_id": dataset_id, "version": new_version,
                                     "content_hash": content_hash, "supersedes_version": supersedes},
                             occurred_at=now)
                return "dataset_version", f"{dataset_id}:{new_version}", {
                    "dataset_id": dataset_id, "version": new_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_dataset_version", payload=payload, create=create)

    # ---------- 运行记录与人工判定 ----------

    def import_run(self, *, request_id: str, actor_id: str, external_key: str,
                   dataset_id: str, dataset_version: int, parameters: dict[str, Any],
                   result_summary: dict[str, Any],
                   internal_notes: str | None = None) -> WriteReceipt:
        if not isinstance(parameters, dict) or not parameters:
            raise ValidationError("parameters 必须是非空对象")
        if not isinstance(result_summary, dict) or not result_summary:
            raise ValidationError("result_summary 必须是非空对象")
        if not isinstance(dataset_version, int) or dataset_version < 1:
            raise ValidationError("dataset_version 必须是正整数")
        notes = self._text(internal_notes, "internal_notes", 500) if internal_notes is not None else None
        payload = {"actor_id": actor_id, "external_key": external_key, "dataset_id": dataset_id,
                   "dataset_version": dataset_version, "parameters": parameters,
                   "result_summary": result_summary, "internal_notes": notes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            dataset_id = self._identifier(dataset_id, "dataset_id")
            external_key = self._identifier(external_key, "external_key")
            dataset = self._dataset_row(connection, dataset_id)
            self._check_org(actor, dataset["organization_id"])
            version_row = self._dataset_version_row(connection, dataset_id, dataset_version)
            if version_row["status"] in TERMINAL_STATUSES:
                raise ValidationError("数据集版本已撤回或过期，不能登记运行记录")
            parameters_hash = digest(parameters)
            result_hash = digest(result_summary)
            content_hash = digest({
                "dataset_id": dataset_id, "dataset_version": dataset_version,
                "parameters": parameters, "result_summary": result_summary,
                "internal_notes": notes,
            })

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM run_records WHERE organization_id=? AND external_key=?",
                    (dataset["organization_id"], external_key),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("同一业务键已经登记不同内容")
                    # 相同证据的重复导入保持幂等
                    return "run", existing["run_id"], {"run_id": existing["run_id"]}
                run_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO run_records(run_id,organization_id,external_key,dataset_id,"
                    "dataset_version,parameters_json,parameters_hash,result_json,result_hash,"
                    "internal_notes,content_hash,status,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, dataset["organization_id"], external_key, dataset_id, dataset_version,
                     canonical_json(parameters), parameters_hash, canonical_json(result_summary),
                     result_hash, notes, content_hash, "active", actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="run.imported",
                             resource_type="run", resource_id=run_id,
                             detail={"external_key": external_key, "dataset_id": dataset_id,
                                     "dataset_version": dataset_version, "content_hash": content_hash},
                             occurred_at=now)
                return "run", run_id, {"run_id": run_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="import_run", payload=payload, create=create)

    def record_judgment(self, *, request_id: str, actor_id: str, external_key: str,
                        run_id: str, verdict: str, rationale: str) -> WriteReceipt:
        if verdict not in JUDGMENT_VERDICTS:
            raise ValidationError("verdict 不在允许范围内")
        rationale = self._text(rationale, "rationale", 500)
        payload = {"actor_id": actor_id, "external_key": external_key, "run_id": run_id,
                   "verdict": verdict, "rationale": rationale}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            external_key = self._identifier(external_key, "external_key")
            run = self._run_row(connection, run_id)
            self._check_org(actor, run["organization_id"])
            if run["status"] in TERMINAL_STATUSES:
                raise ValidationError("运行记录已撤回或过期，不能登记人工判定")
            content_hash = digest({"run_id": run_id, "verdict": verdict, "rationale": rationale})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM judgments WHERE organization_id=? AND external_key=?",
                    (run["organization_id"], external_key),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("同一业务键已经登记不同内容")
                    return "judgment", existing["judgment_id"], {"judgment_id": existing["judgment_id"]}
                judgment_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO judgments(judgment_id,organization_id,external_key,run_id,verdict,"
                    "rationale,content_hash,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (judgment_id, run["organization_id"], external_key, run_id, verdict,
                     rationale, content_hash, "active", actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="judgment.recorded",
                             resource_type="judgment", resource_id=judgment_id,
                             detail={"external_key": external_key, "run_id": run_id,
                                     "verdict": verdict, "content_hash": content_hash},
                             occurred_at=now)
                return "judgment", judgment_id, {"judgment_id": judgment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_judgment", payload=payload, create=create)

    # ---------- 结论生命周期 ----------

    def create_conclusion(self, *, request_id: str, actor_id: str, title: str,
                          statement: str, evidence: Any,
                          organization_id: str | None = None) -> WriteReceipt:
        title = self._text(title, "title")
        statement = self._text(statement, "statement", 2000)
        payload = {"actor_id": actor_id, "title": title, "statement": statement,
                   "evidence": evidence, "organization_id": organization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            target_org = organization_id or actor.organization_id
            if connection.execute(
                    "SELECT 1 FROM organizations WHERE organization_id=?",
                    (target_org,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            self._check_org(actor, target_org)
            links = self._validate_evidence(connection, target_org, evidence)

            def create() -> tuple[str, str, dict[str, Any]]:
                conclusion_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO conclusions(conclusion_id,organization_id,title,statement,revision,"
                    "status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (conclusion_id, target_org, title, statement, 1, "draft", actor_id, now),
                )
                for position, (evidence_type, evidence_id) in enumerate(links):
                    connection.execute(
                        "INSERT INTO conclusion_evidence(conclusion_id,evidence_type,evidence_id,"
                        "position) VALUES(?,?,?,?)",
                        (conclusion_id, evidence_type, evidence_id, position),
                    )
                append_event(connection, actor_id=actor_id, action="conclusion.created",
                             resource_type="conclusion", resource_id=conclusion_id,
                             detail={"title": title,
                                     "evidence": [f"{kind}:{eid}" for kind, eid in links]},
                             occurred_at=now)
                return "conclusion", conclusion_id, {"conclusion_id": conclusion_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_conclusion", payload=payload, create=create)

    def revise_conclusion(self, *, request_id: str, actor_id: str, conclusion_id: str,
                          title: str | None = None, statement: str | None = None,
                          evidence: Any = None) -> WriteReceipt:
        if title is None and statement is None and evidence is None:
            raise ValidationError("没有需要修订的内容")
        payload = {"actor_id": actor_id, "conclusion_id": conclusion_id, "title": title,
                   "statement": statement, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            conclusion = self._conclusion_row(connection, conclusion_id)
            self._check_org(actor, conclusion["organization_id"])
            if conclusion["status"] != "draft":
                raise ConflictError("已发布的结论不能修订")
            new_title = self._text(title, "title") if title is not None else conclusion["title"]
            new_statement = (self._text(statement, "statement", 2000)
                             if statement is not None else conclusion["statement"])
            links = (self._validate_evidence(connection, conclusion["organization_id"], evidence)
                     if evidence is not None else None)

            def create() -> tuple[str, str, dict[str, Any]]:
                new_revision = conclusion["revision"] + 1
                connection.execute(
                    "UPDATE conclusions SET title=?, statement=?, revision=? WHERE conclusion_id=?",
                    (new_title, new_statement, new_revision, conclusion_id),
                )
                if links is not None:
                    connection.execute(
                        "DELETE FROM conclusion_evidence WHERE conclusion_id=?", (conclusion_id,))
                    for position, (evidence_type, evidence_id) in enumerate(links):
                        connection.execute(
                            "INSERT INTO conclusion_evidence(conclusion_id,evidence_type,evidence_id,"
                            "position) VALUES(?,?,?,?)",
                            (conclusion_id, evidence_type, evidence_id, position),
                        )
                append_event(connection, actor_id=actor_id, action="conclusion.revised",
                             resource_type="conclusion", resource_id=conclusion_id,
                             detail={"revision": new_revision}, occurred_at=self._now())
                return "conclusion", conclusion_id, {
                    "conclusion_id": conclusion_id, "revision": new_revision}

            return self._idempotent(connection, request_id=request_id,
                                    action="revise_conclusion", payload=payload, create=create)

    def publish_conclusion(self, *, request_id: str, actor_id: str,
                           conclusion_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "conclusion_id": conclusion_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            conclusion = self._conclusion_row(connection, conclusion_id)
            self._check_org(actor, conclusion["organization_id"])
            if conclusion["status"] != "draft":
                raise ConflictError("结论已经发布")
            affecting = self._affecting_evidence(connection, conclusion_id)
            if affecting:
                raise ConflictError("结论引用的证据已撤回或过期，请先修订")

            def create() -> tuple[str, str, dict[str, Any]]:
                basis = self._basis_snapshot(connection, conclusion_id)
                now = self._now()
                connection.execute(
                    "UPDATE conclusions SET status='published', published_at=?, basis_json=? "
                    "WHERE conclusion_id=?",
                    (now, canonical_json(basis), conclusion_id),
                )
                append_event(connection, actor_id=actor_id, action="conclusion.published",
                             resource_type="conclusion", resource_id=conclusion_id,
                             detail={"basis": basis}, occurred_at=now)
                return "conclusion", conclusion_id, {
                    "conclusion_id": conclusion_id, "status": "published"}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_conclusion", payload=payload, create=create)

    # ---------- 证据状态变更（撤回 / 过期） ----------

    def set_dataset_version_status(self, *, request_id: str, actor_id: str, dataset_id: str,
                                   version: int, status: str, reason: str) -> WriteReceipt:
        if status not in TERMINAL_STATUSES:
            raise ValidationError("status 只能是 retracted 或 expired")
        if not isinstance(version, int) or version < 1:
            raise ValidationError("version 必须是正整数")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "dataset_id": dataset_id, "version": version,
                   "status": status, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            version_row = self._dataset_version_row(connection, dataset_id, version)

            def create() -> tuple[str, str, dict[str, Any]]:
                resource_id = f"{dataset_id}:{version}"
                current = version_row["status"]
                if current == status:
                    if (version_row["status_reason"] or "") != reason:
                        raise ConflictError("证据已处于相同状态但原因不同")
                    return "dataset_version", resource_id, {
                        "dataset_id": dataset_id, "version": version, "status": status}
                if current in TERMINAL_STATUSES:
                    raise ConflictError("证据已处于终态，不能再次变更")
                now = self._now()
                connection.execute(
                    "UPDATE dataset_versions SET status=?, status_reason=?, status_changed_at=? "
                    "WHERE dataset_id=? AND version=?",
                    (status, reason, now, dataset_id, version),
                )
                impact = self._propagate(connection, "dataset_version",
                                         (dataset_id, version), status, reason, now)
                append_event(connection, actor_id=actor_id, action="evidence.status_changed",
                             resource_type="dataset_version", resource_id=resource_id,
                             detail={"status": status, "reason": reason, **impact},
                             occurred_at=now)
                return "dataset_version", resource_id, {
                    "dataset_id": dataset_id, "version": version, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_dataset_version_status",
                                    payload=payload, create=create)

    def set_run_status(self, *, request_id: str, actor_id: str, run_id: str,
                       status: str, reason: str) -> WriteReceipt:
        if status not in TERMINAL_STATUSES:
            raise ValidationError("status 只能是 retracted 或 expired")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "run_id": run_id, "status": status, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            run = self._run_row(connection, run_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                current = run["status"]
                if current == status:
                    if (run["status_reason"] or "") != reason:
                        raise ConflictError("证据已处于相同状态但原因不同")
                    return "run", run_id, {"run_id": run_id, "status": status}
                if current in TERMINAL_STATUSES:
                    raise ConflictError("证据已处于终态，不能再次变更")
                now = self._now()
                connection.execute(
                    "UPDATE run_records SET status=?, status_reason=?, status_changed_at=? "
                    "WHERE run_id=?",
                    (status, reason, now, run_id),
                )
                impact = self._propagate(connection, "run", run_id, status, reason, now)
                append_event(connection, actor_id=actor_id, action="evidence.status_changed",
                             resource_type="run", resource_id=run_id,
                             detail={"status": status, "reason": reason, **impact},
                             occurred_at=now)
                return "run", run_id, {"run_id": run_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_run_status", payload=payload, create=create)

    def set_judgment_status(self, *, request_id: str, actor_id: str, judgment_id: str,
                            status: str, reason: str) -> WriteReceipt:
        if status not in TERMINAL_STATUSES:
            raise ValidationError("status 只能是 retracted 或 expired")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "judgment_id": judgment_id,
                   "status": status, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            judgment = self._judgment_row(connection, judgment_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                current = judgment["status"]
                if current == status:
                    if (judgment["status_reason"] or "") != reason:
                        raise ConflictError("证据已处于相同状态但原因不同")
                    return "judgment", judgment_id, {"judgment_id": judgment_id, "status": status}
                if current in TERMINAL_STATUSES:
                    raise ConflictError("证据已处于终态，不能再次变更")
                now = self._now()
                connection.execute(
                    "UPDATE judgments SET status=?, status_reason=?, status_changed_at=? "
                    "WHERE judgment_id=?",
                    (status, reason, now, judgment_id),
                )
                impact = self._propagate(connection, "judgment", judgment_id, status, reason, now)
                append_event(connection, actor_id=actor_id, action="evidence.status_changed",
                             resource_type="judgment", resource_id=judgment_id,
                             detail={"status": status, "reason": reason, **impact},
                             occurred_at=now)
                return "judgment", judgment_id, {"judgment_id": judgment_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_judgment_status", payload=payload, create=create)

    # ---------- 分级字段视图与反查 ----------

    def list_dataset_versions(self, actor_id: str, dataset_id: str) -> list[dict[str, Any]]:
        """按版本顺序返回数据集版本链，字段范围随操作者角色收敛。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        dataset = self._dataset_row(connection, dataset_id)
        self._check_org(actor, dataset["organization_id"])
        allowed = DATASET_VERSION_ROLE_FIELDS[actor.role]
        rows = connection.execute(
            "SELECT * FROM dataset_versions WHERE dataset_id=? ORDER BY version", (dataset_id,)
        ).fetchall()
        return [self._scoped(self._dataset_version_view(row), allowed) for row in rows]

    def get_run(self, actor_id: str, run_id: str) -> dict[str, Any]:
        """返回按角色裁剪后的运行记录视图。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        run = self._run_row(connection, run_id)
        self._check_org(actor, run["organization_id"])
        return self._scoped(self._run_view(connection, run), RUN_ROLE_FIELDS[actor.role])

    def get_conclusion(self, actor_id: str, conclusion_id: str) -> dict[str, Any]:
        """返回结论详情：当前引用状态、影响中的证据、发布依据快照与影响说明。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        conclusion = self._conclusion_row(connection, conclusion_id)
        self._check_org(actor, conclusion["organization_id"])
        evidence: list[dict[str, Any]] = []
        for evidence_type, evidence_id in self._conclusion_links(connection, conclusion_id):
            if evidence_type == "run":
                run = self._run_row(connection, evidence_id)
                version_row = self._dataset_version_row(
                    connection, run["dataset_id"], run["dataset_version"])
                evidence.append({
                    "evidence_type": "run",
                    "evidence_id": evidence_id,
                    "current_status": run["status"],
                    "dataset_id": run["dataset_id"],
                    "dataset_version": run["dataset_version"],
                    "dataset_version_status": version_row["status"],
                })
            else:
                judgment = self._judgment_row(connection, evidence_id)
                run = self._run_row(connection, judgment["run_id"])
                evidence.append({
                    "evidence_type": "judgment",
                    "evidence_id": evidence_id,
                    "current_status": judgment["status"],
                    "run_id": judgment["run_id"],
                    "run_status": run["status"],
                })
        impacts = connection.execute(
            "SELECT * FROM impact_statements WHERE conclusion_id=? ORDER BY created_at, impact_id",
            (conclusion_id,),
        ).fetchall()
        return {
            "conclusion_id": conclusion["conclusion_id"],
            "organization_id": conclusion["organization_id"],
            "title": conclusion["title"],
            "statement": conclusion["statement"],
            "revision": conclusion["revision"],
            "status": conclusion["status"],
            "created_by": conclusion["created_by"],
            "created_at": conclusion["created_at"],
            "published_at": conclusion["published_at"],
            "evidence": evidence,
            "affecting_evidence": (self._affecting_evidence(connection, conclusion_id)
                                   if conclusion["status"] == "draft" else []),
            "basis": json.loads(conclusion["basis_json"]) if conclusion["basis_json"] else None,
            "impact_statements": [
                {"impact_id": row["impact_id"], "evidence_type": row["evidence_type"],
                 "evidence_id": row["evidence_id"], "event": row["event"],
                 "reason": row["reason"], "summary": row["summary"],
                 "created_at": row["created_at"]}
                for row in impacts
            ],
        }

    def conclusion_runs(self, actor_id: str, conclusion_id: str,
                        affected_only: bool = False) -> dict[str, Any]:
        """按结论反查全部相关运行记录，并标注每条记录当前是否影响该结论。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        conclusion = self._conclusion_row(connection, conclusion_id)
        self._check_org(actor, conclusion["organization_id"])

        entries: dict[str, dict[str, Any]] = {}
        for evidence_type, evidence_id in self._conclusion_links(connection, conclusion_id):
            if evidence_type == "run":
                entry = entries.setdefault(evidence_id, {"linked_via": set(), "judgment_ids": []})
                entry["linked_via"].add("direct")
            else:
                judgment = self._judgment_row(connection, evidence_id)
                entry = entries.setdefault(
                    judgment["run_id"], {"linked_via": set(), "judgment_ids": []})
                entry["linked_via"].add(f"judgment:{evidence_id}")
                entry["judgment_ids"].append(evidence_id)

        items: list[dict[str, Any]] = []
        for run_id, entry in entries.items():
            run = self._run_row(connection, run_id)
            version_row = self._dataset_version_row(
                connection, run["dataset_id"], run["dataset_version"])
            latest = connection.execute(
                "SELECT MAX(version) AS latest FROM dataset_versions WHERE dataset_id=?",
                (run["dataset_id"],),
            ).fetchone()["latest"]
            successor = connection.execute(
                "SELECT version FROM dataset_versions WHERE dataset_id=? AND supersedes_version=?",
                (run["dataset_id"], run["dataset_version"]),
            ).fetchone()
            judgments = [
                self._scoped(self._judgment_view(self._judgment_row(connection, judgment_id)),
                             JUDGMENT_ROLE_FIELDS[actor.role])
                for judgment_id in entry["judgment_ids"]
            ]
            affects = (
                run["status"] in TERMINAL_STATUSES
                or version_row["status"] in TERMINAL_STATUSES
                or any(self._judgment_row(connection, judgment_id)["status"] in TERMINAL_STATUSES
                       for judgment_id in entry["judgment_ids"])
            )
            items.append({
                "run": self._scoped(self._run_view(connection, run), RUN_ROLE_FIELDS[actor.role]),
                "linked_via": sorted(entry["linked_via"]),
                "cited_judgments": judgments,
                "dataset": {
                    "dataset_id": run["dataset_id"],
                    "dataset_version": run["dataset_version"],
                    "dataset_version_status": version_row["status"],
                    "latest_version": latest,
                    "superseded_by": successor["version"] if successor else None,
                },
                "affects_conclusion": affects,
            })
        if affected_only:
            items = [item for item in items if item["affects_conclusion"]]
        return {
            "conclusion_id": conclusion_id,
            "conclusion_status": conclusion["status"],
            "items": items,
        }
