"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .lineage import EvidenceLineageService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链与证据谱系链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = EvidenceLineageService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科研机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="安全审查员", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-auditor", actor_id="admin-001", new_actor_id="auditor-001",
                               display_name="合规审计员", role="auditor", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号创新节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 证据谱系：数据集版本链 → 运行记录 → 人工判定 → 结论
        service.register_dataset(request_id="req-dataset", actor_id="operator-001",
                                 dataset_id="ds-safety", organization_id="org-001",
                                 name="模型安全评估数据集")
        service.register_dataset_version(request_id="req-dsv1", actor_id="operator-001",
                                         dataset_id="ds-safety",
                                         content={"samples": ["prompt-a", "prompt-b"]},
                                         metadata={"purpose": "模型安全评估"})
        run_receipt = service.import_run(request_id="req-run", actor_id="operator-001",
                                         external_key="run-001", dataset_id="ds-safety",
                                         dataset_version=1,
                                         parameters={"model": "model-x", "temperature": 0},
                                         result_summary={"verdict": "pass", "score": 0.98},
                                         internal_notes="内部复核对数")
        run_id = run_receipt.resource_id
        run_retry = service.import_run(request_id="req-run-retry", actor_id="operator-001",
                                       external_key="run-001", dataset_id="ds-safety",
                                       dataset_version=1,
                                       parameters={"model": "model-x", "temperature": 0},
                                       result_summary={"verdict": "pass", "score": 0.98},
                                       internal_notes="内部复核对数")
        judgment_id = service.record_judgment(request_id="req-judgment", actor_id="reviewer-001",
                                              external_key="jud-001", run_id=run_id,
                                              verdict="pass", rationale="人工复核通过").resource_id
        published_id = service.create_conclusion(
            request_id="req-conclusion", actor_id="reviewer-001",
            title="model-x 安全评估结论",
            statement="model-x 在 ds-safety v1 上通过安全评估",
            evidence=[{"type": "run", "id": run_id},
                      {"type": "judgment", "id": judgment_id}]).resource_id
        service.publish_conclusion(request_id="req-publish", actor_id="admin-001",
                                   conclusion_id=published_id)
        # 新数据集版本替换旧版本，旧版本与结论的关系保留在版本链中
        service.register_dataset_version(request_id="req-dsv2", actor_id="operator-001",
                                         dataset_id="ds-safety",
                                         content={"samples": ["prompt-a", "prompt-c"]},
                                         metadata={"purpose": "模型安全评估"})
        draft_id = service.create_conclusion(
            request_id="req-conclusion-2", actor_id="reviewer-001",
            title="model-x 复评结论",
            statement="复评草稿，引用同一运行记录",
            evidence=[{"type": "run", "id": run_id}]).resource_id
        # 撤回运行记录：已发布结论生成影响说明并保留原始依据；未定稿结论被阻止发布
        service.set_run_status(request_id="req-retract", actor_id="admin-001", run_id=run_id,
                               status="retracted", reason="样本污染")
        draft_publish_blocked = False
        try:
            service.publish_conclusion(request_id="req-publish-2", actor_id="admin-001",
                                       conclusion_id=draft_id)
        except ConflictError:
            draft_publish_blocked = True
        # 修订未定稿结论，改引用基于新版本数据集的运行记录后即可发布
        service.register_dataset_version(request_id="req-dsv2-dup", actor_id="operator-001",
                                         dataset_id="ds-safety",
                                         content={"samples": ["prompt-a", "prompt-c"]},
                                         metadata={"purpose": "模型安全评估"})
        new_run_id = service.import_run(request_id="req-run-2", actor_id="operator-001",
                                        external_key="run-002", dataset_id="ds-safety",
                                        dataset_version=2,
                                        parameters={"model": "model-x", "temperature": 0},
                                        result_summary={"verdict": "pass", "score": 0.97}).resource_id
        service.revise_conclusion(request_id="req-revise", actor_id="reviewer-001",
                                  conclusion_id=draft_id,
                                  evidence=[{"type": "run", "id": new_run_id}])
        service.publish_conclusion(request_id="req-publish-3", actor_id="admin-001",
                                   conclusion_id=draft_id)

        published = service.get_conclusion("reviewer-001", published_id)
        reverse = service.conclusion_runs("reviewer-001", published_id)
        versions = service.list_dataset_versions("operator-001", "ds-safety")
        auditor_view = service.get_run("auditor-001", run_id)
        reviewer_view = service.get_run("reviewer-001", run_id)
        operator_view = service.get_run("operator-001", run_id)
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        lineage = {
            "run_import_idempotent": run_retry.resource_id == run_id,
            "dataset_versions": len(versions),
            "first_version_status": versions[0]["status"],
            "published_kept_basis": len(published["basis"] or []) == 2,
            "impact_statements": len(published["impact_statements"]),
            "draft_publish_blocked": draft_publish_blocked,
            "reverse_lookup_runs": len(reverse["items"]),
            "reverse_lookup_affected": sum(1 for item in reverse["items"] if item["affects_conclusion"]),
            "auditor_limited_fields": ("parameters" not in auditor_view
                                       and "internal_notes" not in auditor_view
                                       and "parameters_hash" in auditor_view),
            "reviewer_hides_internal_notes": ("parameters" in reviewer_view
                                              and "internal_notes" not in reviewer_view),
            "operator_full_fields": "internal_notes" in operator_view,
        }
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "lineage": lineage,
                  "lineage_ok": all(lineage.values())}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] and result["lineage_ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
