import unittest

from ai_governance_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertTrue(result["lineage_ok"])
        lineage = result["lineage"]
        self.assertTrue(lineage["run_import_idempotent"])
        self.assertEqual(2, lineage["dataset_versions"])
        self.assertEqual("superseded", lineage["first_version_status"])
        self.assertTrue(lineage["published_kept_basis"])
        self.assertEqual(1, lineage["impact_statements"])
        self.assertTrue(lineage["draft_publish_blocked"])
        self.assertEqual(1, lineage["reverse_lookup_runs"])
        self.assertEqual(1, lineage["reverse_lookup_affected"])
        self.assertTrue(lineage["auditor_limited_fields"])
        self.assertTrue(lineage["reviewer_hides_internal_notes"])
        self.assertTrue(lineage["operator_full_fields"])


if __name__ == "__main__":
    unittest.main()
