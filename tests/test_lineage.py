import unittest
from datetime import datetime, timezone

from ai_governance_foundation.errors import ConflictError, PermissionDenied, ValidationError
from ai_governance_foundation.lineage import LineageService
from ai_governance_foundation.storage import Database


class MutableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


class LineageTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.database = Database()
        self.service = LineageService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="机构一")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev", actor_id="a1", new_actor_id="rv1",
                                    display_name="审查员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="节点", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _dataset(self, request_id="ds1", key="dataset_x", checksum="abc",
                 supersedes=None, expires_at=None, **extra):
        payload = {"name": "数据集X", "record_count": 100, "checksum": checksum,
                   "storage_location": "s3://secret/bucket"}
        payload.update(extra)
        kwargs = {"request_id": request_id, "actor_id": "op1", "site_id": "s1",
                  "evidence_key": key, "evidence_type": "test_dataset", "payload": payload}
        if supersedes is not None:
            kwargs["supersedes"] = supersedes
        if expires_at is not None:
            kwargs["expires_at"] = expires_at
        return self.service.import_evidence(**kwargs)

    def _params(self, request_id="p1"):
        return self.service.import_evidence(
            request_id=request_id, actor_id="op1", site_id="s1", evidence_key="params_a",
            evidence_type="run_parameters",
            payload={"name": "参数A", "model": "m1", "threshold": 0.5, "endpoint": "http://internal"})

    def _result(self, request_id="res1"):
        return self.service.import_evidence(
            request_id=request_id, actor_id="op1", site_id="s1", evidence_key="result_1",
            evidence_type="result_summary",
            payload={"name": "结果1", "metrics": {"f1": 0.9}, "passed": True,
                     "raw_artifact": "s3://art/1"})

    def _judgment(self):
        return self.service.import_evidence(
            request_id="j1", actor_id="rv1", site_id="s1", evidence_key="judge_1",
            evidence_type="manual_judgment",
            payload={"name": "判定1", "decision": "pass", "judge": "rv1", "rationale": "达标"})

    def _basis(self, ds, params=None, result=None, judgment=None):
        basis = [{"evidence_id": ds.resource_id, "basis_role": "dataset"}]
        if params:
            basis.append({"evidence_id": params.resource_id, "basis_role": "parameters"})
        if result:
            basis.append({"evidence_id": result.resource_id, "basis_role": "result"})
        if judgment:
            basis.append({"evidence_id": judgment.resource_id, "basis_role": "manual_judgment"})
        return basis


class EvidenceTest(LineageTestCase):
    def test_duplicate_import_is_naturally_idempotent(self):
        first = self._dataset(request_id="r1")
        again = self._dataset(request_id="r2")
        replay = self._dataset(request_id="r1")
        self.assertEqual(first.resource_id, again.resource_id)
        self.assertFalse(again.replayed)
        self.assertTrue(replay.replayed)
        items = self.service.list_evidence(actor_id="op1", site_id="s1", evidence_key="dataset_x")
        self.assertEqual(1, len(items))

    def test_new_payload_creates_next_version_and_expires_old(self):
        v1 = self._dataset(request_id="r1")
        v2 = self._dataset(request_id="r2", checksum="def")
        self.assertNotEqual(v1.resource_id, v2.resource_id)
        old = self.service.get_evidence(actor_id="op1", evidence_id=v1.resource_id)
        new = self.service.get_evidence(actor_id="op1", evidence_id=v2.resource_id)
        self.assertEqual("expired", old.status)
        self.assertEqual("active", new.status)
        self.assertEqual(v1.resource_id, new.supersedes)
        self.assertEqual(v2.resource_id, old.replaced_by)

    def test_supersedes_must_point_at_latest(self):
        v1 = self._dataset(request_id="r1")
        self._dataset(request_id="r2", checksum="def")
        with self.assertRaises(ConflictError):
            self._dataset(request_id="r3", checksum="ghi", supersedes=v1.resource_id)

    def test_same_key_cannot_change_type(self):
        self._dataset(request_id="r1")
        with self.assertRaises(ConflictError):
            self.service.import_evidence(
                request_id="r-bad", actor_id="op1", site_id="s1", evidence_key="dataset_x",
                evidence_type="run_parameters", payload={"name": "x"})

    def test_reviewer_cannot_import_dataset(self):
        with self.assertRaises(PermissionDenied):
            self.service.import_evidence(
                request_id="r-x", actor_id="rv1", site_id="s1", evidence_key="dd",
                evidence_type="test_dataset", payload={"name": "x"})

    def test_retraction_requires_reason(self):
        ds = self._dataset()
        with self.assertRaises(ValidationError):
            self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                          evidence_id=ds.resource_id, reason=" ")

    def test_expired_evidence_cannot_be_retracted_twice(self):
        ds = self._dataset(request_id="r1")
        self._dataset(request_id="r2", checksum="def")
        with self.assertRaises(ConflictError):
            self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                          evidence_id=ds.resource_id, reason="复核不通过")

    def test_expires_at_sweep(self):
        self.clock.value = datetime(2026, 10, 1, tzinfo=timezone.utc)
        ds = self.service.import_evidence(
            request_id="ds-exp", actor_id="op1", site_id="s1", evidence_key="dy",
            evidence_type="test_dataset", payload={"name": "Y"},
            expires_at="2026-10-02T00:00:00Z")
        self.clock.value = datetime(2026, 10, 3, tzinfo=timezone.utc)
        outcome = self.service.sweep_expired(actor_id="rv1")
        self.assertEqual(1, outcome["count"])
        self.assertEqual("expired", self.service.get_evidence(actor_id="rv1",
                                                               evidence_id=ds.resource_id).status)


