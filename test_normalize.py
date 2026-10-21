import unittest

import helpers  # noqa: F401  (sets import path)
import common
import normalize as n


class TestPay(unittest.TestCase):
    def test_annual_range(self):
        v, ev = n.parse_pay("£24,071 to £25,674 a year")
        self.assertEqual((v["min"], v["max"], v["period"]), (24071, 25674, "year"))
        self.assertIn("£24,071", ev)

    def test_hourly_pro_rata(self):
        v, _ = n.parse_pay("£12.50 per hour pro rata")
        self.assertEqual((v["min"], v["period"], v["pro_rata"]), (12.5, "hour", True))

    def test_period_ambiguous_is_null(self):
        v, _ = n.parse_pay("£30,000 per annum or £15.40 per hour for bank")
        self.assertIsNone(v["period"])

    def test_no_amount(self):
        self.assertEqual(n.parse_pay("Competitive salary"), (None, None))


class TestBandHoursDates(unittest.TestCase):
    def test_band_8a(self):
        self.assertEqual(n.parse_band("Band 8A (£50,470 - £54,482)")[0], ["8A"])

    def test_band_range(self):
        self.assertEqual(n.parse_band("Band 5/6")[0], ["5", "6"])

    def test_hours_various_is_null(self):
        self.assertEqual(n.parse_hours("Various", True), (None, None))

    def test_hours_field(self):
        self.assertEqual(n.parse_hours("37.50", True)[0], {"min": 37.5, "max": 37.5})

    def test_hours_description_needs_week(self):
        self.assertIsNone(n.parse_hours("a 12 hours shift pattern", False)[0])
        self.assertEqual(n.parse_hours("working 22.5 hours per week", False)[0]["min"], 22.5)

    def test_dates_uk_order(self):
        self.assertEqual(n.parse_date("Closing date: 11/06/2021")[0], "2021-06-11")
        self.assertEqual(n.parse_date("Closes:Tue, 15 April 2025")[0], "2025-04-15")
        self.assertIsNone(n.parse_date("31/02/2025")[0])


class TestNation(unittest.TestCase):
    def test_postcodes(self):
        self.assertEqual(n.nation_from_postcode("EH16 4SA")[0], "Scotland")
        self.assertEqual(n.nation_from_postcode("BT9 7AB")[0], "Northern Ireland")
        self.assertEqual(n.nation_from_postcode("CF14 4XW")[0], "Wales")
        self.assertEqual(n.nation_from_postcode("LS1 4AP")[0], "England")
        self.assertIsNone(n.nation_from_postcode("CH5 1AB")[0])   # cross-border
        self.assertIsNone(n.nation_from_postcode("JE2 3QS")[0])   # Crown Dependency

    def test_conflict_gives_unknown(self):
        prov = {"nation": "Wales", "verification_status": "UNVERIFIED", "provider_id": "X"}
        res = n.decide_nation(["England", "Wales"], prov, "LS1 4AP")
        self.assertEqual(res["value"], "UNKNOWN")
        self.assertTrue(res["conflict"])

    def test_nothing_known(self):
        self.assertEqual(n.decide_nation(["England", "Wales"], None, None)["value"], "UNKNOWN")


class TestRedaction(unittest.TestCase):
    def test_removes_contacts_keeps_facts(self):
        text = ("Contact jane.doe@nhs.net or 01234 567890 / 07700 900123. "
                "Salary £24,071, 37.5 hours, closing 2021-06-11, ref 065849.")
        out, k = common.redact_contacts(text)
        self.assertEqual(k, 3)
        for kept in ["£24,071", "37.5 hours", "2021-06-11", "065849"]:
            self.assertIn(kept, out)
        self.assertNotIn("nhs.net", out)


class TestNormaliseRecord(unittest.TestCase):
    def test_missing_values_stay_null(self):
        raw = {"observation_id": "o1", "source_id": "s", "mapped": {"title": "Porter", "description": "  "}}
        rec = n.normalise(raw, ["England", "Wales"], {})
        self.assertIsNone(rec["observed"]["description"]["value"])
        self.assertIsNone(rec["observed"]["pay"]["value"])
        self.assertEqual(rec["inferred"]["nation"]["value"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
