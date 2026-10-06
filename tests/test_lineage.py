import unittest
from datetime import datetime, timezone

from ai_governance_foundation.clock import FixedClock
from ai_governance_foundation.errors import ConflictError, PermissionDenied, ValidationError
from ai_governance_foundation.lineage import EvidenceLineageService
from ai_governance_foundation.storage import Database


class LineageTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = EvidenceLineageService(
            self.database, FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="科研机构一")
        self.service.register_actor(request_id="a1", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="a2", actor_id="admin1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="a3", actor_id="admin1", new_actor_id="rev1",
                                    display_name="审查员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="a4", actor_id="admin1", new_actor_id="aud1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_organization(request_id="org2", actor_id="admin1",
                                           organization_id="o2", name="科研机构二")
        self.service.register_actor(request_id="a5", actor_id="admin1", new_actor_id="rev2",
                                    display_name="外组织审查员", role="reviewer", organization_id="o2")
        self.service.register_dataset(request_id="ds", actor_id="op1", dataset_id="ds1",
                                      organization_id="o1", name="安全评估数据集")

    def tearDown(self):
        self.database.close()

    def _version(self, request_id, content, metadata=None):
        receipt = self.service.register_dataset_version(
            request_id=request_id, actor_id="op1", dataset_id="ds1",
            content=content, metadata=metadata or {})
        return int(receipt.resource_id.split(":")[1])

    def _run(self, request_id, external_key, version=1, parameters=None,
             result=None, notes="内部备注"):
        receipt = self.service.import_run(
            request_id=request_id, actor_id="op1",
            external_key=external_key, dataset_id="ds1", dataset_version=version,
            parameters=parameters or {"model": "m1"},
            result_summary=result or {"verdict": "pass"},
            internal_notes=notes)
        return receipt.resource_id

    def _judgment(self, request_id, external_key, run_id, verdict="pass"):
        receipt = self.service.record_judgment(
            request_id=request_id, actor_id="rev1", external_key=external_key,
            run_id=run_id, verdict=verdict, rationale="人工复核结论")
        return receipt.resource_id

    def _conclusion(self, request_id, evidence, title="评估结论"):
        receipt = self.service.create_conclusion(
            request_id=request_id, actor_id="rev1", title=title,
            statement="基于引用证据得出的结论", evidence=evidence)
        return receipt.resource_id

    # ---------- 版本链 ----------

    def test_dataset_versions_form_traceable_chain(self):
        self.assertEqual(1, self._version("v1", {"samples": ["a"]}))
        self.assertEqual(2, self._version("v2", {"samples": ["b"]}))
        versions = self.service.list_dataset_versions("op1", "ds1")
        self.assertEqual([1, 2], [item["version"] for item in versions])
        self.assertEqual("superseded", versions[0]["status"])
        self.assertEqual("active", versions[1]["status"])
        self.assertIsNone(versions[0]["supersedes_version"])
        self.assertEqual(1, versions[1]["supersedes_version"])

    def test_duplicate_dataset_version_import_is_idempotent(self):
        first = self.service.register_dataset_version(
            request_id="v1", actor_id="op1", dataset_id="ds1",
            content={"samples": ["a"]}, metadata={"batch": 1})
        again = self.service.register_dataset_version(
            request_id="v1-retry", actor_id="op1", dataset_id="ds1",
            content={"samples": ["a"]}, metadata={"batch": 1})
        self.assertEqual(first.resource_id, again.resource_id)
        self.assertFalse(again.replayed)
        self.assertEqual(1, len(self.service.list_dataset_versions("op1", "ds1")))

    def test_same_request_replays_version_registration(self):
        first = self.service.register_dataset_version(
            request_id="v1", actor_id="op1", dataset_id="ds1", content={"samples": ["a"]})
        second = self.service.register_dataset_version(
            request_id="v1", actor_id="op1", dataset_id="ds1", content={"samples": ["a"]})
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, len(self.service.list_dataset_versions("op1", "ds1")))

    # ---------- 幂等导入 ----------

    def test_duplicate_run_import_is_idempotent(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        again = self._run("r2", "key-1")
        self.assertEqual(run_id, again)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM run_records").fetchone()["count"]
        self.assertEqual(1, count)

    def test_run_import_conflicts_on_same_key_with_different_content(self):
        self._version("v1", {"samples": ["a"]})
        self._run("r1", "key-1")
        with self.assertRaises(ConflictError):
            self._run("r2", "key-1", parameters={"model": "m2"})

    def test_duplicate_judgment_import_is_idempotent(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        judgment_id = self._judgment("j1", "jud-1", run_id)
        again = self._judgment("j2", "jud-1", run_id)
        self.assertEqual(judgment_id, again)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM judgments").fetchone()["count"]
        self.assertEqual(1, count)

    # ---------- 撤回 / 过期的影响传播 ----------

    def test_retracted_run_blocks_draft_publish_until_revised(self):
        self._version("v1", {"samples": ["a"]})
        run_a = self._run("ra", "key-a")
        run_b = self._run("rb", "key-b")
        draft = self._conclusion("c1", [{"type": "run", "id": run_a}])
        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_a,
                                    status="retracted", reason="数据污染")
        with self.assertRaises(ConflictError):
            self.service.publish_conclusion(request_id="pub", actor_id="admin1",
                                            conclusion_id=draft)
        view = self.service.get_conclusion("rev1", draft)
        self.assertEqual("draft", view["status"])
        self.assertEqual(
            [{"evidence_type": "run", "evidence_id": run_a,
              "status": "retracted", "reason": "数据污染"}],
            view["affecting_evidence"])
        self.service.revise_conclusion(request_id="rev", actor_id="rev1", conclusion_id=draft,
                                       evidence=[{"type": "run", "id": run_b}])
        self.service.publish_conclusion(request_id="pub2", actor_id="admin1",
                                        conclusion_id=draft)
        view = self.service.get_conclusion("rev1", draft)
        self.assertEqual("published", view["status"])
        self.assertEqual(2, view["revision"])

    def test_published_conclusion_keeps_basis_and_records_impact(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        judgment_id = self._judgment("j1", "jud-1", run_id)
        conclusion_id = self._conclusion(
            "c1", [{"type": "run", "id": run_id}, {"type": "judgment", "id": judgment_id}])
        self.service.publish_conclusion(request_id="pub", actor_id="admin1",
                                        conclusion_id=conclusion_id)
        basis_before = self.service.get_conclusion("rev1", conclusion_id)["basis"]
        self.assertEqual(2, len(basis_before))

        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                    status="retracted", reason="样本污染")
        view = self.service.get_conclusion("rev1", conclusion_id)
        self.assertEqual("published", view["status"])
        self.assertEqual(basis_before, view["basis"])
        self.assertEqual([], view["affecting_evidence"])
        self.assertEqual(1, len(view["impact_statements"]))
        impact = view["impact_statements"][0]
        self.assertEqual("run", impact["evidence_type"])
        self.assertEqual(run_id, impact["evidence_id"])
        self.assertEqual("retracted", impact["event"])
        self.assertIn("样本污染", impact["summary"])

        # 相同状态与原因的重复撤回保持幂等，不重复生成影响说明
        self.service.set_run_status(request_id="ret2", actor_id="admin1", run_id=run_id,
                                    status="retracted", reason="样本污染")
        view = self.service.get_conclusion("rev1", conclusion_id)
        self.assertEqual(1, len(view["impact_statements"]))
        with self.assertRaises(ConflictError):
            self.service.set_run_status(request_id="ret3", actor_id="admin1", run_id=run_id,
                                        status="retracted", reason="另一个原因")

    def test_expired_dataset_version_only_generates_impact_for_published(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        published = self._conclusion("c1", [{"type": "run", "id": run_id}], title="已发布结论")
        self.service.publish_conclusion(request_id="pub", actor_id="admin1",
                                        conclusion_id=published)
        draft = self._conclusion("c2", [{"type": "run", "id": run_id}], title="未定稿结论")

        self.service.set_dataset_version_status(request_id="exp", actor_id="admin1",
                                                dataset_id="ds1", version=1,
                                                status="expired", reason="超过保留期")
        published_view = self.service.get_conclusion("rev1", published)
        self.assertEqual("published", published_view["status"])
        self.assertEqual(1, len(published_view["impact_statements"]))
        impact = published_view["impact_statements"][0]
        self.assertEqual("dataset_version", impact["evidence_type"])
        self.assertEqual("ds1:1", impact["evidence_id"])
        self.assertEqual("expired", impact["event"])
        self.assertIn("超过保留期", impact["summary"])

        draft_view = self.service.get_conclusion("rev1", draft)
        self.assertEqual("draft", draft_view["status"])
        self.assertEqual([], draft_view["impact_statements"])
        self.assertIn({"evidence_type": "dataset_version", "evidence_id": "ds1:1",
                       "status": "expired", "reason": "超过保留期"},
                      draft_view["affecting_evidence"])
        with self.assertRaises(ConflictError):
            self.service.publish_conclusion(request_id="pub2", actor_id="admin1",
                                            conclusion_id=draft)

    def test_retract_is_idempotent_per_request(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        first = self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                            status="retracted", reason="数据污染")
        second = self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                             status="retracted", reason="数据污染")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)

    def test_terminal_status_cannot_change_again(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                    status="retracted", reason="数据污染")
        with self.assertRaises(ConflictError):
            self.service.set_run_status(request_id="exp", actor_id="admin1", run_id=run_id,
                                        status="expired", reason="超过保留期")

    # ---------- 分级字段视图 ----------

    def test_field_scope_differs_by_role(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        operator_view = self.service.get_run("op1", run_id)
        self.assertIn("parameters", operator_view)
        self.assertIn("internal_notes", operator_view)

        reviewer_view = self.service.get_run("rev1", run_id)
        self.assertIn("parameters", reviewer_view)
        self.assertNotIn("internal_notes", reviewer_view)

        auditor_view = self.service.get_run("aud1", run_id)
        self.assertNotIn("parameters", auditor_view)
        self.assertNotIn("internal_notes", auditor_view)
        self.assertNotIn("created_by", auditor_view)
        self.assertIn("parameters_hash", auditor_view)
        self.assertIn("result_summary", auditor_view)

    def test_auditor_sees_redacted_dataset_versions(self):
        self._version("v1", {"samples": ["a"]}, metadata={"purpose": "安全评估"})
        auditor_versions = self.service.list_dataset_versions("aud1", "ds1")
        self.assertNotIn("metadata", auditor_versions[0])
        self.assertIn("content_hash", auditor_versions[0])
        operator_versions = self.service.list_dataset_versions("op1", "ds1")
        self.assertEqual({"purpose": "安全评估"}, operator_versions[0]["metadata"])

    # ---------- 按结论反查运行记录 ----------

    def test_conclusion_runs_reverse_lookup(self):
        self._version("v1", {"samples": ["a"]})
        run_a = self._run("ra", "key-a")
        run_b = self._run("rb", "key-b")
        judgment_id = self._judgment("j1", "jud-1", run_b)
        conclusion_id = self._conclusion(
            "c1", [{"type": "run", "id": run_a}, {"type": "judgment", "id": judgment_id}])

        result = self.service.conclusion_runs("rev1", conclusion_id)
        self.assertEqual("draft", result["conclusion_status"])
        self.assertEqual(2, len(result["items"]))
        by_run = {item["run"]["run_id"]: item for item in result["items"]}
        self.assertEqual(["direct"], by_run[run_a]["linked_via"])
        self.assertEqual([f"judgment:{judgment_id}"], by_run[run_b]["linked_via"])
        self.assertEqual(judgment_id, by_run[run_b]["cited_judgments"][0]["judgment_id"])
        self.assertEqual("active", by_run[run_a]["dataset"]["dataset_version_status"])
        self.assertFalse(by_run[run_a]["affects_conclusion"])

        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_b,
                                    status="retracted", reason="数据污染")
        result = self.service.conclusion_runs("rev1", conclusion_id)
        by_run = {item["run"]["run_id"]: item for item in result["items"]}
        self.assertFalse(by_run[run_a]["affects_conclusion"])
        self.assertTrue(by_run[run_b]["affects_conclusion"])
        affected = self.service.conclusion_runs("rev1", conclusion_id, affected_only=True)
        self.assertEqual([run_b], [item["run"]["run_id"] for item in affected["items"]])

    def test_reverse_lookup_marks_superseded_dataset_version(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        conclusion_id = self._conclusion("c1", [{"type": "run", "id": run_id}])
        self._version("v2", {"samples": ["b"]})
        result = self.service.conclusion_runs("rev1", conclusion_id)
        dataset = result["items"][0]["dataset"]
        self.assertEqual("superseded", dataset["dataset_version_status"])
        self.assertEqual(2, dataset["latest_version"])
        self.assertEqual(2, dataset["superseded_by"])
        self.assertFalse(result["items"][0]["affects_conclusion"])

    # ---------- 权限与边界 ----------

    def test_cross_org_access_is_denied(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        with self.assertRaises(PermissionDenied):
            self.service.get_run("rev2", run_id)
        with self.assertRaises(PermissionDenied):
            self.service.create_conclusion(request_id="c9", actor_id="rev2", title="越权结论",
                                           statement="引用其他组织证据",
                                           evidence=[{"type": "run", "id": run_id}])

    def test_role_permissions_for_lineage_actions(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        with self.assertRaises(PermissionDenied):
            self.service.import_run(request_id="r2", actor_id="aud1", external_key="key-2",
                                    dataset_id="ds1", dataset_version=1,
                                    parameters={"model": "m1"}, result_summary={"verdict": "pass"})
        with self.assertRaises(PermissionDenied):
            self.service.record_judgment(request_id="j9", actor_id="op1", external_key="jud-9",
                                         run_id=run_id, verdict="pass", rationale="越权判定")
        draft = self._conclusion("c1", [{"type": "run", "id": run_id}])
        with self.assertRaises(PermissionDenied):
            self.service.publish_conclusion(request_id="p9", actor_id="rev1", conclusion_id=draft)
        with self.assertRaises(PermissionDenied):
            self.service.set_run_status(request_id="s9", actor_id="op1", run_id=run_id,
                                        status="retracted", reason="越权撤回")

    def test_run_on_retracted_dataset_version_is_rejected(self):
        self._version("v1", {"samples": ["a"]})
        self.service.set_dataset_version_status(request_id="ret", actor_id="admin1",
                                                dataset_id="ds1", version=1,
                                                status="retracted", reason="数据源失效")
        with self.assertRaises(ValidationError):
            self._run("r1", "key-1")

    def test_judgment_on_retracted_run_is_rejected(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                    status="retracted", reason="数据污染")
        with self.assertRaises(ValidationError):
            self._judgment("j1", "jud-1", run_id)

    def test_conclusion_citing_retracted_evidence_is_rejected(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                    status="retracted", reason="数据污染")
        with self.assertRaises(ValidationError):
            self._conclusion("c1", [{"type": "run", "id": run_id}])

    def test_published_conclusion_cannot_be_revised(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        conclusion_id = self._conclusion("c1", [{"type": "run", "id": run_id}])
        self.service.publish_conclusion(request_id="pub", actor_id="admin1",
                                        conclusion_id=conclusion_id)
        with self.assertRaises(ConflictError):
            self.service.revise_conclusion(request_id="rev", actor_id="rev1",
                                           conclusion_id=conclusion_id, statement="新表述")

    def test_audit_chain_covers_lineage_events(self):
        self._version("v1", {"samples": ["a"]})
        run_id = self._run("r1", "key-1")
        conclusion_id = self._conclusion("c1", [{"type": "run", "id": run_id}])
        self.service.publish_conclusion(request_id="pub", actor_id="admin1",
                                        conclusion_id=conclusion_id)
        self.service.set_run_status(request_id="ret", actor_id="admin1", run_id=run_id,
                                    status="retracted", reason="数据污染")
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)
        actions = {event["action"] for event in self.service.audit_events()}
        self.assertIn("dataset_version.registered", actions)
        self.assertIn("run.imported", actions)
        self.assertIn("conclusion.created", actions)
        self.assertIn("conclusion.published", actions)
        self.assertIn("evidence.status_changed", actions)


if __name__ == "__main__":
    unittest.main()