class ImpactTest(LineageTestCase):
    def _build_published_and_draft(self):
        ds = self._dataset()
        params = self._params()
        result = self._result()
        judge = self._judgment()
        self.service.register_run(request_id="run1", actor_id="op1", site_id="s1",
                                  client_run_key="run-001", dataset_id=ds.resource_id,
                                  parameters_id=params.resource_id, result_id=result.resource_id)
        basis = self._basis(ds, params, result, judge)
        draft = self.service.create_conclusion(
            request_id="c-draft", actor_id="rv1", site_id="s1", conclusion_key="report_draft",
            title="草稿", content={"verdict": "待定"}, basis=basis)
        published = self.service.create_conclusion(
            request_id="c-pub", actor_id="rv1", site_id="s1", conclusion_key="report_pub",
            title="发布稿", content={"verdict": "通过"}, basis=basis)
        self.service.publish_conclusion(request_id="pub", actor_id="rv1",
                                        conclusion_id=published.resource_id)
        return ds, params, result, judge, draft, published

    def test_retraction_only_invalidates_unfinalized_conclusions(self):
        ds, params, result, judge, draft, published = self._build_published_and_draft()
        self.clock.value = datetime(2026, 10, 5, tzinfo=timezone.utc)
        self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                      evidence_id=ds.resource_id, reason="数据集来源错误")
        draft_view = self.service.get_conclusion(actor_id="rv1", conclusion_id=draft.resource_id)
        pub_view = self.service.get_conclusion(actor_id="rv1", conclusion_id=published.resource_id)
        self.assertEqual("invalidated", draft_view.status)
        self.assertEqual("published", pub_view.status)
        self.assertIsNotNone(draft_view.invalidated_at)
        self.assertEqual(ds.resource_id, draft_view.invalidation["trigger_evidence_id"])
        # 已发布结论保留原始依据快照。
        self.assertEqual(ds.resource_id, pub_view.basis[0].evidence_id)

    def test_impact_statements_cover_three_scopes(self):
        ds, *_ = self._build_published_and_draft()
        self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                      evidence_id=ds.resource_id, reason="x")
        statements = self.service.list_impact_statements(actor_id="rv1")
        scopes = {item.scope for item in statements}
        self.assertEqual({"draft_invalidated", "published_preserved", "run_affected"}, scopes)
        run_statement = next(s for s in statements if s.scope == "run_affected")
        self.assertEqual("run-001", run_statement.detail["client_run_key"])

    def test_published_report_remains_reversible_to_runs_after_retraction(self):
        ds, params, result, judge, draft, published = self._build_published_and_draft()
        affected_before = self.service.affected_runs(actor_id="rv1",
                                                     conclusion_id=published.resource_id)
        self.assertEqual(["run-001"], [r.client_run_key for r in affected_before["runs"]])
        self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                      evidence_id=ds.resource_id, reason="x")
        affected_after = self.service.affected_runs(actor_id="rv1",
                                                    conclusion_id=published.resource_id)
        self.assertEqual(["run-001"], [r.client_run_key for r in affected_after["runs"]])

    def test_run_rejects_retracted_inputs(self):
        ds = self._dataset()
        params = self._params()
        self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                      evidence_id=ds.resource_id, reason="x")
        with self.assertRaises(ConflictError):
            self.service.register_run(request_id="run-bad", actor_id="op1", site_id="s1",
                                      client_run_key="run-x", dataset_id=ds.resource_id,
                                      parameters_id=params.resource_id)

    def test_publish_blocked_when_basis_stale(self):
        ds = self._dataset()
        params = self._params()
        conclusion = self.service.create_conclusion(
            request_id="c1", actor_id="rv1", site_id="s1", conclusion_key="rep",
            title="t", content={"v": 1}, basis=self._basis(ds, params))
        self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                      evidence_id=ds.resource_id, reason="x")
        with self.assertRaises(ConflictError):
            self.service.publish_conclusion(request_id="pub", actor_id="rv1",
                                            conclusion_id=conclusion.resource_id)

    def test_revise_invalidated_draft_with_fresh_basis(self):
        ds1 = self._dataset(request_id="r1")
        params = self._params()
        conclusion = self.service.create_conclusion(
            request_id="c1", actor_id="rv1", site_id="s1", conclusion_key="rep",
            title="t", content={"v": 1}, basis=self._basis(ds1, params))
        self.service.retract_evidence(request_id="ret", actor_id="rv1",
                                      evidence_id=ds1.resource_id, reason="x")
        ds2 = self.service.import_evidence(
            request_id="r2", actor_id="op1", site_id="s1", evidence_key="dataset_z",
            evidence_type="test_dataset", payload={"name": "Z", "checksum": "zz"})
        revised = self.service.revise_conclusion(
            request_id="c2", actor_id="rv1", site_id="s1", conclusion_key="rep",
            title="t2", content={"v": 2}, basis=self._basis(ds2, params))
        self.service.publish_conclusion(request_id="pub2", actor_id="rv1",
                                        conclusion_id=revised.resource_id)
        view = self.service.get_conclusion(actor_id="rv1", conclusion_id=revised.resource_id)
        self.assertEqual("published", view.status)
        self.assertEqual(2, view.version)

    def test_affected_runs_traverses_result_back_to_dataset(self):
        ds = self._dataset()
        params = self._params()
        result = self._result()
        self.service.register_run(request_id="run1", actor_id="op1", site_id="s1",
                                  client_run_key="run-001", dataset_id=ds.resource_id,
                                  parameters_id=params.resource_id, result_id=result.resource_id)
        # 结论只引用结果摘要，仍应反查到产生该结果的运行。
        conclusion = self.service.create_conclusion(
            request_id="c1", actor_id="rv1", site_id="s1", conclusion_key="rep",
            title="t", content={"v": 1}, basis=[{"evidence_id": result.resource_id}])
        affected = self.service.affected_runs(actor_id="rv1", conclusion_id=conclusion.resource_id)
        self.assertEqual(["run-001"], [r.client_run_key for r in affected["runs"]])


