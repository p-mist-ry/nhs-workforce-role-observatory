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


# A source exactly as shipped before configuration: every key field empty.
# Tests use this made-up source, never the live config/sources.yaml, so they
# keep passing whichever real sources you switch on.
UNCONFIGURED = {
    "source_id": "synthetic_unconfigured", "enabled": False, "endpoint": None,
    "documentation_url": None, "terms_url": None, "terms_reviewed_on": None,
    "format": None, "record_path": None, "field_map": {},
}


class TestFailSafe(unittest.TestCase):
    def test_unconfigured_source_is_blocked(self):
        problems = cc.preflight(UNCONFIGURED, DEFAULTS)
        for expected in ["enabled is not true", "endpoint is not filled in",
                         "terms_url is not filled in", "field_map.title is not mapped"]:
            self.assertIn(expected, problems)

    def test_each_missing_field_blocks(self):
        for key in ["endpoint", "documentation_url", "terms_url", "terms_reviewed_on", "format"]:
            src = copy.deepcopy(GOOD)
            src[key] = None
            self.assertTrue(cc.preflight(src, DEFAULTS), key)

    def test_no_network_when_blocked(self):
        with mock.patch.object(cc, "load_source", return_value=(UNCONFIGURED, DEFAULTS)), \
             mock.patch.object(cc, "robots_allows", side_effect=AssertionError("network used")), \
             mock.patch.object(cc, "fetch", side_effect=AssertionError("network used")), \
             mock.patch.object(cc.common, "log_run") as log:
            self.assertEqual(cc.collect("synthetic_unconfigured"), 0)
            self.assertEqual(log.call_args[0][3], "BLOCKED_NOT_CONFIGURED")

    def test_live_config_parses(self):
        """Every source in config/sources.yaml can be loaded (whether on or off)."""
        for sid in ["nhsjobs", "nhs_scotland", "hscni"]:
            src, defaults = cc.load_source(sid)
            self.assertEqual(src["source_id"], sid)
            self.assertIsInstance(cc.preflight(src, defaults), list)

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
    """An unchanged advert is stored once; later days add it to a compact 'seen again' list."""

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
                 mock.patch.object(cc.common, "storage_problem", return_value=None), \
                 mock.patch.object(cc.common, "log_run"), \
                 mock.patch.object(cc, "load_source", return_value=(src, DEFAULTS)), \
                 mock.patch.object(cc, "robots_allows", return_value=True), \
                 mock.patch.object(cc, "fetch", return_value=(200, body)), \
                 mock.patch.object(cc.common, "today_utc", side_effect=["2026-11-20", "2026-11-21"]), \
                 mock.patch.object(cc.common, "make_run_id", side_effect=["run1", "run2", "n1"]):
                self.assertEqual(cc.collect("synthetic"), 0)
                self.assertEqual(cc.collect("synthetic"), 0)
                files = sorted((tmp / "obs").rglob("*.jsonl"))
                recs = [r for f in files for r in cc.common.read_jsonl(f)]
                self.assertEqual([r["schema"] for r in recs], ["raw_observation/1", "raw_sighting_batch/1"])
                self.assertEqual(recs[1]["items"], [recs[0]["observation_id"]])
                self.assertIn("[REDACTED_PHONE]", recs[0]["mapped"]["description"])
                with mock.patch.object(normalize.common, "load_providers", return_value=[]):
                    normalize.main()
                out = [r for f in (tmp / "derived").rglob("*.jsonl") for r in cc.common.read_jsonl(f)]
                self.assertEqual(len(out), 1)       # one advert version...
                self.assertEqual(out[0]["observed_dates"], ["2026-11-20", "2026-11-21"])  # ...seen twice
                self.assertEqual(out[0]["observed"]["title"]["value"], "Porter")


class TestPaging(unittest.TestCase):
    def test_ignored_page_parameter_stops_without_duplicates(self):
        """If the site ignores the page parameter, page 2 repeats page 1: stop, no duplicates."""
        import tempfile
        from pathlib import Path
        body = (b"<nhsSearch><vacancyDetails><id>1</id><title>Porter</title>"
                b"<url>https://example.org/1</url></vacancyDetails></nhsSearch>")
        src = copy.deepcopy(GOOD)
        src.update({"format": "xml", "record_path": "vacancyDetails",
                    "field_map": {"source_record_id": "id", "title": "title", "url": "url"},
                    "pagination": {"mode": "page_param", "param": "page", "start": 1, "max_pages": 5}})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cc.common, "OBSERVATIONS", Path(tmp)), \
                 mock.patch.object(cc.common, "storage_problem", return_value=None), \
                 mock.patch.object(cc.common, "log_run"), \
                 mock.patch.object(cc, "load_source", return_value=(src, DEFAULTS)), \
                 mock.patch.object(cc, "robots_allows", return_value=True), \
                 mock.patch.object(cc.time, "sleep"), \
                 mock.patch.object(cc, "fetch", return_value=(200, body)) as f:
                self.assertEqual(cc.collect("synthetic"), 0)
                self.assertEqual(f.call_count, 2)      # page 1, then page 2 found nothing new
                recs = [r for p in Path(tmp).rglob("*.jsonl") for r in cc.common.read_jsonl(p)]
                self.assertEqual(len(recs), 1)
