"""Deterministic, rule-based classification of vacancy clusters.

Principles enforced here:
  * Never classify from the title alone: only DESCRIPTION matches score.
    Title matches are kept as `title_hints` for transparency.
  * Every classification carries evidence spans with character offsets
    into the canonical description, so validate.py can re-check them.
  * Sentences repeated across adverts for different roles (employer
    boilerplate) are excluded from role scoring, and AI mentions inside them are reported
    as AI_ORGANISATIONAL_CONTEXT rather than role content.

Input : data/derived/dedup/vacancy_clusters.jsonl
Output: data/derived/classified/rules_classification.jsonl (rebuilt)
"""
from __future__ import annotations

import re
from collections import defaultdict

import common


# ------------------------------------------------------------------ compile config
def compile_taxonomy(tax):
    fams = []
    for f in tax["role_families"]:
        fams.append({"id": f["id"], "clinical": f["clinical"],
                     "indicators": [(re.compile(i["pattern"], re.I), int(i["weight"]), i["pattern"])
                                    for i in f["indicators"]]})
    tasks = [{"id": t["id"], "indicators": [(re.compile(p, re.I), p) for p in t["indicators"]]}
             for t in tax["task_categories"]]
    clinical = [re.compile(p, re.I) for p in tax["rule_settings"]["clinical_indicators"]]
    return fams, tasks, clinical


def compile_lexicon(lex):
    terms = []
    for t in lex["terms"]:
        flags = 0 if t.get("case_sensitive") else re.I
        terms.append({
            "id": t["id"], "category": t["category"],
            "rx": re.compile(t["pattern"], flags),
            "exclude": re.compile(t["exclude_if_sentence_matches"]) if t.get("exclude_if_sentence_matches") else None,
            "require": re.compile(t["require_sentence_matches"]) if t.get("require_sentence_matches") else None,
        })
    return terms


# ------------------------------------------------------------------ helpers
def desc_and_title(cluster):
    obs = cluster["canonical"]["observed"]
    return (obs.get("description") or {}).get("value") or "", (obs.get("title") or {}).get("value") or ""


def employer_key(cluster):
    return common.norm_key((cluster["canonical"]["observed"].get("employer") or {}).get("value"))


def sentence_keys(text, min_chars):
    return {common.norm_key(s) for _a, _b, s in common.split_sentences(text) if len(s) >= min_chars}


def boilerplate_counts(clusters, min_chars):
    """Count how many DIFFERENT roles (normalised titles) each sentence appears in."""
    by_emp, overall = defaultdict(set), defaultdict(set)
    for c in clusters:
        desc, title = desc_and_title(c)
        emp, tkey = employer_key(c), common.norm_key(title) or c["cluster_id"]
        for k in sentence_keys(desc, min_chars):
            overall[k].add(tkey)
            if emp:
                by_emp[(emp, k)].add(tkey)
    return ({k: len(v) for k, v in by_emp.items()}, {k: len(v) for k, v in overall.items()})


def sentence_for(sentences, pos):
    for a, b, s in sentences:
        if a <= pos < b:
            return a, b, s
    return None


def span(text, m, fld, **extra):
    return {"field": fld, "start": m.start(), "end": m.end(), "text": text[m.start():m.end()]} | extra