class RunTest(LineageTestCase):
    def test_run_idempotent_by_client_key(self):
        ds = self._dataset()
        params = self._params()
        first = self.service.register_run(request_id="run1", actor_id="op1", site_id="s1",
                                          client_run_key="run-001", dataset_id=ds.resource_id,
                                          parameters_id=params.resource_id)
        replay = self.service.register_run(request_id="run1", actor_id="op1", site_id="s1",
                                           client_run_key="run-001", dataset_id=ds.resource_id,
                                           parameters_id=params.resource_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)

    def test_same_client_key_rejects_changed_inputs(self):
        ds1 = self._dataset(request_id="r1")
        ds2 = self._dataset(request_id="r2", key="dataset_z", checksum="zz")
        params = self._params()
        self.service.register_run(request_id="run1", actor_id="op1", site_id="s1",
                                  client_run_key="run-001", dataset_id=ds1.resource_id,
                                  parameters_id=params.resource_id)
        with self.assertRaises(ConflictError):
            self.service.register_run(request_id="run2", actor_id="op1", site_id="s1",
                                      client_run_key="run-001", dataset_id=ds2.resource_id,
                                      parameters_id=params.resource_id)

    def test_attach_result_then_duplicate_attach_replays(self):
        ds = self._dataset()
        params = self._params()
        result = self._result()
        run = self.service.register_run(request_id="run1", actor_id="op1", site_id="s1",
                                        client_run_key="run-001", dataset_id=ds.resource_id,
                                        parameters_id=params.resource_id)
        attached = self.service.attach_run_result(request_id="att", actor_id="op1",
                                                  run_id=run.resource_id, result_id=result.resource_id)
        replay = self.service.attach_run_result(request_id="att", actor_id="op1",
                                                run_id=run.resource_id, result_id=result.resource_id)
        self.assertFalse(attached.replayed)
        self.assertTrue(replay.replayed)


