import unittest

import helpers
import common
import classify_rules as cr

TAX = common.load_yaml(common.CONFIG / "taxonomy.yaml")
LEX = common.load_yaml(common.CONFIG / "ai_lexicon.yaml")


def run(clusters):
    return {r["cluster_id"]: r for r in cr.run(clusters, TAX, LEX)}


class TestTitleOnlyGuard(unittest.TestCase):
    def test_title_alone_is_unknown(self):
        r = run([helpers.make_cluster("c1", "Registered Nurse", "Join our friendly team on Ward 3.")])["c1"]
        self.assertEqual(r["role_family"]["value"], "UNKNOWN")
        self.assertEqual(r["role_family"]["reason"], "TITLE_ONLY_EVIDENCE_INSUFFICIENT")
        self.assertTrue(r["role_family"]["title_hints"])

    def test_description_evidence_classifies(self):
        d = "You must hold current NMC registration as a registered nurse."
        r = run([helpers.make_cluster("c1", "Ward Sister", d)])["c1"]
        self.assertEqual(r["role_family"]["value"], "nursing_midwifery")
        ev = r["role_family"]["evidence"][0]
        self.assertEqual(d[ev["start"]:ev["end"]], ev["text"])
        self.assertEqual(r["clinical_status"]["value"], "CLINICAL")


class TestAILexicon(unittest.TestCase):
    def test_false_positives_excluded(self):
        d = ("Applicants with an LLM (Master of Laws) are welcome. Administer 500ml infusions. "
             "Experience of artificial insemination (AI) clinics.")
        r = run([helpers.make_cluster("c1", "Clinic Nurse", d)])["c1"]
        self.assertEqual(r["ai"]["mentions"], [])
        self.assertEqual(r["ai"]["relationship_candidates"], ["AI_NONE_OBSERVED"])

    def test_role_ai_content(self):
        d = "You will review AI-assisted triage outputs and support the ambient voice technology pilot."
        r = run([helpers.make_cluster("c1", "Advanced Practitioner", d)])["c1"]
        self.assertTrue(r["ai"]["headline_ai_role_content"])
        self.assertIn("AI_SUPPORTED_TASK", r["ai"]["relationship_candidates"])

    def test_boilerplate_is_organisational_context(self):
        boiler = "Our trust is proud to be a leader in artificial intelligence and digital innovation across the region."
        titles = ["Porter", "Domestic Assistant", "Catering Assistant"]
        clusters = [helpers.make_cluster(f"c{i}", titles[i], boiler + f" Duty number {i} on site.")
                    for i in range(3)]
        out = run(clusters)
        for r in out.values():
            self.assertFalse(r["ai"]["headline_ai_role_content"])
            self.assertEqual(r["ai"]["relationship_candidates"], ["AI_ORGANISATIONAL_CONTEXT"])

    def test_repeated_job_description_is_not_boilerplate(self):
        d = ("You will review AI-assisted triage outputs with the clinical team and escalate concerns "
             "to the nurse in charge on every shift.")
        clusters = [helpers.make_cluster(f"c{i}", "Triage Nurse", d) for i in range(30)]
        for r in run(clusters).values():
            self.assertTrue(r["ai"]["headline_ai_role_content"])


if __name__ == "__main__":
    unittest.main()
