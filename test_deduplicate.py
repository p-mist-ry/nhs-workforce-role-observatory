import unittest

import helpers
import deduplicate as dd

DESC = ("We are looking for an enthusiastic healthcare support worker to join the "
        "respiratory ward team, providing personal care and recording clinical observations "
        "under the supervision of registered nurses on a rotating shift pattern.")


class TestDedup(unittest.TestCase):
    def test_same_record_many_days_is_one(self):
        recs = [helpers.make_normalized(f"o{i}", "nhsjobs", "HCSW", "Example Trust", DESC,
                                        f"2026-09-0{i}", record_id="A1") for i in range(1, 4)]
        out = dd.cluster(recs, 0.85, 45, {})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["members"][0]["n_observations"], 3)

    def test_cross_posting_merged(self):
        a = helpers.make_normalized("o1", "nhsjobs", "HCSW", "Example Trust", DESC, "2026-09-01", "A1")
        b = helpers.make_normalized("o2", "other", "HCSW", "Example Trust", DESC, "2026-09-02", "B7")
        out = dd.cluster([a, b], 0.85, 45, {"nhsjobs": 1, "other": 2})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["n_sources"], 2)
        self.assertEqual(out[0]["canonical_source_id"], "nhsjobs")
        self.assertEqual(out[0]["dedup_links"][0]["method"], "cross_source_text_similarity")

    def test_different_employers_not_merged(self):
        a = helpers.make_normalized("o1", "nhsjobs", "HCSW", "Trust A", DESC, "2026-09-01", "A1")
        b = helpers.make_normalized("o2", "other", "HCSW", "Trust B", DESC, "2026-09-01", "B1")
        self.assertEqual(len(dd.cluster([a, b], 0.85, 45, {})), 2)

    def test_readvert_after_gap_is_new(self):
        a = helpers.make_normalized("o1", "nhsjobs", "HCSW", "Trust A", DESC, "2026-01-01", "A1", closing="2026-01-20")
        b = helpers.make_normalized("o2", "nhsjobs", "HCSW", "Trust A", DESC, "2026-06-01", "A1", closing="2026-06-20")
        self.assertEqual(len(dd.cluster([a, b], 0.85, 45, {})), 2)


if __name__ == "__main__":
    unittest.main()
