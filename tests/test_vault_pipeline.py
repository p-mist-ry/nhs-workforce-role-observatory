"""Tests for vault storage, full paging, search probes and quote-free public outputs.

All data here is synthetic. Nothing touches the network or a real vault.
"""
import copy
import subprocess
import tempfile
import time as _time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import helpers
import build_dashboard_data as bdd
import classify_rules as cr
import collect_core as cc
import common
import deduplicate as dd
import normalize
import probe_feed_search as probe
import validate

SRC = {
    "source_id": "synthetic", "enabled": True, "endpoint": "https://feed.example.org/search_xml",
    "documentation_url": "https://docs.example.org", "terms_url": "https://example.org/terms",
    "terms_reviewed_on": "2026-11-24", "format": "xml", "record_path": "vacancyDetails",
    "field_map": {"source_record_id": "id", "title": "title", "url": "url",
                  "employer": "employer", "description": "description"},
    "pagination": {"mode": "page_param", "param": "page", "start": 1, "max_pages": 10},
    "params": {"limit": 100},
}
DEFAULTS = {"user_agent": "Observatory (+https://github.com/example/observatory)",
            "min_seconds_between_requests": 0}


def page_xml(ids):
    items = "".join(f"<vacancyDetails><id>{i}</id><title>Porter</title><employer>Trust A</employer>"
                    f"<url>https://example.org/{i}</url><description>Moving patients.</description>"
                    f"</vacancyDetails>" for i in ids)
    return f"<nhsJobs>{items}</nhsJobs>".encode()


