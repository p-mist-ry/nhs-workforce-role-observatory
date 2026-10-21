import unittest

import helpers  # noqa: F401
import common
import classify_genai as g

TAX = common.load_yaml(common.CONFIG / "taxonomy.yaml")
TEXT = "You will validate outputs from the AI triage tool before they reach patients."


class TestDeterministicChecks(unittest.TestCase):
    def test_invented_quote_rejected(self):
        out = {"x": [{"quote": "validate outputs from the AI triage tool"}, {"quote": "write Python"}]}
        res = g.check_spans(out, TEXT)
        self.assertEqual(res["n_missing"], 1)

    def test_whitespace_tolerant(self):
        self.assertEqual(g.check_spans({"quote": "validate  outputs\nfrom"}, TEXT)["n_missing"], 0)

    def test_labels(self):
        bad = {"role_family": {"label": "astronaut", "evidence": []}}
        self.assertTrue(g.check_labels(bad, g.label_sets(TAX)))
        ok = {"ai_relationships": [{"label": "AI_NONE_OBSERVED", "evidence": []}]}
        self.assertEqual(g.check_labels(ok, g.label_sets(TAX)), [])

    def test_confidence_range(self):
        self.assertTrue(g.check_confidences({"a": {"confidence": 1.4}}))
        self.assertFalse(g.check_confidences({"a": {"confidence": 0.7}}))

    def test_prompts_render_without_leftovers(self):
        body, meta = g.load_prompt("extraction")
        self.assertEqual(meta["prompt_id"], "extraction")
        g.render(body, {"ADVERT_META": {}, "ADVERT_TEXT": TEXT})


if __name__ == "__main__":
    unittest.main()
