"""Detect how advertised work is changing.

All measures are about ADVERTISED DEMAND (deduplicated vacancy clusters),
never about the workforce in post. Outputs:

  indicators.jsonl            month x nation x role family: number of
                              clusters and share with AI content in the
                              role text (not boilerplate), with 95% Wilson
                              intervals; small cells suppressed.
  change_signals.jsonl        latest complete month vs pooled baseline,
                              flagged only when intervals do not overlap.
  term_emergence.jsonl        first month each AI lexicon term appears in
                              role text, per nation.
  role_change_candidates.jsonl  successive adverts by the same employer with
                              the same normalised title: sentence-level
                              additions/removals, AI and task changes.
                              The title links candidates only; whether it is
                              really the same role is left to evidence review.
"""
from __future__ import annotations

import math
from collections import defaultdict

import common


def wilson(k, n, z=1.96):
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def month_of(date_str):
    return date_str[:7] if date_str else None


def previous_months(month, n):
    y, m = map(int, month.split("-"))
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return out


def build_indicators(rules, min_n):
    cells = defaultdict(lambda: {"n": 0, "ai": 0, "categories": defaultdict(int)})
    for r in rules:
        month = month_of(r["first_seen"])
        fam = r["role_family"]["value"]
        for nation in (r["nation"], "ALL"):
            for family in (fam, "ALL"):
                c = cells[(month, nation, family)]
                c["n"] += 1
                if r["ai"]["headline_ai_role_content"]:
                    c["ai"] += 1
                for cat in r["ai"]["categories_in_role_text"]:
                    c["categories"][cat] += 1
    out = []
    for (month, nation, family), c in sorted(cells.items()):
        small = c["n"] < min_n
        lo, hi = wilson(c["ai"], c["n"])
        out.append({
            "schema": "ai_indicator/1", "month": month, "nation": nation, "role_family": family,
            "n_vacancy_clusters": c["n"], "n_ai_role_content": None if small else c["ai"],
            "share_ai_role_content": None if small else round(c["ai"] / c["n"], 4),
            "ci95_low": None if small else lo, "ci95_high": None if small else hi,
            "ai_categories": None if small else dict(c["categories"]),
            "suppressed_small_n": small, "measure": "advertised demand, not workforce stock",
        })
    return out


def build_signals(rules, min_n, baseline_months, current_month):
    series = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rules:
        month = month_of(r["first_seen"])
        for nation in (r["nation"], "ALL"):
            for fam in (r["role_family"]["value"], "ALL"):
                cell = series[(nation, fam)][month]
                cell[0] += 1
                cell[1] += int(r["ai"]["headline_ai_role_content"])
    out = []
    for (nation, fam), months in series.items():
        complete = sorted(m for m in months if m < current_month)
        if not complete:
            continue
        latest = complete[-1]
        n, k = months[latest]
        base = [months[m] for m in previous_months(latest, baseline_months) if m in months]
        bn, bk = sum(b[0] for b in base), sum(b[1] for b in base)
        if n < min_n or bn < min_n:
            continue
        lo, hi = wilson(k, n)
        blo, bhi = wilson(bk, bn)
        direction = "UP" if lo > bhi else "DOWN" if hi < blo else None
        if direction:
            out.append({
                "schema": "change_signal/1", "nation": nation, "role_family": fam,
                "month": latest, "share": round(k / n, 4), "ci95": [lo, hi], "n": n,
                "baseline_months": baseline_months, "baseline_share": round(bk / bn, 4),
                "baseline_ci95": [blo, bhi], "baseline_n": bn, "direction": direction,
                "indicator": "share of vacancy clusters with AI content in role text",
            })
    return out


def build_emergence(rules):
    first = {}
    for r in sorted(rules, key=lambda r: r["first_seen"]):
        for m in r["ai"]["mentions"]:
            if m["boilerplate"]:
                continue
            for nation in (r["nation"], "ALL"):
                key = (m["term_id"], nation)
                if key not in first:
                    first[key] = {"schema": "term_emergence/1", "term_id": m["term_id"],
                                  "category": m["category"], "nation": nation,
                                  "first_month": month_of(r["first_seen"]),
                                  "first_cluster_id": r["cluster_id"], "record_url": r["record_url"],
                                  "evidence": m["text"], "sentence": m["sentence"]}
    return sorted(first.values(), key=lambda e: (e["first_month"], e["term_id"], e["nation"]))