class TempStore:
    """Point every storage path at a fresh temporary folder."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [mock.patch.object(common, name, base / sub) for name, sub in
                        [("OBSERVATIONS", "observations"), ("PROBES", "probes"),
                         ("DERIVED", "derived"), ("DASHBOARD", "dashboard"), ("DOCS_DATA", "docs")]]
        self.patches.append(mock.patch.object(common, "log_run"))
        for p in self.patches:
            p.start()
        return base

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class TestVaultGuard(unittest.TestCase):
    def test_no_vault_means_no_collection(self):
        with mock.patch.object(common, "VAULT", None), \
             mock.patch.object(cc, "load_source", return_value=(SRC, DEFAULTS)), \
             mock.patch.object(cc, "fetch", side_effect=AssertionError("network used")), \
             mock.patch.object(common, "log_run") as log:
            self.assertEqual(cc.collect("synthetic"), 0)
            self.assertEqual(log.call_args[0][3], "BLOCKED_NO_VAULT")

    def test_shipped_settings_require_vault(self):
        with mock.patch.object(common, "VAULT", None):
            self.assertIn("vault", common.storage_problem())


class TestFullPaging(unittest.TestCase):
    def run_collect(self, pages, base, day="2026-11-24"):
        responses = iter(pages)

        def fake_fetch(url, defaults):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return 200, item
        with mock.patch.object(common, "storage_problem", return_value=None), \
             mock.patch.object(cc, "load_source", return_value=(SRC, DEFAULTS)), \
             mock.patch.object(cc, "robots_allows", return_value=True), \
             mock.patch.object(cc, "fetch", side_effect=fake_fetch) as f, \
             mock.patch.object(common, "today_utc", return_value=day):
            cc.collect("synthetic")
        return f

    def test_reads_every_page_until_empty(self):
        with TempStore() as base:
            f = self.run_collect([page_xml([1, 2]), page_xml([3]), page_xml([])], base)
            self.assertEqual(f.call_count, 3)
            urls = [c.args[0] for c in f.call_args_list]
            self.assertIn("page=2", urls[1])
            self.assertIn("limit=100", urls[0])
            recs = [r for p in base.rglob("*.jsonl") for r in common.read_jsonl(p)]
            self.assertEqual(sorted(r["source_record_id"] for r in recs), ["1", "2", "3"])
            self.assertEqual(common.log_run.call_args[0][3], "OK")

    def test_failure_part_way_keeps_pages_already_read(self):
        with TempStore() as base:
            self.run_collect([page_xml([1, 2]), urllib.error.URLError("down")], base)
            recs = [r for p in base.rglob("*.jsonl") for r in common.read_jsonl(p)]
            self.assertEqual(len(recs), 2)
            self.assertEqual(common.log_run.call_args[0][3], "PARTIAL")
            self.assertIn("stopped at page 2", common.log_run.call_args[0][7])

    def test_failure_on_first_page_is_an_error(self):
        with TempStore() as base:
            self.run_collect([urllib.error.URLError("down")], base)
            self.assertEqual(list(base.rglob("*.jsonl")), [])
            self.assertEqual(common.log_run.call_args[0][3], "ERROR")

    def test_page_cap_is_reported(self):
        src = copy.deepcopy(SRC)
        src["pagination"]["max_pages"] = 2
        with TempStore(), \
             mock.patch.object(common, "storage_problem", return_value=None), \
             mock.patch.object(cc, "load_source", return_value=(src, DEFAULTS)), \
             mock.patch.object(cc, "robots_allows", return_value=True), \
             mock.patch.object(cc, "fetch", side_effect=[(200, page_xml([1])), (200, page_xml([2]))]):
            cc.collect("synthetic")
            self.assertIn("max_pages", common.log_run.call_args[0][7])


class TestRetries(unittest.TestCase):
    def test_temporary_failure_is_retried(self):
        with mock.patch.object(cc.time, "sleep") as sleep, \
             mock.patch.object(cc, "fetch_once", side_effect=[urllib.error.URLError("blip"), (200, b"ok")]):
            self.assertEqual(cc.fetch("https://x.org", DEFAULTS), (200, b"ok"))
            sleep.assert_called_once()

    def test_not_found_is_not_retried(self):
        err = urllib.error.HTTPError("https://x.org", 404, "Not Found", {}, None)
        with mock.patch.object(cc.time, "sleep"), \
             mock.patch.object(cc, "fetch_once", side_effect=err) as once:
            with self.assertRaises(urllib.error.HTTPError):
                cc.fetch("https://x.org", DEFAULTS)
            self.assertEqual(once.call_count, 1)


class TestSightingsOverTime(unittest.TestCase):
    def test_dates_flow_through_normalise_and_dedup(self):
        with TempStore() as base, \
             mock.patch.object(common, "storage_problem", return_value=None), \
             mock.patch.object(cc, "load_source", return_value=(SRC, DEFAULTS)), \
             mock.patch.object(cc, "robots_allows", return_value=True), \
             mock.patch.object(normalize.common, "load_providers", return_value=[]):
            for day in ["2026-11-22", "2026-11-23", "2026-11-24"]:
                with mock.patch.object(cc, "fetch", side_effect=[(200, page_xml([7])), (200, page_xml([]))]), \
                     mock.patch.object(common, "today_utc", return_value=day):
                    cc.collect("synthetic")
            normalize.main()
            recs = list(common.read_jsonl(base / "derived" / "normalized" / "synthetic.jsonl"))
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["observed_dates"], ["2026-11-22", "2026-11-23", "2026-11-24"])
            clusters = dd.cluster(recs, 0.85, 45, {})
            self.assertEqual(len(clusters), 1)
            self.assertEqual((clusters[0]["first_seen"], clusters[0]["last_seen"]), ("2026-11-22", "2026-11-24"))
            self.assertEqual(clusters[0]["members"][0]["n_observations"], 3)

    def test_older_one_line_sightings_still_read(self):
        with TempStore() as base, mock.patch.object(normalize.common, "load_providers", return_value=[]):
            raw = {"schema": "raw_observation/1", "observation_id": "a1", "source_id": "nhsjobs",
                   "observed_date": "2026-11-19", "mapped": {"title": "Porter"}}
            old = {"schema": "raw_sighting/1", "observation_id": "s1", "source_id": "nhsjobs",
                   "observed_date": "2026-11-20", "same_content_as": "a1"}
            common.write_jsonl(base / "observations" / "nhsjobs" / "2026-11-19" / "r1.jsonl", [raw])
            common.write_jsonl(base / "observations" / "nhsjobs" / "2026-11-20" / "r2.jsonl", [old])
            normalize.main()
            recs = list(common.read_jsonl(base / "derived" / "normalized" / "nhsjobs.jsonl"))
            self.assertEqual(recs[0]["observed_dates"], ["2026-11-19", "2026-11-20"])


class TestDedupScales(unittest.TestCase):
    def test_many_adverts_same_title_and_employer_is_fast(self):
        recs = []
        for i in range(3000):
            day = (helpers_date(i // 8))
            recs.append(helpers.make_normalized(f"o{i}", "nhsjobs", "Staff Nurse", "Big Trust",
                                                f"Unique duties number {i} for ward {i}.", day,
                                                record_id=str(i)))
        start = _time.time()
        out = dd.cluster(recs, 0.85, 45, {})
        self.assertLess(_time.time() - start, 20)
        self.assertEqual(len(out), 3000)


class TestTruncatedTextNotMerged(unittest.TestCase):
    def test_separate_adverts_with_identical_extracts_stay_separate(self):
        extract = "We have an exciting opportunity for a Staff Nurse to join our busy medical ward team at the..."
        a = helpers.make_normalized("o1", "nhsjobs", "Staff Nurse", "Trust A", extract, "2026-11-24",
                                    record_id="1", closing="2026-12-10")
        b = helpers.make_normalized("o2", "nhsjobs", "Staff Nurse", "Trust A", extract, "2026-11-24",
                                    record_id="2", closing="2026-12-10")
        self.assertEqual(len(dd.cluster([a, b], 0.85, 45, {})), 2)

    def test_identical_full_adverts_are_merged(self):
        full = "Main duties include " + "caring for patients on the ward and supporting colleagues. " * 8
        a = helpers.make_normalized("o1", "nhsjobs", "Staff Nurse", "Trust A", full, "2026-11-24",
                                    record_id="1", closing="2026-12-10")
        b = helpers.make_normalized("o2", "nhsjobs", "Staff Nurse", "Trust A", full, "2026-11-24",
                                    record_id="2", closing="2026-12-10")
        out = dd.cluster([a, b], 0.85, 45, {})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["dedup_links"][0]["method"], "same_source_identical_text")


def helpers_date(n):
    import datetime as dt
    return (dt.date(2026, 1, 1) + dt.timedelta(days=n)).isoformat()


class TestSearchProbes(unittest.TestCase):
    def test_probe_records_ids_and_links_to_vacancies(self):
        settings = {"enabled": True, "source_id": "synthetic", "params": {"limit": 100},
                    "max_pages_per_term": 5,
                    "terms": [{"id": "artificial_intelligence", "keyword": "artificial intelligence"}]}
        with TempStore() as base, \
             mock.patch.object(common, "storage_problem", return_value=None), \
             mock.patch.object(common, "load_yaml", side_effect=lambda p: {"feed_search_probes": settings}
                               if p.name == "pipeline.yaml" else common_load_yaml(p)), \
             mock.patch.object(cc, "load_source", return_value=(SRC, DEFAULTS)), \
             mock.patch.object(cc, "robots_allows", return_value=True), \
             mock.patch.object(cc, "fetch", side_effect=[(200, page_xml([7, 9])), (200, page_xml([]))]) as f, \
             mock.patch.object(common, "today_utc", return_value="2026-11-24"):
            probe.main()
            self.assertIn("keyword=artificial+intelligence", f.call_args_list[0].args[0])
            rec = next(common.read_jsonl(next(base.joinpath("probes").rglob("*.jsonl"))))
            self.assertEqual(rec["source_record_ids"], ["7", "9"])
            self.assertTrue(rec["complete"])
            self.assertEqual(rec["verification"], "UNVERIFIED_SEARCH_MATCH")
            self.assertNotIn("description", str(rec))        # no advert text is stored
            index = cr.load_search_matches()
            cluster = {"cluster_id": "c1", "members": [{"source_id": "synthetic", "source_record_id": "7"}]}
            res = cr.attach_search_matches([{"cluster_id": "c1", "ai": {}}], [cluster], index)
            self.assertEqual(res[0]["ai"]["feed_search_matches"], {"artificial_intelligence": "2026-11-24"})

    def test_probe_blocked_without_vault(self):
        with mock.patch.object(common, "VAULT", None), \
             mock.patch.object(cc, "fetch", side_effect=AssertionError("network used")), \
             mock.patch.object(common, "log_run") as log:
            probe.main()
            self.assertEqual(log.call_args[0][3], "BLOCKED")


common_load_yaml = common.load_yaml


class TestPublicOutputs(unittest.TestCase):
    def test_no_quoted_sentences_by_default(self):
        pair = {"pair_id": "p", "ai_sentences_added": ["We use an AI scribe in clinic."],
                "ai_sentences_removed": [], "url_a": "https://a", "url_b": "https://b"}
        out = bdd.public_pair(pair)
        self.assertNotIn("ai_sentences_added", out)
        self.assertEqual(out["n_ai_sentences_added"], 1)
        self.assertNotIn("We use", str(out))
        e = bdd.public_emergence({"term_id": "ai_scribe", "evidence": "AI scribe",
                                  "sentence": "We use an AI scribe in clinic.", "record_url": "https://a"})
        self.assertNotIn("sentence", e)
        self.assertEqual(e["record_url"], "https://a")

    def test_snippets_kept_only_when_allowed(self):
        pair = {"ai_sentences_added": ["x"], "ai_sentences_removed": []}
        self.assertIn("ai_sentences_added", bdd.public_pair(pair, keep_text=True))

    def test_shipped_setting_is_no_snippets(self):
        cfg = common.load_yaml(common.CONFIG / "pipeline.yaml")
        self.assertFalse(cfg["publish"]["evidence_snippets"])
        self.assertTrue(cfg["storage"]["require_vault"])


class TestValidateVault(unittest.TestCase):
    def test_edited_vault_file_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            obs = vault / "observations" / "nhsjobs" / "2026-11-24"
            obs.mkdir(parents=True)
            (obs / "r1.jsonl").write_text('{"a": 1}\n')
            git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
            subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
            subprocess.run(git + ["commit", "-qm", "x"], cwd=vault, check=True)
            (obs / "r1.jsonl").write_text('{"a": 2}\n')          # tampering
            validate.RESULTS.clear()
            with mock.patch.object(common, "VAULT", vault):
                validate.check_append_only("HEAD")
            fails = [r for r in validate.RESULTS if r["status"] == "FAIL"]
            self.assertTrue(any("vault/observations" in r["file"] for r in fails))
            validate.RESULTS.clear()

    def test_batch_records_validate(self):
        with TempStore() as base:
            batch = {"schema": "raw_sighting_batch/1", "observation_id": "b1", "run_id": "r",
                     "source_id": "nhsjobs", "observed_date": "2026-11-24",
                     "request_url": "https://www.jobs.nhs.uk/api/v1/search_xml", "n_items": 2,
                     "items": ["a", "b"]}
            common.write_jsonl(base / "observations" / "nhsjobs" / "2026-11-24" / "r.jsonl", [batch])
            validate.RESULTS.clear()
            validate.check_observations()
            self.assertFalse([r for r in validate.RESULTS if r["status"] == "FAIL"])
            validate.RESULTS.clear()


if __name__ == "__main__":
    unittest.main()
