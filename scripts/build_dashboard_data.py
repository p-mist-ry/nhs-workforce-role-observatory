"""Build the small JSON file the public dashboard reads.

GitHub Pages can only serve files inside /docs, so the output is written
twice: data/dashboard/summary.json (the record) and docs/data/summary.json
(the copy the website loads).
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict

import common


def load(path):
    return list(common.read_jsonl(path)) if path.exists() else []


def last_rows(csv_path, key):
    latest = {}
    if not csv_path.exists():
        return latest
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            latest[row.get(key)] = row
    return latest


def main():
    run_id = common.make_run_id("build_dashboard_data")
    started = common.utc_now_iso()
    d = common.DERIVED
    clusters = load(d / "dedup" / "vacancy_clusters.jsonl")
    rules = load(d / "classified" / "rules_classification.jsonl")
    indicators = load(d / "changes" / "indicators.jsonl")
    signals = load(d / "changes" / "change_signals.jsonl")
    emergence = load(d / "changes" / "term_emergence.jsonl")
    pairs = load(d / "changes" / "role_change_candidates.jsonl")
    genai = [r for f in common.iter_files(d / "genai_runs", "*.jsonl") for r in common.read_jsonl(f)]
    n_obs = sum(1 for f in common.iter_files(d / "normalized", "*.jsonl") for _ in common.read_jsonl(f))
    min_n = int(common.load_yaml(common.CONFIG / "pipeline.yaml")["change_detection"]["min_adverts_per_cell"])

    src_cfg = common.load_yaml(common.CONFIG / "sources.yaml")["sources"]
    runs_by_source = last_rows(common.LOGS / "runs.csv", "source_id")
    sources = []
    for s in src_cfg:
        last = runs_by_source.get(s["source_id"], {})
        sources.append({
            "source_id": s["source_id"], "name": s["name"], "role": s["role"],
            "nation_scope": s.get("nation_scope"), "enabled": bool(s.get("enabled")),
            "endpoint_configured": bool(s.get("endpoint")),
            "last_status": last.get("status") or None, "last_run_utc": last.get("finished_at_utc") or None,
            "last_message": last.get("message") or None,
        })

    by_nation = Counter(c["canonical"]["inferred"]["nation"]["value"] for c in clusters)
    fam = Counter(r["role_family"]["value"] for r in rules)
    rel = Counter(x for r in rules for x in r["ai"]["relationship_candidates"])
    ai_series = defaultdict(list)
    for i in indicators:
        if i["role_family"] == "ALL":
            ai_series[i["nation"]].append({k: i[k] for k in [
                "month", "n_vacancy_clusters", "share_ai_role_content", "ci95_low", "ci95_high",
                "suppressed_small_n"]})
    ai_by_nation = {}
    for nation in common.NATIONS + [common.UNKNOWN]:
        sub = [r for r in rules if r["nation"] == nation]
        k = sum(r["ai"]["headline_ai_role_content"] for r in sub)
        small = len(sub) < min_n
        ai_by_nation[nation] = {"n": len(sub), "n_ai": None if small else k,
                                "share": None if small or not sub else round(k / len(sub), 4),
                                "suppressed_small_n": small}
    interesting = [p for p in pairs if p["ai_sentences_added"] or p["ai_sentences_removed"]
                   or p["task_categories_added"] or p["task_categories_removed"]]
    interesting.sort(key=lambda p: p["first_seen_b"], reverse=True)

    qa_rows = []
    qa_path = common.LOGS / "qa_log.csv"
    if qa_path.exists():
        with open(qa_path, encoding="utf-8", newline="") as fh:
            qa_rows = list(csv.DictReader(fh))
    last_qa_run = qa_rows[-1]["run_id"] if qa_rows else None
    last_qa = [r for r in qa_rows if r["run_id"] == last_qa_run]
    n_rules = len(rules) or 1
    summary = {
        "generated_at_utc": started,
        "versions": common.version_stamp(),
        "caveat": ("Counts are deduplicated job adverts (advertised demand). They are not "
                   "workforce numbers, not posts filled, and not vacancy rates."),
        "sources": sources,
        "totals": {
            "observations": n_obs,
            "vacancy_clusters": len(clusters),
            "cross_posted_clusters": sum(1 for c in clusters if c["n_sources"] > 1),
            "clusters_by_nation": dict(by_nation),
        },
        "role_families": dict(fam.most_common()),
        "ai": {
            "min_cell_size": min_n,
            "by_nation_all_time": ai_by_nation,
            "monthly_series": ai_series,
            "relationship_candidates_rules": dict(rel.most_common()),
            "change_signals": signals,
            "term_emergence": emergence[:100],
            "role_change_examples": interesting[:50],
        },
        "genai": {"records": len(genai), "by_status": dict(Counter(r.get("status") for r in genai))},
        "quality": {
            "share_nation_unknown": round(by_nation.get(common.UNKNOWN, 0) / (len(clusters) or 1), 4),
            "share_role_family_unknown_or_ambiguous":
                round((fam.get(common.UNKNOWN, 0) + fam.get("AMBIGUOUS", 0)) / n_rules, 4),
            "title_only_refusals": sum(1 for r in rules
                                       if r["role_family"]["reason"] == "TITLE_ONLY_EVIDENCE_INSUFFICIENT"),
            "last_qa_run_id": last_qa_run,
            "last_qa_failures": [r for r in last_qa if r["status"] == "FAIL"],
            "last_qa_checks": len(last_qa),
        },
    }
    common.write_json(common.DASHBOARD / "summary.json", summary)
    common.write_json(common.DOCS_DATA / "summary.json", summary)
    common.log_run(run_id, "build_dashboard_data", started, "OK", "", len(rules), 1,
                   "wrote data/dashboard/summary.json and docs/data/summary.json")
    print("dashboard data written")


if __name__ == "__main__":
    main()
