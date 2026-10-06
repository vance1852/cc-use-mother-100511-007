import unittest

from ai_governance_foundation.api import route
from ai_governance_foundation.lineage import LineageService
from ai_governance_foundation.storage import Database


class LineageApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = LineageService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "adm", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        for rid, actor_id, name, role in (
                ("op", "op1", "操作员", "operator"),
                ("rv", "rv1", "审查员", "reviewer")):
            route(self.service, "POST", "/actors",
                  {"request_id": rid, "new_actor_id": actor_id, "display_name": name,
                   "role": role, "organization_id": "o1"},
                  {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "节点", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "op1"})

    def tearDown(self):
        self.database.close()

    def _import(self, actor, rid, key, etype, payload, headers_extra=None):
        return route(self.service, "POST", "/evidence",
                     {"request_id": rid, "site_id": "s1", "evidence_key": key,
                      "evidence_type": etype, "payload": payload},
                      {"X-Actor-Id": actor})

    def _bootstrap_chain(self):
        ds = self._import("op1", "ds1", "dx", "test_dataset",
                          {"name": "X", "record_count": 1, "storage_location": "secret"})
        params = self._import("op1", "p1", "pa", "run_parameters",
                              {"name": "P", "model": "m", "endpoint": "internal"})
        result = self._import("op1", "res1", "r1", "result_summary",
                              {"name": "R", "passed": True, "raw_artifact": "art"})
        return ds[1]["resource_id"], params[1]["resource_id"], result[1]["resource_id"]

    def test_evidence_import_and_role_redaction(self):
        self._bootstrap_chain()
        status, payload = route(self.service, "GET", "/evidence?site_id=s1&evidence_key=dx",
                                None, {"X-Actor-Id": "rv1"})
        self.assertEqual(200, status)
        item = payload["items"][0]
        self.assertNotIn("storage_location", item["payload"])
        self.assertTrue(item["payload_redacted"])
        status, payload = route(self.service, "GET", "/evidence?site_id=s1&evidence_key=dx",
                                None, {"X-Actor-Id": "a1"})
        self.assertIn("storage_location", payload["items"][0]["payload"])

    def test_run_conclusion_publish_affected_runs_flow(self):
        ds, params, result = self._bootstrap_chain()
        status, payload = route(self.service, "POST", "/runs",
                                {"request_id": "run1", "site_id": "s1", "client_run_key": "run-001",
                                 "dataset_id": ds, "parameters_id": params, "result_id": result},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        run_id = payload["resource_id"]
        status, payload = route(self.service, "POST", "/conclusions",
                                {"request_id": "c1", "site_id": "s1", "conclusion_key": "rep",
                                 "title": "报告", "content": {"verdict": "通过"},
                                 "basis": [{"evidence_id": result, "basis_role": "result"}]},
                                {"X-Actor-Id": "rv1"})
        self.assertEqual(201, status)
        conclusion_id = payload["resource_id"]
        status, payload = route(self.service, "POST", f"/conclusions/{conclusion_id}/publish",
                                {}, {"X-Actor-Id": "rv1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET",
                                f"/conclusions/{conclusion_id}/affected-runs",
                                None, {"X-Actor-Id": "rv1"})
        self.assertEqual(200, status)
        self.assertEqual([run_id], [item["run_id"] for item in payload["runs"]])

    def test_retraction_generates_published_preservation_statement(self):
        ds, params, result = self._bootstrap_chain()
        route(self.service, "POST", "/runs",
              {"request_id": "run1", "site_id": "s1", "client_run_key": "run-001",
               "dataset_id": ds, "parameters_id": params, "result_id": result},
              {"X-Actor-Id": "op1"})
        _, created = route(self.service, "POST", "/conclusions",
                           {"request_id": "c1", "site_id": "s1", "conclusion_key": "rep",
                            "title": "报告", "content": {"v": 1},
                            "basis": [{"evidence_id": ds, "basis_role": "dataset"}]},
                           {"X-Actor-Id": "rv1"})
        cid = created["resource_id"]
        route(self.service, "POST", f"/conclusions/{cid}/publish", {}, {"X-Actor-Id": "rv1"})
        status, payload = route(self.service, "POST", f"/evidence/{ds}/retract",
                                {"request_id": "ret", "reason": "数据集错误"},
                                {"X-Actor-Id": "rv1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET",
                                f"/impact-statements?conclusion_id={cid}",
                                None, {"X-Actor-Id": "rv1"})
        scopes = {item["scope"] for item in payload["items"]}
        self.assertIn("published_preserved", scopes)
        # 发布稿仍可读取，状态未变。
        status, payload = route(self.service, "GET", f"/conclusions/{cid}",
                                None, {"X-Actor-Id": "rv1"})
        self.assertEqual("published", payload["status"])
        self.assertEqual(ds, payload["basis"][0]["evidence_id"])

    def test_evidence_list_requires_site(self):
        status, payload = route(self.service, "GET", "/evidence", None, {"X-Actor-Id": "rv1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_reviewer_cannot_register_run(self):
        ds, params, _ = self._bootstrap_chain()
        status, payload = route(self.service, "POST", "/runs",
                                {"request_id": "runx", "site_id": "s1", "client_run_key": "rx",
                                 "dataset_id": ds, "parameters_id": params},
                                {"X-Actor-Id": "rv1"})
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