class RedactionTest(LineageTestCase):
    def test_dataset_fields_differ_by_role(self):
        ds = self._dataset()
        reviewer = self.service.get_evidence(actor_id="rv1", evidence_id=ds.resource_id)
        operator = self.service.get_evidence(actor_id="op1", evidence_id=ds.resource_id)
        admin = self.service.get_evidence(actor_id="a1", evidence_id=ds.resource_id)
        self.assertNotIn("storage_location", reviewer.payload)
        self.assertTrue(reviewer.payload_redacted)
        self.assertIn("storage_location", operator.payload)
        self.assertNotIn("storage_location", reviewer.payload)
        self.assertIn("storage_location", admin.payload)
        self.assertFalse(admin.payload_redacted)

    def test_conclusion_internal_fields_hidden_from_reviewer(self):
        ds = self._dataset()
        conclusion = self.service.create_conclusion(
            request_id="c1", actor_id="rv1", site_id="s1", conclusion_key="rep", title="t",
            content={"verdict": "通过", "internal_note": "敏感复核记录"},
            basis=[{"evidence_id": ds.resource_id}])
        reviewer = self.service.get_conclusion(actor_id="rv1", conclusion_id=conclusion.resource_id)
        admin = self.service.get_conclusion(actor_id="a1", conclusion_id=conclusion.resource_id)
        self.assertNotIn("internal_note", reviewer.content)
        self.assertTrue(reviewer.content_redacted)
        self.assertEqual("敏感复核记录", admin.content["internal_note"])

    def test_manual_judgment_rationale_visible_to_reviewer_not_operator(self):
        judge = self._judgment()
        reviewer = self.service.get_evidence(actor_id="rv1", evidence_id=judge.resource_id)
        operator = self.service.get_evidence(actor_id="op1", evidence_id=judge.resource_id)
        self.assertIn("rationale", reviewer.payload)
        self.assertNotIn("rationale", operator.payload)


class CrossOrganizationTest(LineageTestCase):
    def _second_org(self):
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="机构二")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="二机构操作员", role="operator",
                                    organization_id="o2")
        self.service.register_site(request_id="site2", actor_id="op2", site_id="s2",
                                   organization_id="o2", name="节点二",
                                   timezone_name="Asia/Shanghai")

    def test_operator_cannot_touch_other_org_site(self):
        self._second_org()
        with self.assertRaises(PermissionDenied):
            self.service.import_evidence(
                request_id="x1", actor_id="op1", site_id="s2", evidence_key="dd",
                evidence_type="test_dataset", payload={"name": "x"})

    def test_auditor_can_read_other_org_but_not_write(self):
        self._second_org()
        receipt = self.service.import_evidence(
            request_id="x2", actor_id="op2", site_id="s2", evidence_key="dd",
            evidence_type="test_dataset", payload={"name": "x", "storage_location": "s"})
        view = self.service.get_evidence(actor_id="au1", evidence_id=receipt.resource_id)
        self.assertEqual("x", view.payload["name"])
        with self.assertRaises(PermissionDenied):
            self.service.retract_evidence(request_id="r2", actor_id="au1",
                                          evidence_id=receipt.resource_id, reason="审计不可撤回")


if __name__ == "__main__":
    unittest.main()
