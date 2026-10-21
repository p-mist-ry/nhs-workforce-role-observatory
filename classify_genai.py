"""Optional GenAI interpretation of tasks, skills, AI relationships and change.

OFF by default. It runs only when config/pipeline.yaml has
genai.enabled: true, genai.model filled in from the official model list,
and the GitHub secret ANTHROPIC_API_KEY exists. Otherwise it logs
BLOCKED and exits without calling anything.

Every output is checked deterministically:
  * every evidence quote must appear verbatim in the advert text;
  * every label must exist in config/taxonomy.yaml;
  * confidence must be a number between 0 and 1.
Outputs failing a check are kept (for audit) but marked REJECTED.

Output (append-only): data/derived/genai_runs/<date>/<run_id>.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request

import common

STAGE_PROMPTS = {"extraction": "extraction.md", "classification": "classification.md",
                 "verification": "verification.md", "change_detection": "change_detection.md"}
# Labels that state absence or uncertainty and therefore need no quote
NO_EVIDENCE_LABELS = {"UNKNOWN", "AI_NONE_OBSERVED", "NO_CHANGE_EVIDENCED"}


class Blocked(Exception):
    pass


# ------------------------------------------------------------------ prompts
def load_prompt(stage):
    path = common.PROMPTS / STAGE_PROMPTS[stage]
    text = path.read_text(encoding="utf-8")
    meta = common.prompt_metadata(path)
    body = common.PROMPT_HEADER_RE.sub("", text.lstrip("\ufeff"), count=1).strip()
    return body, {"prompt_id": meta.get("prompt_id"), "version": meta.get("version"),
                  "sha256": common.sha256_text(common.normalise_for_hash(text))[:12]}


def render(template, values):
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1))
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if leftover:
        raise Blocked(f"prompt placeholders not filled: {leftover}")
    return out


# ------------------------------------------------------------------ API
def call_model(cfg, prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    body = json.dumps({
        "model": cfg["model"], "max_tokens": int(cfg["max_output_tokens"]),
        "temperature": cfg.get("temperature", 0),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(cfg["api_url"], data=body, method="POST", headers={
        "content-type": "application/json", "x-api-key": key,
        "anthropic-version": cfg["anthropic_version_header"]})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_json(text):
    clean = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(clean)


# ------------------------------------------------------------------ deterministic checks
def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def collect_quotes(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "quote" and isinstance(v, str):
                yield path, v
            else:
                yield from collect_quotes(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from collect_quotes(v, f"{path}[{i}]")


def check_spans(output, *texts):
    hay = [squash(t) for t in texts if t]
    missing, n = [], 0
    for path, quote in collect_quotes(output):
        n += 1
        if not quote.strip() or not any(squash(quote) in h for h in hay):
            missing.append({"path": path, "quote": quote[:200]})
    return {"n_quotes": n, "n_missing": len(missing), "missing": missing}


def check_confidences(obj, path="$"):
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "confidence" and not (isinstance(v, (int, float)) and 0 <= v <= 1):
                bad.append(f"{path}.{k}={v!r}")
            else:
                bad.extend(check_confidences(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad.extend(check_confidences(v, f"{path}[{i}]"))
    return bad


def label_sets(tax):
    return {
        "role_family": {f["id"] for f in tax["role_families"]} | {"UNKNOWN"},
        "clinical_status": {"CLINICAL", "NON_CLINICAL", "UNKNOWN"},
        "task_categories": {t["id"] for t in tax["task_categories"]},
        "ai_relationships": {a["id"] for a in tax["ai_relationships"]},
        "work_change_mechanisms": {m["id"] for m in tax["work_change_mechanisms"]},
    }


def check_labels(classification, sets):
    bad = []
    for key, allowed in sets.items():
        node = classification.get(key)
        items = node if isinstance(node, list) else [node] if node else []
        for it in items:
            if not isinstance(it, dict) or it.get("label") not in allowed:
                bad.append(f"{key}: {it.get('label') if isinstance(it, dict) else it!r}")
            elif it.get("label") not in NO_EVIDENCE_LABELS and not it.get("evidence"):
                bad.append(f"{key}: {it.get('label')} has no evidence")
    return bad


# ------------------------------------------------------------------ runner
def advert_meta(cluster):
    o = cluster["canonical"]["observed"]
    return {"title": o["title"]["value"], "employer": o["employer"]["value"],
            "nation": cluster["canonical"]["inferred"]["nation"]["value"],
            "first_seen": cluster["first_seen"], "url": cluster["canonical"].get("record_url")}


def process_cluster(cfg, tax, cluster):
    text = cluster["canonical"]["observed"]["description"]["value"] or ""
    meta = advert_meta(cluster)
    rec = {"schema": "genai_classification/1", "cluster_id": cluster["cluster_id"],
           "record_url": meta["url"], "model": cfg["model"], "stages": {}, "prompts": {}}
    labels = {k: sorted(v) for k, v in label_sets(tax).items()}
    values = {"ADVERT_META": meta, "ADVERT_TEXT": text, "TAXONOMY_LABELS": labels}
    status = "ACCEPTED"
    for stage in cfg["stages"]:
        if stage == "change_detection":
            continue
        tmpl, pmeta = load_prompt(stage)
        rec["prompts"][stage] = pmeta
        raw = call_model(cfg, render(tmpl, values))
        try:
            parsed = parse_json(raw)
        except json.JSONDecodeError:
            rec["stages"][stage] = {"raw_response": raw, "parsed": None}
            status = "REJECTED_INVALID_JSON"
            break
        spans = check_spans(parsed, text)
        conf = check_confidences(parsed)
        rec["stages"][stage] = {"raw_response": raw, "parsed": parsed, "span_check": spans,
                                "confidence_problems": conf}
        if stage == "extraction":
            values["EXTRACTION_JSON"] = parsed
        if stage == "classification":
            values["CLASSIFICATION_JSON"] = parsed
            lab = check_labels(parsed, label_sets(tax))
            rec["stages"][stage]["label_problems"] = lab
            if lab:
                status = "REJECTED_LABELS"
        if spans["n_missing"]:
            status = "REJECTED_UNVERIFIABLE_SPANS"
        if conf:
            status = "REJECTED_CONFIDENCE"
        if stage == "verification" and parsed.get("overall_verdict") == "REJECT":
            status = "VERIFIER_REJECTED"
        if status != "ACCEPTED":
            break
    rec["status"] = status
    return rec


def process_change_pair(cfg, tax, pair, clusters):
    a, b = clusters.get(pair["cluster_a"]), clusters.get(pair["cluster_b"])
    if not a or not b:
        return None
    ta = a["canonical"]["observed"]["description"]["value"] or ""
    tb = b["canonical"]["observed"]["description"]["value"] or ""
    tmpl, pmeta = load_prompt("change_detection")
    prompt = render(tmpl, {"ADVERT_A_META": advert_meta(a), "ADVERT_A_TEXT": ta,
                           "ADVERT_B_META": advert_meta(b), "ADVERT_B_TEXT": tb,
                           "MECHANISM_LABELS": [m["id"] for m in tax["work_change_mechanisms"]]})
    raw = call_model(cfg, prompt)
    rec = {"schema": "genai_change/1", "pair_id": pair["pair_id"], "cluster_a": pair["cluster_a"],
           "cluster_b": pair["cluster_b"], "model": cfg["model"], "prompts": {"change_detection": pmeta},
           "raw_response": raw}
    try:
        parsed = parse_json(raw)
    except json.JSONDecodeError:
        return rec | {"parsed": None, "status": "REJECTED_INVALID_JSON"}
    spans = check_spans(parsed, ta, tb)
    conf = check_confidences(parsed)
    status = "ACCEPTED" if not spans["n_missing"] and not conf else "REJECTED_UNVERIFIABLE_SPANS"
    return rec | {"parsed": parsed, "span_check": spans, "confidence_problems": conf, "status": status}


def already_done(kind):
    done = set()
    for f in common.iter_files(common.DERIVED / "genai_runs", "*.jsonl"):
        for r in common.read_jsonl(f):
            if r.get("schema") == kind:
                done.add(r.get("cluster_id") or r.get("pair_id"))
    return done


def main():
    ap = argparse.ArgumentParser(description="Optional GenAI layer (fail-safe).")
    ap.add_argument("--mode", choices=["classify", "change"], default="classify")
    args = ap.parse_args()
    script = f"classify_genai_{args.mode}"
    run_id = common.make_run_id(script)
    started = common.utc_now_iso()
    cfg = common.load_yaml(common.CONFIG / "pipeline.yaml")["genai"]
    tax = common.load_yaml(common.CONFIG / "taxonomy.yaml")
    try:
        problems = []
        if cfg.get("enabled") is not True:
            problems.append("genai.enabled is not true")
        if not cfg.get("model"):
            problems.append("genai.model is not filled in")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY secret is not available")
        if problems:
            raise Blocked("; ".join(problems))
        cpath = common.DERIVED / "dedup" / "vacancy_clusters.jsonl"
        clusters = {c["cluster_id"]: c for c in common.read_jsonl(cpath)} if cpath.exists() else {}
        cap = int(cfg["max_items_per_run"])
        out = []
        if args.mode == "classify":
            rpath = common.DERIVED / "classified" / "rules_classification.jsonl"
            todo = [r for r in common.read_jsonl(rpath) if r["needs_genai_review"]] if rpath.exists() else []
            done = already_done("genai_classification/1")
            for r in [r for r in todo if r["cluster_id"] not in done][:cap]:
                out.append(process_cluster(cfg, tax, clusters[r["cluster_id"]]))
        else:
            ppath = common.DERIVED / "changes" / "role_change_candidates.jsonl"
            pairs = list(common.read_jsonl(ppath)) if ppath.exists() else []
            done = already_done("genai_change/1")
            for p in [p for p in pairs if p["pair_id"] not in done][:cap]:
                rec = process_change_pair(cfg, tax, p, clusters)
                if rec:
                    out.append(rec)
        for r in out:
            r["run_id"] = run_id
            r["created_at_utc"] = started
            r["versions"] = common.version_stamp()
        n = 0
        if out:
            path = common.DERIVED / "genai_runs" / common.today_utc() / f"{run_id}.jsonl"
            n = common.write_jsonl(path, out, exclusive=True)
        accepted = sum(1 for r in out if r["status"] == "ACCEPTED")
        common.log_run(run_id, script, started, "OK", "", len(out), n, f"{accepted}/{n} accepted")
        print(f"{n} GenAI records written, {accepted} accepted")
    except Blocked as exc:
        common.log_run(run_id, script, started, "BLOCKED", "", 0, 0, str(exc))
        print(f"BLOCKED (nothing sent): {exc}")
    except (urllib.error.URLError, KeyError, OSError) as exc:
        common.log_run(run_id, script, started, "ERROR", "", 0, 0, repr(exc))
        print(f"ERROR: {exc!r}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