def build_pairs(rules, clusters):
    groups = defaultdict(list)
    for r in rules:
        c = clusters[r["cluster_id"]]
        obs = c["canonical"]["observed"]
        key = (common.norm_key(obs["employer"]["value"]), common.norm_key(obs["title"]["value"]), r["nation"])
        if key[0] and key[1]:
            groups[key].append((c, r))
    out = []
    for (emp, title, nation), items in groups.items():
        items.sort(key=lambda x: x[0]["first_seen"])
        for (ca, ra), (cb, rb) in zip(items, items[1:]):
            if cb["first_seen"] <= ca["last_seen"]:
                continue
            ta = ca["canonical"]["observed"]["description"]["value"] or ""
            tb = cb["canonical"]["observed"]["description"]["value"] or ""
            sa = {common.norm_key(s): s for _x, _y, s in common.split_sentences(ta)}
            sb = {common.norm_key(s): s for _x, _y, s in common.split_sentences(tb)}
            added = [sb[k] for k in sb if k not in sa]
            removed = [sa[k] for k in sa if k not in sb]
            ai_sent_b = {common.norm_key(m["sentence"]) for m in rb["ai"]["mentions"] if not m["boilerplate"]}
            ai_sent_a = {common.norm_key(m["sentence"]) for m in ra["ai"]["mentions"] if not m["boilerplate"]}
            tasks_a = {t["task_category"] for t in ra["task_categories"]}
            tasks_b = {t["task_category"] for t in rb["task_categories"]}
            union = len(set(sa) | set(sb)) or 1
            out.append({
                "schema": "role_change_candidate/1",
                "pair_id": "rp_" + common.sha256_text(ca["cluster_id"] + cb["cluster_id"])[:16],
                "cluster_a": ca["cluster_id"], "cluster_b": cb["cluster_id"],
                "url_a": ca["canonical"].get("record_url"), "url_b": cb["canonical"].get("record_url"),
                "first_seen_a": ca["first_seen"], "first_seen_b": cb["first_seen"],
                "nation": nation, "employer": ca["canonical"]["observed"]["employer"]["value"],
                "title_a": ca["canonical"]["observed"]["title"]["value"],
                "title_b": cb["canonical"]["observed"]["title"]["value"],
                "link_basis": "same employer and normalised title (linking only, not classification)",
                "sentence_overlap": round(len(set(sa) & set(sb)) / union, 3),
                "n_sentences_added": len(added), "n_sentences_removed": len(removed),
                "ai_sentences_added": [sb[k] for k in sb if k in ai_sent_b and k not in sa][:5],
                "ai_sentences_removed": [sa[k] for k in sa if k in ai_sent_a and k not in sb][:5],
                "ai_role_content_a": ra["ai"]["headline_ai_role_content"],
                "ai_role_content_b": rb["ai"]["headline_ai_role_content"],
                "task_categories_added": sorted(tasks_b - tasks_a),
                "task_categories_removed": sorted(tasks_a - tasks_b),
                "content_changed": bool(added or removed),
                "needs_evidence_review": True,
            })
    return out


def main():
    run_id = common.make_run_id("detect_changes")
    started = common.utc_now_iso()
    cfg = common.load_yaml(common.CONFIG / "pipeline.yaml")["change_detection"]
    rpath = common.DERIVED / "classified" / "rules_classification.jsonl"
    cpath = common.DERIVED / "dedup" / "vacancy_clusters.jsonl"
    rules = list(common.read_jsonl(rpath)) if rpath.exists() else []
    clusters = {c["cluster_id"]: c for c in common.read_jsonl(cpath)} if cpath.exists() else {}
    min_n = int(cfg["min_adverts_per_cell"])
    out_dir = common.DERIVED / "changes"
    current_month = common.today_utc()[:7]
    n1 = common.write_jsonl(out_dir / "indicators.jsonl", build_indicators(rules, min_n))
    n2 = common.write_jsonl(out_dir / "change_signals.jsonl",
                            build_signals(rules, min_n, int(cfg["baseline_months"]), current_month))
    n3 = common.write_jsonl(out_dir / "term_emergence.jsonl", build_emergence(rules))
    n4 = common.write_jsonl(out_dir / "role_change_candidates.jsonl", build_pairs(rules, clusters))
    msg = f"indicators={n1} signals={n2} emergence={n3} pairs={n4}"
    common.log_run(run_id, "detect_changes", started, "OK" if rules else "OK_EMPTY", "", len(rules), n1, msg)
    print(msg)


if __name__ == "__main__":
    main()
