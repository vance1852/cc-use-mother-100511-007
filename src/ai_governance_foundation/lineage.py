"""提供证据、运行、结论之间的可追溯版本链与影响分析。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Iterable

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    BasisLink,
    ConclusionRecord,
    EvidenceRecord,
    ImpactStatement,
    RunRecord,
    WriteReceipt,
)
from .service import DomainService


EVIDENCE_TYPES = frozenset({
    "test_dataset",
    "run_parameters",
    "result_summary",
    "manual_judgment",
})

BASIS_ROLES = frozenset({
    "dataset",
    "parameters",
    "result",
    "manual_judgment",
    "supporting",
})

# 不同角色在不同证据类型上可见的载荷字段；"*" 表示全部可见。
_ALL = frozenset({"*"})
FIELD_POLICY: dict[str, dict[str, frozenset[str]]] = {
    "test_dataset": {
        "admin": _ALL,
        "auditor": _ALL,
        "operator": frozenset(
            {"name", "version_source", "record_count", "checksum", "description", "storage_location"}
        ),
        "reviewer": frozenset({"name", "version_source", "record_count", "checksum", "description"}),
    },
    "run_parameters": {
        "admin": _ALL,
        "auditor": _ALL,
        "operator": frozenset({"name", "model", "threshold", "seed", "config", "endpoint"}),
        "reviewer": frozenset({"name", "model", "threshold", "config"}),
    },
    "result_summary": {
        "admin": _ALL,
        "auditor": _ALL,
        "operator": frozenset({"name", "metrics", "passed", "sample_failures", "raw_artifact"}),
        "reviewer": frozenset({"name", "metrics", "passed", "sample_failures"}),
    },
    "manual_judgment": {
        "admin": _ALL,
        "auditor": _ALL,
        "operator": frozenset({"name", "decision", "judge"}),
        "reviewer": frozenset({"name", "decision", "judge", "rationale", "conflict_notes"}),
    },
}

# 结论正文中仅内部角色可见的字段前缀。
CONCLUSION_INTERNAL_PREFIX = "internal_"

WRITE_ROLES_BY_TYPE = {
    "test_dataset": ("admin", "operator"),
    "run_parameters": ("admin", "operator"),
    "result_summary": ("admin", "operator"),
    "manual_judgment": ("admin", "reviewer"),
}


def redact_payload(evidence_type: str, payload: dict[str, Any] | None,
                   role: str) -> tuple[dict[str, Any] | None, bool]:
    """按角色裁剪证据载荷，返回裁剪后的载荷与是否发生遮蔽。"""

    allowed = FIELD_POLICY.get(evidence_type, {}).get(role)
    if allowed is None:
        return None, payload is not None
    if "*" in allowed or payload is None:
        return payload, False
    kept = {key: value for key, value in payload.items() if key in allowed}
    return kept, len(kept) != len(payload)


def redact_content(content: dict[str, Any], role: str) -> tuple[dict[str, Any], bool]:
    """按角色裁剪结论正文，internal_ 前缀字段仅 admin/auditor 可见。"""

    if role in ("admin", "auditor"):
        return content, False
    kept = {key: value for key, value in content.items()
            if not key.startswith(CONCLUSION_INTERNAL_PREFIX)}
    return kept, len(kept) != len(content)


class LineageService(DomainService):
    """在基础服务之上实现证据谱系、定稿保护与影响说明。"""

    # ------------------------------------------------------------------ 工具

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]] | tuple[str, str, dict[str, Any], bool]]
                    ) -> WriteReceipt:
        """与基础服务一致的请求幂等；create 可额外返回布尔表示业务层自然去重。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        result = create()
        resource_type, resource_id, response = result[0], result[1], result[2]
        natural_dedup = result[3] if len(result) > 3 else False
        if natural_dedup:
            response = {**response, "natural_dedup": True}
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _check_site(self, connection, actor, site_id: str, *, write: bool = False):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        # admin 与 auditor 可以跨组织调阅；跨组织写入仍只允许 admin。
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            if write or actor.role != "auditor":
                raise PermissionDenied("不能访问其他组织的场所")
        return site

    def _effective_status(self, row, now: str | None = None) -> str:
        now = now or self._now()
        if row["status"] == "active" and row["expires_at"] and row["expires_at"] <= now:
            return "expired"
        return row["status"]

    def _load_evidence_row(self, connection, evidence_id: str):
        row = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError("证据不存在")
        return row

    def _view_evidence(self, connection, row, actor) -> EvidenceRecord:
        effective = self._effective_status(row)
        payload, redacted = redact_payload(row["evidence_type"], json.loads(row["payload_json"]), actor.role)
        return EvidenceRecord(
            row["evidence_id"], row["site_id"], row["evidence_key"], row["evidence_type"],
            row["version"], payload, row["payload_hash"], effective,
            row["supersedes"], row["replaced_by"], row["expires_at"],
            row["retraction_reason"], row["retracted_by"], row["retracted_at"],
            row["created_by"], row["created_at"], payload_redacted=redacted,
        )

    def _view_conclusion(self, connection, row, actor) -> ConclusionRecord:
        basis = tuple(
            BasisLink(item["evidence_id"], item["basis_role"], item["snapshot_hash"])
            for item in connection.execute(
                "SELECT * FROM conclusion_basis WHERE conclusion_id=? ORDER BY basis_role, evidence_id",
                (row["conclusion_id"],),
            )
        )
        content, redacted = redact_content(json.loads(row["content_json"]), actor.role)
        invalidation = json.loads(row["invalidation_json"]) if row["invalidation_json"] else {}
        if actor.role not in ("admin", "auditor") and invalidation:
            invalidation = {key: value for key, value in invalidation.items() if key != "internal_note"}
        return ConclusionRecord(
            row["conclusion_id"], row["site_id"], row["conclusion_key"], row["version"],
            row["title"], content, row["status"], row["supersedes"], row["published_at"],
            row["invalidated_at"], invalidation, row["created_by"], row["created_at"],
            basis=basis, content_redacted=redacted,
        )

    def _statement(self, row) -> ImpactStatement:
        return ImpactStatement(
            row["statement_id"], row["site_id"], row["trigger_evidence_id"], row["trigger_status"],
            row["conclusion_id"], row["run_id"], row["scope"], row["message"],
            json.loads(row["detail_json"]), row["created_by"], row["created_at"],
        )

    def _add_impact(self, connection, *, site_id: str, trigger_evidence_id: str,
                    trigger_status: str, scope: str, message: str, detail: dict[str, Any],
                    conclusion_id: str | None = None, run_id: str | None = None,
                    actor_id: str, now: str) -> str:
        statement_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO impact_statements(statement_id,site_id,trigger_evidence_id,trigger_status,"
            "conclusion_id,run_id,scope,message,detail_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (statement_id, site_id, trigger_evidence_id, trigger_status, conclusion_id, run_id,
             scope, message, canonical_json(detail), actor_id, now),
        )
        return statement_id

    def _closure(self, connection, seed: Iterable[str], site_id: str):
        """通过运行记录在证据图上做无向扩散，返回受波及的证据集合与运行集合。"""

        evidence_ids = set(seed)
        runs: dict[str, Any] = {}
        pending = True
        while pending:
            pending = False
            placeholders = ",".join("?" for _ in evidence_ids)
            rows = connection.execute(
                f"SELECT * FROM runs WHERE site_id=? AND ("
                f"dataset_id IN ({placeholders}) OR parameters_id IN ({placeholders}) "
                f"OR (result_id IS NOT NULL AND result_id IN ({placeholders})))",
                [site_id, *evidence_ids, *evidence_ids, *evidence_ids],
            ).fetchall()
            for row in rows:
                if row["run_id"] in runs:
                    continue
                runs[row["run_id"]] = row
                for ref in (row["dataset_id"], row["parameters_id"], row["result_id"]):
                    if ref and ref not in evidence_ids:
                        evidence_ids.add(ref)
                        pending = True
        return evidence_ids, runs

    def _apply_status_change(self, connection, *, actor_id: str, row, new_status: str,
                             reason: str, action: str, now: str) -> dict[str, Any]:
        """把证据置为 expired/retracted，并让影响只波及其应波及的结论。"""

        site_id = row["site_id"]
        evidence_ref = f"{row['evidence_key']}@v{row['version']}"
        connection.execute(
            "UPDATE evidence SET status=?, retraction_reason=?, retracted_by=?, retracted_at=? "
            "WHERE evidence_id=?",
            (new_status, reason, actor_id, now, row["evidence_id"]),
        )
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type="evidence", resource_id=row["evidence_id"],
                     detail={"site_id": site_id, "evidence_key": row["evidence_key"],
                             "version": row["version"], "status": new_status, "reason": reason},
                     occurred_at=now)

        evidence_ids, runs = self._closure(connection, [row["evidence_id"]], site_id)
        run_ids: list[str] = []
        for run_id, run_row in runs.items():
            run_ids.append(run_id)
            self._add_impact(
                connection, site_id=site_id, trigger_evidence_id=row["evidence_id"],
                trigger_status=new_status, scope="run_affected", run_id=run_id,
                message=f"运行记录 {run_row['client_run_key']} 引用的证据 {evidence_ref} 已{self._status_label(new_status)}，"
                        f"运行记录保留原始输入并标记为受影响。",
                detail={"client_run_key": run_row["client_run_key"],
                        "evidence_key": row["evidence_key"], "evidence_version": row["version"]},
                actor_id=actor_id, now=now,
            )

        placeholders = ",".join("?" for _ in evidence_ids) or "''"
        conclusion_rows = connection.execute(
            f"SELECT c.* FROM conclusion_basis b JOIN conclusions c ON c.conclusion_id=b.conclusion_id "
            f"WHERE b.evidence_id IN ({placeholders})",
            tuple(evidence_ids),
        ).fetchall()
        draft_ids: list[str] = []
        published_ids: list[str] = []
        for conclusion_row in conclusion_rows:
            if conclusion_row["status"] == "draft":
                connection.execute(
                    "UPDATE conclusions SET status='invalidated', invalidated_at=?, invalidation_json=? "
                    "WHERE conclusion_id=?",
                    (now, canonical_json({
                        "trigger_evidence_id": row["evidence_id"],
                        "evidence_key": row["evidence_key"],
                        "evidence_version": row["version"],
                        "trigger_status": new_status,
                        "reason": reason,
                    }), conclusion_row["conclusion_id"]),
                )
                draft_ids.append(conclusion_row["conclusion_id"])
                self._add_impact(
                    connection, site_id=site_id, trigger_evidence_id=row["evidence_id"],
                    trigger_status=new_status, scope="draft_invalidated",
                    conclusion_id=conclusion_row["conclusion_id"],
                    message=f"结论 {conclusion_row['conclusion_key']}@v{conclusion_row['version']} 尚未定稿，"
                            f"其依据 {evidence_ref} 已{self._status_label(new_status)}，草稿已标记为失效，需更换依据后重开版本。",
                    detail={"conclusion_key": conclusion_row["conclusion_key"],
                            "conclusion_version": conclusion_row["version"],
                            "evidence_key": row["evidence_key"], "evidence_version": row["version"]},
                    actor_id=actor_id, now=now,
                )
                append_event(connection, actor_id=actor_id, action="conclusion.invalidated",
                             resource_type="conclusion", resource_id=conclusion_row["conclusion_id"],
                             detail={"trigger_evidence_id": row["evidence_id"], "trigger_status": new_status},
                             occurred_at=now)
            elif conclusion_row["status"] == "published":
                published_ids.append(conclusion_row["conclusion_id"])
                self._add_impact(
                    connection, site_id=site_id, trigger_evidence_id=row["evidence_id"],
                    trigger_status=new_status, scope="published_preserved",
                    conclusion_id=conclusion_row["conclusion_id"],
                    message=f"结论 {conclusion_row['conclusion_key']}@v{conclusion_row['version']} 已经发布，"
                            f"原始依据 {evidence_ref} 按定格快照保留；证据{self._status_label(new_status)}"
                            f"不改变已发布结论，建议在新版报告中复核。",
                    detail={"conclusion_key": conclusion_row["conclusion_key"],
                            "conclusion_version": conclusion_row["version"],
                            "published_at": conclusion_row["published_at"],
                            "evidence_key": row["evidence_key"], "evidence_version": row["version"],
                            "reason": reason},
                    actor_id=actor_id, now=now,
                )
        return {"run_ids": run_ids, "draft_ids": draft_ids, "published_ids": published_ids}

    @staticmethod
    def _status_label(status: str) -> str:
        return {"expired": "过期或被新版本替代", "retracted": "撤回"}[status]

    # ------------------------------------------------------------------ 证据

    def import_evidence(self, *, request_id: str, actor_id: str, site_id: str,
                        evidence_key: str, evidence_type: str, payload: dict[str, Any],
                        supersedes: str | None = None,
                        expires_at: str | None = None) -> WriteReceipt:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        if evidence_type not in EVIDENCE_TYPES:
            raise ValidationError("evidence_type 不在允许范围内")
        request_payload = {"actor_id": actor_id, "site_id": site_id, "evidence_key": evidence_key,
                           "evidence_type": evidence_type, "payload": payload,
                           "supersedes": supersedes, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *WRITE_ROLES_BY_TYPE[evidence_type])
            self._check_site(connection, actor, site_id, write=True)
            evidence_key = self._identifier(evidence_key, "evidence_key")
            payload_text = canonical_json(payload)
            payload_digest = digest(payload)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any], bool]:
                duplicate = connection.execute(
                    "SELECT * FROM evidence WHERE site_id=? AND evidence_key=? "
                    "AND evidence_type=? AND payload_hash=?",
                    (site_id, evidence_key, evidence_type, payload_digest),
                ).fetchone()
                if duplicate is not None:
                    return "evidence", duplicate["evidence_id"], {
                        "evidence_id": duplicate["evidence_id"],
                        "version": duplicate["version"],
                    }, True

                latest = connection.execute(
                    "SELECT * FROM evidence WHERE site_id=? AND evidence_key=? "
                    "ORDER BY version DESC LIMIT 1",
                    (site_id, evidence_key),
                ).fetchone()
                if latest is not None and latest["evidence_type"] != evidence_type:
                    raise ConflictError("同一证据键不能登记不同证据类型")
                if supersedes is not None and (latest is None or latest["evidence_id"] != supersedes):
                    raise ConflictError("supersedes 必须指向该证据键的最新版本")
                target = latest
                if target is not None and self._effective_status(target, now) != "active":
                    raise ConflictError("只能替代仍然有效的证据版本")
                version = (target["version"] + 1) if target is not None else 1
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO evidence(evidence_id,site_id,evidence_key,evidence_type,version,"
                    "payload_json,payload_hash,status,supersedes,replaced_by,expires_at,"
                    "retraction_reason,retracted_by,retracted_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'active',?,NULL,?,'','',NULL,?,?)",
                    (evidence_id, site_id, evidence_key, evidence_type, version,
                     payload_text, payload_digest,
                     target["evidence_id"] if target else None, expires_at, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="evidence.imported",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"site_id": site_id, "evidence_key": evidence_key,
                                     "evidence_type": evidence_type, "version": version,
                                     "payload_hash": payload_digest,
                                     "supersedes": target["evidence_id"] if target else None},
                             occurred_at=now)
                if target is not None:
                    self._apply_status_change(
                        connection, actor_id=actor_id, row=target,
                        new_status="expired", reason=f"被 v{version} 替代",
                        action="evidence.superseded", now=now,
                    )
                    connection.execute(
                        "UPDATE evidence SET replaced_by=? WHERE evidence_id=?",
                        (evidence_id, target["evidence_id"]),
                    )
                return "evidence", evidence_id, {
                    "evidence_id": evidence_id, "version": version,
                    "superseded": target["evidence_id"] if target else None,
                }, False

            return self._idempotent(connection, request_id=request_id,
                                    action="import_evidence", payload=request_payload, create=create)

    def _change_status(self, *, action_name: str, audit_action: str, request_id: str,
                       actor_id: str, evidence_id: str, new_status: str,
                       reason: str, allowed_roles: tuple[str, ...]) -> WriteReceipt:
        reason = str(reason or "").strip()
        if new_status == "retracted" and not reason:
            raise ValidationError("撤回原因不能为空")
        request_payload = {"actor_id": actor_id, "evidence_id": evidence_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *allowed_roles)
            row = self._load_evidence_row(connection, evidence_id)
            self._check_site(connection, actor, row["site_id"], write=True)
            now = self._now()
            if self._effective_status(row, now) != "active":
                raise ConflictError("证据已经失效，不能重复变更状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                effect = self._apply_status_change(
                    connection, actor_id=actor_id, row=row, new_status=new_status,
                    reason=reason, action=audit_action, now=now,
                )
                return "evidence", evidence_id, {"evidence_id": evidence_id, "effect": effect}

            return self._idempotent(connection, request_id=request_id,
                                    action=action_name, payload=request_payload, create=create)

    def retract_evidence(self, *, request_id: str, actor_id: str,
                         evidence_id: str, reason: str) -> WriteReceipt:
        """撤回一条仍然有效的证据，并联动失效草稿、保留发布稿。"""

        return self._change_status(action_name="retract_evidence", audit_action="evidence.retracted",
                                   request_id=request_id, actor_id=actor_id, evidence_id=evidence_id,
                                   new_status="retracted", reason=reason,
                                   allowed_roles=("admin", "reviewer"))

    def expire_evidence(self, *, request_id: str, actor_id: str,
                        evidence_id: str, reason: str = "") -> WriteReceipt:
        """手工把证据标记为过期。"""

        return self._change_status(action_name="expire_evidence", audit_action="evidence.expired",
                                   request_id=request_id, actor_id=actor_id, evidence_id=evidence_id,
                                   new_status="expired", reason=reason,
                                   allowed_roles=("admin", "reviewer", "operator"))

    def sweep_expired(self, *, actor_id: str) -> dict[str, Any]:
        """把所有已到 expires_at 的活动证据批量转为过期并生成影响说明。"""

        transitioned: list[str] = []
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            now = self._now()
            if actor.role == "admin":
                rows = connection.execute(
                    "SELECT e.* FROM evidence e WHERE e.status='active' "
                    "AND e.expires_at IS NOT NULL AND e.expires_at<=?",
                    (now,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT e.* FROM evidence e JOIN sites s ON s.site_id=e.site_id "
                    "WHERE e.status='active' AND e.expires_at IS NOT NULL AND e.expires_at<=? "
                    "AND s.organization_id=?",
                    (now, actor.organization_id),
                ).fetchall()
            for row in rows:
                effect = self._apply_status_change(
                    connection, actor_id=actor_id, row=row, new_status="expired",
                    reason="超过登记的失效时间", action="evidence.expired", now=now,
                )
                transitioned.append({"evidence_id": row["evidence_id"], "effect": effect})
        return {"transitioned": transitioned, "count": len(transitioned)}

    def get_evidence(self, *, actor_id: str, evidence_id: str) -> EvidenceRecord:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = self._load_evidence_row(connection, evidence_id)
            self._check_site(connection, actor, row["site_id"])
            return self._view_evidence(connection, row, actor)

    def list_evidence(self, *, actor_id: str, site_id: str, evidence_key: str | None = None,
                      evidence_type: str | None = None, status: str | None = None) -> list[EvidenceRecord]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM evidence WHERE site_id=?"
        if evidence_key:
            query += " AND evidence_key=?"
            parameters.append(evidence_key)
        if evidence_type:
            if evidence_type not in EVIDENCE_TYPES:
                raise ValidationError("evidence_type 不在允许范围内")
            query += " AND evidence_type=?"
            parameters.append(evidence_type)
        query += " ORDER BY evidence_key, version"
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._check_site(connection, actor, site_id)
            now = self._now()
            items = []
            for row in connection.execute(query, parameters):
                effective = self._effective_status(row, now)
                if status and effective != status:
                    continue
                items.append(self._view_evidence(connection, row, actor))
            return items

    def evidence_lineage(self, *, actor_id: str, evidence_id: str) -> dict[str, Any]:
        """返回单条证据的版本链、关联运行与关联结论。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = self._load_evidence_row(connection, evidence_id)
            self._check_site(connection, actor, row["site_id"])
            versions = [
                self._view_evidence(connection, item, actor)
                for item in connection.execute(
                    "SELECT * FROM evidence WHERE site_id=? AND evidence_key=? ORDER BY version",
                    (row["site_id"], row["evidence_key"]),
                )
            ]
            evidence_ids, runs = self._closure(connection, [evidence_id], row["site_id"])
            run_items = [self._run_record(item) for item in runs.values()]
            placeholders = ",".join("?" for _ in evidence_ids)
            conclusions = [
                {"conclusion_id": item["conclusion_id"], "conclusion_key": item["conclusion_key"],
                 "version": item["version"], "status": item["status"]}
                for item in connection.execute(
                    f"SELECT DISTINCT c.* FROM conclusion_basis b JOIN conclusions c "
                    f"ON c.conclusion_id=b.conclusion_id WHERE b.evidence_id IN ({placeholders})",
                    tuple(evidence_ids),
                )
            ]
            links = [
                {"source_id": item["source_id"], "target_id": item["target_id"],
                 "relation": item["relation"]}
                for item in connection.execute(
                    "SELECT * FROM evidence_links WHERE source_id=? OR target_id=? ORDER BY relation",
                    (evidence_id, evidence_id),
                )
            ]
            return {
                "evidence": self._view_evidence(connection, row, actor).__dict__,
                "versions": [item.__dict__ for item in versions],
                "links": links,
                "runs": [item.__dict__ for item in run_items],
                "conclusions": conclusions,
            }

    # ------------------------------------------------------------------ 运行

    @staticmethod
    def _run_record(row) -> RunRecord:
        return RunRecord(row["run_id"], row["site_id"], row["client_run_key"],
                         row["dataset_id"], row["parameters_id"], row["result_id"],
                         row["note"], row["created_by"], row["created_at"])

    def _run_evidence(self, connection, actor, evidence_id: str, site_id: str,
                      expected_type: str, must_be_active: bool, now: str):
        row = self._load_evidence_row(connection, evidence_id)
        if row["site_id"] != site_id:
            raise ValidationError(f"{expected_type} 证据不属于该场所")
        if row["evidence_type"] != expected_type:
            raise ValidationError(f"{evidence_id} 不是 {expected_type} 类型证据")
        if must_be_active and self._effective_status(row, now) != "active":
            raise ConflictError(f"{expected_type} 证据已经失效，不能用于新运行")
        return row

    def register_run(self, *, request_id: str, actor_id: str, site_id: str,
                     client_run_key: str, dataset_id: str, parameters_id: str,
                     result_id: str | None = None, note: str = "") -> WriteReceipt:
        client_run_key = str(client_run_key or "").strip()
        if not client_run_key:
            raise ValidationError("client_run_key 不能为空")
        request_payload = {"actor_id": actor_id, "site_id": site_id, "client_run_key": client_run_key,
                           "dataset_id": dataset_id, "parameters_id": parameters_id,
                           "result_id": result_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._check_site(connection, actor, site_id, write=True)
            now = self._now()
            self._run_evidence(connection, actor, dataset_id, site_id, "test_dataset", True, now)
            self._run_evidence(connection, actor, parameters_id, site_id, "run_parameters", True, now)
            if result_id:
                self._run_evidence(connection, actor, result_id, site_id, "result_summary", True, now)

            def create() -> tuple[str, str, dict[str, Any], bool]:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE site_id=? AND client_run_key=?",
                    (site_id, client_run_key),
                ).fetchone()
                if existing is not None:
                    same = (existing["dataset_id"] == dataset_id
                            and existing["parameters_id"] == parameters_id
                            and (existing["result_id"] or None) == (result_id or None)
                            and existing["note"] == note)
                    if not same:
                        raise ConflictError("client_run_key 已被不同运行内容使用")
                    return "run", existing["run_id"], {"run_id": existing["run_id"]}, True
                run_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO runs(run_id,site_id,client_run_key,dataset_id,parameters_id,"
                    "result_id,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, site_id, client_run_key, dataset_id, parameters_id,
                     result_id or None, note, actor_id, now),
                )
                self._connect_run_evidence(connection, dataset_id, parameters_id, result_id, now)
                append_event(connection, actor_id=actor_id, action="run.registered",
                             resource_type="run", resource_id=run_id,
                             detail={"site_id": site_id, "client_run_key": client_run_key,
                                     "dataset_id": dataset_id, "parameters_id": parameters_id,
                                     "result_id": result_id}, occurred_at=now)
                return "run", run_id, {"run_id": run_id}, False

            return self._idempotent(connection, request_id=request_id,
                                    action="register_run", payload=request_payload, create=create)

    @staticmethod
    def _connect_run_evidence(connection, dataset_id: str, parameters_id: str,
                              result_id: str | None, now: str) -> None:
        # 数据集与参数共同构成本次运行的输入。
        connection.execute(
            "INSERT OR IGNORE INTO evidence_links(source_id,target_id,relation,created_at) "
            "VALUES(?,?,?,?)",
            (dataset_id, parameters_id, "run_inputs", now),
        )
        if result_id:
            for target_id, relation in ((dataset_id, "derived_from"),
                                        (parameters_id, "configured_by")):
                connection.execute(
                    "INSERT OR IGNORE INTO evidence_links(source_id,target_id,relation,created_at) "
                    "VALUES(?,?,?,?)",
                    (result_id, target_id, relation, now),
                )

    def attach_run_result(self, *, request_id: str, actor_id: str,
                          run_id: str, result_id: str) -> WriteReceipt:
        request_payload = {"actor_id": actor_id, "run_id": run_id, "result_id": result_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            run_row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run_row is None:
                raise NotFoundError("运行记录不存在")
            self._check_site(connection, actor, run_row["site_id"], write=True)
            now = self._now()
            self._run_evidence(connection, actor, result_id, run_row["site_id"],
                               "result_summary", True, now)

            def create() -> tuple[str, str, dict[str, Any], bool]:
                existing = connection.execute(
                    "SELECT result_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if existing["result_id"]:
                    if existing["result_id"] != result_id:
                        raise ConflictError("运行记录已经绑定其他结果证据")
                    return "run", run_id, {"run_id": run_id}, True
                connection.execute("UPDATE runs SET result_id=? WHERE run_id=?", (result_id, run_id))
                self._connect_run_evidence(connection, run_row["dataset_id"],
                                           run_row["parameters_id"], result_id, now)
                append_event(connection, actor_id=actor_id, action="run.result_attached",
                             resource_type="run", resource_id=run_id,
                             detail={"result_id": result_id}, occurred_at=now)
                return "run", run_id, {"run_id": run_id}, False

            return self._idempotent(connection, request_id=request_id,
                                    action="attach_run_result", payload=request_payload, create=create)

    def get_run(self, *, actor_id: str, run_id: str) -> RunRecord:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError("运行记录不存在")
            self._check_site(connection, actor, row["site_id"])
            return self._run_record(row)

    def list_runs(self, *, actor_id: str, site_id: str) -> list[RunRecord]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._check_site(connection, actor, site_id)
            rows = connection.execute(
                "SELECT * FROM runs WHERE site_id=? ORDER BY created_at, run_id", (site_id,))
            return [self._run_record(row) for row in rows]

    # ------------------------------------------------------------------ 结论

    def _normalize_basis(self, connection, site_id: str, basis: list[dict[str, Any]], now: str):
        if not isinstance(basis, list) or not basis:
            raise ValidationError("basis 必须是非空数组")
        normalized = []
        seen = set()
        for item in basis:
            if not isinstance(item, dict) or "evidence_id" not in item:
                raise ValidationError("basis 条目必须包含 evidence_id")
            evidence_id = str(item["evidence_id"]).strip()
            basis_role = str(item.get("basis_role", "supporting")).strip()
            if basis_role not in BASIS_ROLES:
                raise ValidationError("basis_role 不在允许范围内")
            if evidence_id in seen:
                raise ValidationError("同一证据不能在依据中重复出现")
            seen.add(evidence_id)
            evidence_row = self._load_evidence_row(connection, evidence_id)
            if evidence_row["site_id"] != site_id:
                raise ValidationError("依据证据不属于该场所")
            normalized.append((evidence_id, basis_role, evidence_row))
        return normalized

    def create_conclusion(self, *, request_id: str, actor_id: str, site_id: str,
                          conclusion_key: str, title: str, content: dict[str, Any],
                          basis: list[dict[str, Any]]) -> WriteReceipt:
        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        request_payload = {"actor_id": actor_id, "site_id": site_id, "conclusion_key": conclusion_key,
                           "title": title, "content": content, "basis": basis}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._check_site(connection, actor, site_id, write=True)
            conclusion_key = self._identifier(conclusion_key, "conclusion_key")
            title = self._text(title, "title")
            now = self._now()
            normalized = self._normalize_basis(connection, site_id, basis, now)

            def create() -> tuple[str, str, dict[str, Any]]:
                exists = connection.execute(
                    "SELECT 1 FROM conclusions WHERE site_id=? AND conclusion_key=?",
                    (site_id, conclusion_key),
                ).fetchone()
                if exists:
                    raise ConflictError("结论键已存在，请使用 revise 开新版本")
                conclusion_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO conclusions(conclusion_id,site_id,conclusion_key,version,title,"
                    "content_json,status,supersedes,published_at,invalidated_at,invalidation_json,"
                    "created_by,created_at) VALUES(?,?,?,1,?,?,'draft',NULL,NULL,NULL,'',?,?)",
                    (conclusion_id, site_id, conclusion_key, title,
                     canonical_json(content), actor_id, now),
                )
                for evidence_id, basis_role, evidence_row in normalized:
                    connection.execute(
                        "INSERT INTO conclusion_basis(conclusion_id,evidence_id,basis_role,snapshot_hash) "
                        "VALUES(?,?,?,?)",
                        (conclusion_id, evidence_id, basis_role, evidence_row["payload_hash"]),
                    )
                append_event(connection, actor_id=actor_id, action="conclusion.created",
                             resource_type="conclusion", resource_id=conclusion_id,
                             detail={"site_id": site_id, "conclusion_key": conclusion_key,
                                     "basis": [{"evidence_id": eid, "basis_role": role}
                                               for eid, role, _ in normalized]},
                             occurred_at=now)
                return "conclusion", conclusion_id, {"conclusion_id": conclusion_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_conclusion", payload=request_payload, create=create)

    def revise_conclusion(self, *, request_id: str, actor_id: str, site_id: str,
                          conclusion_key: str, title: str, content: dict[str, Any],
                          basis: list[dict[str, Any]]) -> WriteReceipt:
        if not isinstance(content, dict) or not content:
            raise ValidationError("content 必须是非空对象")
        request_payload = {"actor_id": actor_id, "site_id": site_id, "conclusion_key": conclusion_key,
                           "title": title, "content": content, "basis": basis}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._check_site(connection, actor, site_id, write=True)
            conclusion_key = self._identifier(conclusion_key, "conclusion_key")
            title = self._text(title, "title")
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT * FROM conclusions WHERE site_id=? AND conclusion_key=? "
                    "ORDER BY version DESC LIMIT 1",
                    (site_id, conclusion_key),
                ).fetchone()
                if latest is None:
                    raise NotFoundError("结论不存在")
                if latest["status"] == "published":
                    raise ConflictError("已发布结论不可修订，请以新结论键出具新版报告")
                normalized = self._normalize_basis(connection, site_id, basis, now)
                conclusion_id = uuid.uuid4().hex
                version = latest["version"] + 1
                # 只有普通草稿会被标记为 superseded；已失效草稿保留 invalidated 终态作为历史。
                if latest["status"] == "draft":
                    connection.execute(
                        "UPDATE conclusions SET status='superseded' WHERE conclusion_id=?",
                        (latest["conclusion_id"],),
                    )
                connection.execute(
                    "INSERT INTO conclusions(conclusion_id,site_id,conclusion_key,version,title,"
                    "content_json,status,supersedes,published_at,invalidated_at,invalidation_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,'draft',?,NULL,NULL,'',?,?)",
                    (conclusion_id, site_id, conclusion_key, version, title,
                     canonical_json(content), latest["conclusion_id"], actor_id, now),
                )
                for evidence_id, basis_role, evidence_row in normalized:
                    connection.execute(
                        "INSERT INTO conclusion_basis(conclusion_id,evidence_id,basis_role,snapshot_hash) "
                        "VALUES(?,?,?,?)",
                        (conclusion_id, evidence_id, basis_role, evidence_row["payload_hash"]),
                    )
                append_event(connection, actor_id=actor_id, action="conclusion.revised",
                             resource_type="conclusion", resource_id=conclusion_id,
                             detail={"site_id": site_id, "conclusion_key": conclusion_key,
                                     "version": version, "supersedes": latest["conclusion_id"]},
                             occurred_at=now)
                return "conclusion", conclusion_id, {
                    "conclusion_id": conclusion_id, "version": version,
                    "supersedes": latest["conclusion_id"],
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="revise_conclusion", payload=request_payload, create=create)

    def publish_conclusion(self, *, request_id: str, actor_id: str,
                           conclusion_id: str) -> WriteReceipt:
        request_payload = {"actor_id": actor_id, "conclusion_id": conclusion_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            row = connection.execute(
                "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)).fetchone()
            if row is None:
                raise NotFoundError("结论不存在")
            self._check_site(connection, actor, row["site_id"], write=True)
            now = self._now()

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = connection.execute(
                    "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)).fetchone()
                if latest["status"] == "published":
                    return "conclusion", conclusion_id, {
                        "conclusion_id": conclusion_id, "version": latest["version"],
                        "published_at": latest["published_at"],
                    }
                if latest["status"] != "draft":
                    raise ConflictError(f"结论状态为 {latest['status']}，不能定稿")
                basis_rows = connection.execute(
                    "SELECT evidence_id FROM conclusion_basis WHERE conclusion_id=?", (conclusion_id,))
                stale = []
                for item in basis_rows:
                    evidence_row = self._load_evidence_row(connection, item["evidence_id"])
                    if self._effective_status(evidence_row, now) != "active":
                        stale.append(item["evidence_id"])
                if stale:
                    raise ConflictError("依据中存在失效证据，不能定稿，请先修订依据")
                connection.execute(
                    "UPDATE conclusions SET status='published', published_at=? WHERE conclusion_id=?",
                    (now, conclusion_id),
                )
                append_event(connection, actor_id=actor_id, action="conclusion.published",
                             resource_type="conclusion", resource_id=conclusion_id,
                             detail={"conclusion_key": latest["conclusion_key"],
                                     "version": latest["version"]}, occurred_at=now)
                return "conclusion", conclusion_id, {
                    "conclusion_id": conclusion_id, "version": latest["version"],
                    "published_at": now,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_conclusion", payload=request_payload, create=create)

    def get_conclusion(self, *, actor_id: str, conclusion_id: str) -> ConclusionRecord:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)).fetchone()
            if row is None:
                raise NotFoundError("结论不存在")
            self._check_site(connection, actor, row["site_id"])
            return self._view_conclusion(connection, row, actor)

    def list_conclusions(self, *, actor_id: str, site_id: str,
                         conclusion_key: str | None = None) -> list[ConclusionRecord]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._check_site(connection, actor, site_id)
            if conclusion_key:
                rows = connection.execute(
                    "SELECT * FROM conclusions WHERE site_id=? AND conclusion_key=? ORDER BY version",
                    (site_id, conclusion_key),
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM conclusions WHERE site_id=? ORDER BY conclusion_key, version",
                    (site_id,),
                )
            return [self._view_conclusion(connection, row, actor) for row in rows]

    def affected_runs(self, *, actor_id: str, conclusion_id: str) -> dict[str, Any]:
        """按结论反查全部受影响运行：沿依据证据经运行图反向扩散。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            conclusion_row = connection.execute(
                "SELECT * FROM conclusions WHERE conclusion_id=?", (conclusion_id,)).fetchone()
            if conclusion_row is None:
                raise NotFoundError("结论不存在")
            self._check_site(connection, actor, conclusion_row["site_id"])
            basis_ids = [row["evidence_id"] for row in connection.execute(
                "SELECT evidence_id FROM conclusion_basis WHERE conclusion_id=?", (conclusion_id,))]
            evidence_ids, runs = self._closure(connection, basis_ids, conclusion_row["site_id"])
            evidence_views = [
                self._view_evidence(connection, self._load_evidence_row(connection, eid), actor)
                for eid in sorted(evidence_ids)
            ]
            run_items = sorted((self._run_record(row) for row in runs.values()),
                               key=lambda item: (item.created_at, item.run_id))
            return {
                "conclusion_id": conclusion_id,
                "runs": run_items,
                "evidence": evidence_views,
            }

    def list_impact_statements(self, *, actor_id: str, conclusion_id: str | None = None,
                               evidence_id: str | None = None, run_id: str | None = None) -> list[ImpactStatement]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if conclusion_id:
            clauses.append("conclusion_id=?")
            parameters.append(conclusion_id)
        if evidence_id:
            clauses.append("trigger_evidence_id=?")
            parameters.append(evidence_id)
        if run_id:
            clauses.append("run_id=?")
            parameters.append(run_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            rows = connection.execute(
                f"SELECT * FROM impact_statements{where} ORDER BY created_at, statement_id",
                parameters,
            ).fetchall()
            # 仅返回该角色可访问场所的说明。
            allowed = []
            for row in rows:
                site = connection.execute(
                    "SELECT organization_id FROM sites WHERE site_id=?", (row["site_id"],)).fetchone()
                if site and (actor.role == "admin" or actor.organization_id == site["organization_id"]):
                    allowed.append(self._statement(row))
            return allowed