# ------------------------------------------------------------------ classify one
def classify(cluster, fams, tasks, clinical_rx, terms, lex, settings, by_emp, overall):
    desc, title = desc_and_title(cluster)
    bp = lex["boilerplate"]
    emp = employer_key(cluster)
    sentences = common.split_sentences(desc)

    def is_boiler(sent):
        if not sent or len(sent[2]) < bp["min_sentence_chars"]:
            return False
        k = common.norm_key(sent[2])
        return (by_emp.get((emp, k), 0) >= bp["min_distinct_titles_same_employer"]
                or overall.get(k, 0) >= bp["min_distinct_titles_any_employer"])

    boiler_ranges = [(a, b) for a, b, s in sentences if is_boiler((a, b, s))]

    def in_boiler(pos):
        return any(a <= pos < b for a, b in boiler_ranges)

    # ---- role family
    scores, evidence, title_hints = {}, {}, []
    for f in fams:
        score, ev = 0, []
        for rx, w, pat in f["indicators"]:
            hits = [m for m in rx.finditer(desc) if not in_boiler(m.start())]
            if hits:
                score += w
                ev.append(span(desc, hits[0], "description", indicator=pat, weight=w))
            tm = rx.search(title)
            if tm:
                title_hints.append({"family": f["id"], "field": "title", "text": tm.group(0)})
        if score:
            scores[f["id"]], evidence[f["id"]] = score, ev
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    rs = settings
    if ranked and ranked[0][1] >= rs["min_description_score"]:
        top, top_score = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0
        if top_score - runner >= rs["min_margin_over_runner_up"]:
            family = {"value": top, "reason": "DESCRIPTION_EVIDENCE", "score": top_score,
                      "evidence": evidence[top]}
        else:
            family = {"value": "AMBIGUOUS", "reason": "TIED_OR_CLOSE_SCORES", "score": top_score,
                      "candidates": [k for k, s in ranked if top_score - s < rs["min_margin_over_runner_up"]],
                      "evidence": [e for k, s in ranked[:2] for e in evidence[k]]}
    else:
        reason = "TITLE_ONLY_EVIDENCE_INSUFFICIENT" if title_hints else \
            ("NO_DESCRIPTION" if not desc else "INSUFFICIENT_DESCRIPTION_EVIDENCE")
        family = {"value": common.UNKNOWN, "reason": reason,
                  "score": ranked[0][1] if ranked else 0, "evidence": []}
    family["title_hints"] = title_hints

    # ---- clinical status
    fam_cfg = next((f for f in fams if f["id"] == family["value"]), None)
    if fam_cfg is None:
        clinical = {"value": common.UNKNOWN, "basis": "role family not determined", "evidence": []}
    elif fam_cfg["clinical"] == "clinical":
        clinical = {"value": "CLINICAL", "basis": "role family", "evidence": family["evidence"][:1]}
    elif fam_cfg["clinical"] == "non_clinical":
        clinical = {"value": "NON_CLINICAL", "basis": "role family", "evidence": family["evidence"][:1]}
    else:
        hit = next((m for rx in clinical_rx for m in rx.finditer(desc) if not in_boiler(m.start())), None)
        clinical = ({"value": "CLINICAL", "basis": "clinical indicator in mixed family",
                     "evidence": [span(desc, hit, "description")]} if hit else
                    {"value": common.UNKNOWN, "basis": "mixed family without clinical indicator", "evidence": []})

    # ---- tasks
    task_out = []
    for t in tasks:
        ev = []
        for rx, pat in t["indicators"]:
            m = next((m for m in rx.finditer(desc) if not in_boiler(m.start())), None)
            if m:
                ev.append(span(desc, m, "description", indicator=pat))
        if ev:
            task_out.append({"task_category": t["id"], "evidence": ev})

    # ---- AI mentions
    mentions = []
    for term in terms:
        for m in term["rx"].finditer(desc):
            sent = sentence_for(sentences, m.start())
            stext = sent[2] if sent else desc
            if term["exclude"] and term["exclude"].search(stext):
                continue
            if term["require"] and not term["require"].search(stext):
                continue
            mentions.append(span(desc, m, "description", term_id=term["id"], category=term["category"],
                                 sentence=stext[:400], boilerplate=in_boiler(m.start())))
    title_ai = [{"term_id": t["id"], "text": m.group(0)} for t in terms for m in [t["rx"].search(title)] if m]
    cats = lex["categories"]
    role_hits = [x for x in mentions if not x["boilerplate"]]
    headline = any(cats[x["category"]]["headline"] for x in role_hits)
    candidates = sorted({r for x in role_hits for r in cats[x["category"]]["relationship_candidates"]})
    if not mentions:
        candidates = ["AI_NONE_OBSERVED"]
    elif not role_hits:
        candidates = ["AI_ORGANISATIONAL_CONTEXT"]

    return {
        "schema": "rules_classification/1",
        "cluster_id": cluster["cluster_id"],
        "canonical_observation_id": cluster["canonical_observation_id"],
        "record_url": cluster["canonical"].get("record_url"),
        "nation": cluster["canonical"]["inferred"]["nation"]["value"],
        "first_seen": cluster["first_seen"],
        "role_family": family,
        "clinical_status": clinical,
        "task_categories": task_out,
        "ai": {
            "mentions": mentions,
            "headline_ai_role_content": headline,
            "relationship_candidates": candidates,
            "categories_in_role_text": sorted({x["category"] for x in role_hits}),
            "title_ai_terms": title_ai,
        },
        "boilerplate_sentences_excluded": len(boiler_ranges),
        "needs_genai_review": bool(role_hits) or (family["value"] in (common.UNKNOWN, "AMBIGUOUS") and bool(desc)),
        "method": "rules",
        "versions": common.version_stamp(),
    }


def run(clusters, tax, lex):
    fams, tasks, clinical_rx = compile_taxonomy(tax)
    terms = compile_lexicon(lex)
    by_emp, overall = boilerplate_counts(clusters, lex["boilerplate"]["min_sentence_chars"])
    return [classify(c, fams, tasks, clinical_rx, terms, lex, tax["rule_settings"], by_emp, overall)
            for c in clusters]


def main():
    run_id = common.make_run_id("classify_rules")
    started = common.utc_now_iso()
    tax = common.load_yaml(common.CONFIG / "taxonomy.yaml")
    lex = common.load_yaml(common.CONFIG / "ai_lexicon.yaml")
    path = common.DERIVED / "dedup" / "vacancy_clusters.jsonl"
    clusters = list(common.read_jsonl(path)) if path.exists() else []
    out = run(clusters, tax, lex)
    n = common.write_jsonl(common.DERIVED / "classified" / "rules_classification.jsonl", out)
    common.log_run(run_id, "classify_rules", started, "OK" if clusters else "OK_EMPTY", "",
                   len(clusters), n, f"taxonomy {tax['version']}, lexicon {lex['version']}")
    print(f"classified {n} clusters")


if __name__ == "__main__":
    main()
