import copy
import unittest
from unittest import mock

import helpers  # noqa: F401
import collect_core as cc

GOOD = {
    "source_id": "synthetic", "enabled": True, "endpoint": "https://feed.example.org/jobs",
    "documentation_url": "https://docs.example.org", "terms_url": "https://example.org/terms",
    "terms_reviewed_on": "2026-10-20", "format": "rss", "record_path": None,
    "field_map": {"title": "title", "url": "link"},
}
DEFAULTS = {"user_agent": "Observatory (contact: someone@example.org)"}


class TestFailSafe(unittest.TestCase):
    def test_shipped_sources_are_blocked(self):
        for sid in ["nhsjobs", "nhs_scotland", "hscni"]:
            src, defaults = cc.load_source(sid)
            self.assertTrue(cc.preflight(src, defaults), sid)

    def test_no_network_when_blocked(self):
        with mock.patch.object(cc, "fetch", side_effect=AssertionError("network used")), \
             mock.patch.object(cc.common, "log_run") as log:
            self.assertEqual(cc.collect("nhsjobs"), 0)
            self.assertEqual(log.call_args[0][3], "BLOCKED_NOT_CONFIGURED")

    def test_placeholder_user_agent_blocks(self):
        problems = cc.preflight(GOOD, {"user_agent": "x CHANGE_ME"})
        self.assertTrue(any("CHANGE_ME" in p for p in problems))

    def test_complete_config_passes(self):
        self.assertEqual(cc.preflight(GOOD, DEFAULTS), [])

    def test_http_endpoint_rejected(self):
        bad = copy.deepcopy(GOOD)
        bad["endpoint"] = "http://feed.example.org"
        self.assertTrue(cc.preflight(bad, DEFAULTS))


class TestParsing(unittest.TestCase):
    def test_rss_mapping(self):
        body = (b"<rss><channel><item><title>Porter</title><link>https://example.org/1</link>"
                b"<description>Contact a.b@nhs.net</description></item></channel></rss>")
        recs = cc.parse_records(body, GOOD)
        mapped = cc.map_record(recs[0], {"title": "title", "url": "link", "description": "description"})
        self.assertEqual(mapped["title"], "Porter")
        self.assertIsNone(mapped["pay_text"])


if __name__ == "__main__":
    unittest.main()


class TestSightings(unittest.TestCase):
    """Second sighting of an unchanged advert stores a small pointer, not the text again."""

    def test_unchanged_advert_stored_once(self):
        import tempfile
        from pathlib import Path
        import normalize
        body = (b"<rss><channel><item><title>Porter</title><link>https://example.org/1</link>"
                b"<description>Moving patients safely. Call 01234 567890.</description></item>"
                b"</channel></rss>")
        src = copy.deepcopy(GOOD)
        src["field_map"] = {"title": "title", "url": "link", "description": "description"}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(cc.common, "OBSERVATIONS", tmp / "obs"), \
                 mock.patch.object(cc.common, "DERIVED", tmp / "derived"), \
                 mock.patch.object(cc.common, "log_run"), \
                 mock.patch.object(cc, "load_source", return_value=(src, DEFAULTS)), \
                 mock.patch.object(cc, "robots_allows", return_value=True), \
                 mock.patch.object(cc, "fetch", return_value=(200, body)), \
                 mock.patch.object(cc.common, "make_run_id", side_effect=["run1", "run2", "n1"]):
                self.assertEqual(cc.collect("synthetic"), 0)
                self.assertEqual(cc.collect("synthetic"), 0)
                files = sorted((tmp / "obs").rglob("*.jsonl"))
                recs = [r for f in files for r in cc.common.read_jsonl(f)]
                self.assertEqual([r["schema"] for r in recs], ["raw_observation/1", "raw_sighting/1"])
                self.assertEqual(recs[1]["same_content_as"], recs[0]["observation_id"])
                self.assertIn("[REDACTED_PHONE]", recs[0]["mapped"]["description"])
                with mock.patch.object(normalize.common, "load_providers", return_value=[]):
                    normalize.main()
                out = [r for f in (tmp / "derived").rglob("*.jsonl") for r in cc.common.read_jsonl(f)]
                self.assertEqual(len(out), 2)
                self.assertEqual(out[1]["content_from_observation_id"], recs[0]["observation_id"])
                self.assertEqual(out[1]["observed"]["title"]["value"], "Porter")
