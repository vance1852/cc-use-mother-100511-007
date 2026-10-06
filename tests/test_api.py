import unittest

from ai_governance_foundation.api import route
from ai_governance_foundation.lineage import EvidenceLineageService
from ai_governance_foundation.storage import Database


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = EvidenceLineageService(self.database)

    def tearDown(self):
        self.database.close()

    def _bootstrap_lineage(self):
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="科研机构一")
        self.service.register_actor(request_id="a1", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="a2", actor_id="admin1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="a3", actor_id="admin1", new_actor_id="rev1",
                                    display_name="审查员", role="reviewer", organization_id="o1")
        self.service.register_dataset(request_id="ds", actor_id="op1", dataset_id="ds1",
                                      organization_id="o1", name="安全评估数据集")
        self.service.register_dataset_version(request_id="v1", actor_id="op1", dataset_id="ds1",
                                              content={"samples": ["a"]})
        run_id = self.service.import_run(request_id="r1", actor_id="op1", external_key="key-1",
                                         dataset_id="ds1", dataset_version=1,
                                         parameters={"model": "m1"},
                                         result_summary={"verdict": "pass"}).resource_id
        conclusion_id = self.service.create_conclusion(
            request_id="c1", actor_id="rev1", title="评估结论", statement="结论内容",
            evidence=[{"type": "run", "id": run_id}]).resource_id
        return run_id, conclusion_id

    def test_health_is_available_without_actor(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_json_shape_returns_400(self):
        status, payload = route(self.service, "POST", "/organizations", {"request_id": "x"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_conclusion_runs_reverse_lookup_route(self):
        _, conclusion_id = self._bootstrap_lineage()
        status, payload = route(self.service, "GET", f"/conclusions/{conclusion_id}/runs",
                                None, {"X-Actor-Id": "rev1"})
        self.assertEqual(200, status)
        self.assertEqual(conclusion_id, payload["conclusion_id"])
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual(["direct"], payload["items"][0]["linked_via"])

    def test_conclusion_runs_requires_known_actor(self):
        _, conclusion_id = self._bootstrap_lineage()
        status, payload = route(self.service, "GET", f"/conclusions/{conclusion_id}/runs",
                                None, {"X-Actor-Id": "ghost"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_lineage_write_routes(self):
        status, payload = route(self.service, "POST", "/organizations",
                                {"request_id": "org", "organization_id": "o1", "name": "科研机构一"},
                                {"X-Actor-Id": "bootstrap"})
        self.assertEqual(201, status)
        route(self.service, "POST", "/actors",
              {"request_id": "a1", "new_actor_id": "admin1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "a2", "new_actor_id": "op1", "display_name": "操作员",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "admin1"})
        status, payload = route(self.service, "POST", "/datasets",
                                {"request_id": "ds", "dataset_id": "ds1",
                                 "organization_id": "o1", "name": "安全评估数据集"},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/datasets/ds1/versions",
                                {"request_id": "v1", "content": {"samples": ["a"]}},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        self.assertEqual("ds1:1", payload["resource_id"])
        status, payload = route(self.service, "POST", "/runs",
                                {"request_id": "r1", "external_key": "key-1", "dataset_id": "ds1",
                                 "dataset_version": 1, "parameters": {"model": "m1"},
                                 "result_summary": {"verdict": "pass"}},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        run_id = payload["resource_id"]
        status, payload = route(self.service, "GET", f"/runs/{run_id}", None,
                                {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertEqual("key-1", payload["external_key"])
        status, payload = route(self.service, "POST", f"/runs/{run_id}/status",
                                {"request_id": "s1", "status": "retracted", "reason": "数据污染"},
                                {"X-Actor-Id": "admin1"})
        self.assertEqual(200, status)
        status, payload = route(self.service, "GET", "/datasets/ds1/versions", None,
                                {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))


if __name__ == "__main__":
    unittest.main()
