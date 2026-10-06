"""运行基础服务与证据谱系的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .lineage import LineageService
from .storage import Database


def _lineage_scenario(service: LineageService) -> dict[str, object]:
    """覆盖证据版本链、定稿保护、影响说明、幂等与按结论反查运行。"""

    service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="rev-001",
                           display_name="合规审查员", role="reviewer", organization_id="org-001")
    dataset_v1 = service.import_evidence(
        request_id="req-dataset-v1", actor_id="operator-001", site_id="site-001",
        evidence_key="red_team_set", evidence_type="test_dataset",
        payload={"name": "红队评测集", "version_source": "2026Q3", "record_count": 2000,
                 "checksum": "sha256:aaaa", "storage_location": "s3://internal/a"})
    dataset_dup = service.import_evidence(
        request_id="req-dataset-dup", actor_id="operator-001", site_id="site-001",
        evidence_key="red_team_set", evidence_type="test_dataset",
        payload={"name": "红队评测集", "version_source": "2026Q3", "record_count": 2000,
                 "checksum": "sha256:aaaa", "storage_location": "s3://internal/a"})
    parameters = service.import_evidence(
        request_id="req-params", actor_id="operator-001", site_id="site-001",
        evidence_key="eval_params", evidence_type="run_parameters",
        payload={"name": "评测参数", "model": "guard-model-7", "threshold": 0.8, "seed": 42})
    result = service.import_evidence(
        request_id="req-result", actor_id="operator-001", site_id="site-001",
        evidence_key="eval_result", evidence_type="result_summary",
        payload={"name": "结果摘要", "metrics": {"attack_success_rate": 0.03}, "passed": True})
    judgment = service.import_evidence(
        request_id="req-judgment", actor_id="rev-001", site_id="site-001",
        evidence_key="judgment-1", evidence_type="manual_judgment",
        payload={"name": "人工复核", "decision": "pass", "judge": "rev-001", "rationale": "未见红线突破"})

    basis = [
        {"evidence_id": dataset_v1.resource_id, "basis_role": "dataset"},
        {"evidence_id": parameters.resource_id, "basis_role": "parameters"},
        {"evidence_id": result.resource_id, "basis_role": "result"},
        {"evidence_id": judgment.resource_id, "basis_role": "manual_judgment"},
    ]
    run = service.register_run(
        request_id="req-run", actor_id="operator-001", site_id="site-001",
        client_run_key="run-2026-09-25-01", dataset_id=dataset_v1.resource_id,
        parameters_id=parameters.resource_id, result_id=result.resource_id, note="季度安全评测")
    draft = service.create_conclusion(
        request_id="req-draft", actor_id="rev-001", site_id="site-001",
        conclusion_key="safety_report", title="模型安全报告（草稿）",
        content={"verdict": "通过", "internal_note": "内部讨论稿"}, basis=basis)
    published = service.create_conclusion(
        request_id="req-publish-draft", actor_id="rev-001", site_id="site-001",
        conclusion_key="safety_report_q3", title="模型安全报告 Q3",
        content={"verdict": "通过"}, basis=basis)
    service.publish_conclusion(request_id="req-publish", actor_id="rev-001",
                               conclusion_id=published.resource_id)

    affected_before = service.affected_runs(actor_id="rev-001",
                                            conclusion_id=published.resource_id)
    # 报告引用的测试结果被新数据集替换：旧数据集版本过期。
    dataset_v2 = service.import_evidence(
        request_id="req-dataset-v2", actor_id="operator-001", site_id="site-001",
        evidence_key="red_team_set", evidence_type="test_dataset",
        payload={"name": "红队评测集", "version_source": "2026Q4", "record_count": 2400,
                 "checksum": "sha256:bbbb", "storage_location": "s3://internal/b"},
        supersedes=dataset_v1.resource_id)

    draft_view = service.get_conclusion(actor_id="rev-001", conclusion_id=draft.resource_id)
    published_view = service.get_conclusion(actor_id="rev-001", conclusion_id=published.resource_id)
    affected_after = service.affected_runs(actor_id="rev-001",
                                           conclusion_id=published.resource_id)
    statements = service.list_impact_statements(actor_id="rev-001")
    scopes = {item.scope for item in statements}
    reviewer_view = service.get_evidence(actor_id="rev-001", evidence_id=dataset_v1.resource_id)
    admin_view = service.get_evidence(actor_id="admin-001", evidence_id=dataset_v1.resource_id)
    valid, _ = service.verify_audit()

    return {
        "lineage_idempotent": dataset_dup.resource_id == dataset_v1.resource_id,
        "lineage_dataset_versioned": dataset_v2.resource_id != dataset_v1.resource_id,
        "lineage_draft_invalidated": draft_view.status == "invalidated",
        "lineage_published_preserved": published_view.status == "published",
        "lineage_published_basis_retained": published_view.basis[0].evidence_id == dataset_v1.resource_id,
        "lineage_affected_runs": [item.client_run_key for item in affected_after["runs"]] == ["run-2026-09-25-01"],
        "lineage_run_preserved_after_change": [item.client_run_key for item in affected_before["runs"]] == ["run-2026-09-25-01"],
        "lineage_run_id": run.resource_id,
        "lineage_scopes": sorted(scopes),
        "lineage_reviewer_redacted": "storage_location" not in reviewer_view.payload and reviewer_view.payload_redacted,
        "lineage_admin_sees_all": "storage_location" in admin_view.payload and not admin_view.payload_redacted,
        "lineage_audit_valid": valid,
    }


def run() -> dict[str, object]:
    """执行一条完整登记链与证据谱系链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = LineageService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科研机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号创新节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        lineage = _lineage_scenario(service)
        final_valid, final_count = service.verify_audit()
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed}
        result.update(lineage)
        result["audit_valid"] = final_valid
        result["audit_events"] = final_count
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    success = result["status"] == "ok" and result["audit_valid"]
    lineage_ok = all(value for key, value in result.items()
                     if key.startswith("lineage_") and key != "lineage_scopes" and isinstance(value, bool))
    return 0 if success and lineage_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
