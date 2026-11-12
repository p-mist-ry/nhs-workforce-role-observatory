"""Quality assurance for the whole repository.

Every check writes rows to logs/qa_log.csv (append-only). The script exits
with code 1 if any check FAILS, which turns the GitHub Action red.

Checks
  config            YAML parses; enabled sources are fully documented
  providers         providers.csv columns, nations, unique ids
  log_headers       log files have the expected columns
  change_log        every current artefact version has a change_log row
  version_drift     an artefact's content changed but its version did not
  observations      raw observation schema, unique ids, no e-mail addresses
  storage           warns when stored observations grow large
  normalized        nation values, no empty strings, regex values have evidence
  classifications   evidence offsets match the text; no title-only labels
  genai             ACCEPTED GenAI records really passed span checks
  append_only       logs only grew; observation and GenAI files never edited
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess

import common

RESULTS: list[dict] = []
SIZE_WARN_MB = 500


def record(check, status, details, file="", severity=None):
    RESULTS.append({"check_name": check, "status": status, "file": file,
                    "severity": severity or ("high" if status == "FAIL" else "info"),
                    "details": str(details)[:500]})


def summarise(check, problems, ok_message, cap=20):
    if problems:
        for p in problems[:cap]:
            record(check, "FAIL", p[1], p[0])
        if len(problems) > cap:
            record(check, "FAIL", f"... and {len(problems) - cap} more problems")
    else:
        record(check, "PASS", ok_message)


# ------------------------------------------------------------------ checks
def check_config():
    problems = []
    for name in ["sources.yaml", "taxonomy.yaml", "ai_lexicon.yaml", "pipeline.yaml"]:
        try:
            common.load_yaml(common.CONFIG / name)
        except Exception as exc:
            problems.append((f"config/{name}", f"does not parse: {exc}"))
    if problems:
        return summarise("config", problems, "")
    for s in common.load_yaml(common.CONFIG / "sources.yaml")["sources"]:
        sid = s.get("source_id")
        if s.get("enabled"):
            for key in ["endpoint", "documentation_url", "terms_url", "terms_reviewed_on"]:
                if not s.get(key):
                    problems.append(("config/sources.yaml", f"{sid}: enabled but {key} is empty"))
        if s.get("endpoint") and not str(s["endpoint"]).startswith("https://"):
            problems.append(("config/sources.yaml", f"{sid}: endpoint is not https"))
        for n in s.get("nation_scope") or []:
            if n not in common.NATIONS:
                problems.append(("config/sources.yaml", f"{sid}: unknown nation {n}"))
    summarise("config", problems, "all config files parse; enabled sources documented")


def check_providers():
    path = common.CONFIG / "providers.csv"
    expected = ["provider_id", "provider_name", "aliases", "nation", "provider_type", "ods_code",
                "nhs_funded", "verification_status", "verified_source_url", "verified_on", "notes"]
    problems, seen = [], set()
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != expected:
            problems.append(("config/providers.csv", f"columns are {reader.fieldnames}"))
        for i, row in enumerate(reader, 2):
            if row["provider_id"] in seen:
                problems.append(("config/providers.csv", f"line {i}: duplicate id {row['provider_id']}"))
            seen.add(row["provider_id"])
            if row["nation"] not in common.NATIONS:
                problems.append(("config/providers.csv", f"line {i}: nation '{row['nation']}'"))
            if row["verification_status"] not in {"VERIFIED", "UNVERIFIED"}:
                problems.append(("config/providers.csv", f"line {i}: verification_status"))
            if row["verification_status"] == "VERIFIED" and not row["verified_source_url"]:
                problems.append(("config/providers.csv", f"line {i}: VERIFIED without verified_source_url"))
    summarise("providers", problems, f"{len(seen)} providers valid")


def check_log_headers():
    problems = []
    for name, header in [("runs.csv", common.RUNS_HEADER), ("qa_log.csv", common.QA_HEADER),
                         ("change_log.csv", common.CHANGE_HEADER)]:
        path = common.LOGS / name
        if not path.exists():
            problems.append((f"logs/{name}", "missing"))
            continue
        with open(path, encoding="utf-8", newline="") as fh:
            first = next(csv.reader(fh), None)
        if first != header:
            problems.append((f"logs/{name}", f"header is {first}"))
    summarise("log_headers", problems, "log headers correct")


def read_change_log():
    with open(common.LOGS / "change_log.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def check_change_log(arts):
    rows = read_change_log()
    problems = []
    ids = [r["change_id"] for r in rows]
    if len(ids) != len(set(ids)):
        problems.append(("logs/change_log.csv", "change_id values are not unique"))
    logged = {(r["artefact"], r["new_version"]) for r in rows}
    for name, (version, _h) in arts.items():
        if version == "MISSING":
            problems.append(("", f"{name} has no version field"))
        elif (name, version) not in logged:
            problems.append(("logs/change_log.csv",
                             f"no row for artefact={name} new_version={version}; add one describing the change"))
    summarise("change_log", problems, "every current artefact version is logged")


def check_version_drift(arts):
    problems = []
    path = common.LOGS / "runs.csv"
    seen = {}
    if path.exists():
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                for part in (row.get("artefact_fingerprints") or "").split("|"):
                    if "@" in part and ":" in part:
                        name, rest = part.split("@", 1)
                        version, h = rest.rsplit(":", 1)
                        seen.setdefault((name, version), set()).add(h)
    for name, (version, h) in arts.items():
        earlier = seen.get((name, version), set())
        if any(x != h for x in earlier):
            problems.append(("", f"{name} content changed but version is still {version}: "
                                 f"bump the version and add a change_log row"))
    summarise("version_drift", problems, "no unversioned changes detected")


def check_observations():
    problems, ids, n = [], set(), 0
    required = ["schema", "observation_id", "run_id", "source_id", "retrieved_at_utc",
                "observed_date", "request_url", "mapped", "raw", "content_sha256_original"]
    sighting_required = ["schema", "observation_id", "run_id", "source_id", "observed_date",
                         "request_url", "content_sha256_original", "same_content_as"]
    total_bytes = 0
    for f in common.iter_files(common.OBSERVATIONS, "*.jsonl"):
        rel = str(f.relative_to(common.ROOT))
        total_bytes += f.stat().st_size
        for rec in common.read_jsonl(f):
            n += 1
            need = sighting_required if rec.get("schema") == "raw_sighting/1" else required
            miss = [k for k in need if k not in rec]
            if miss:
                problems.append((rel, f"{rec.get('observation_id')}: missing {miss}"))
            if rec.get("observation_id") in ids:
                problems.append((rel, f"duplicate observation_id {rec.get('observation_id')}"))
            ids.add(rec.get("observation_id"))
            if not str(rec.get("request_url", "")).startswith("https://"):
                problems.append((rel, f"{rec.get('observation_id')}: request_url not https"))
            if rec.get("raw") and common.EMAIL_RE.search(json.dumps(rec.get("raw"), ensure_ascii=False)):
                problems.append((rel, f"{rec.get('observation_id')}: unredacted e-mail address"))
    summarise("observations", problems, f"{n} raw observations valid")
    mb = total_bytes / 1e6
    if mb > SIZE_WARN_MB:
        record("storage", "WARN", f"observations use {mb:.0f} MB; plan external storage (see README)",
               severity="medium")
    else:
        record("storage", "PASS", f"observations use {mb:.1f} MB")


def walk_empty_strings(obj, path="$"):
    if isinstance(obj, str) and obj.strip() == "":
        yield path
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_empty_strings(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_empty_strings(v, f"{path}[{i}]")


def check_normalized():
    problems, n = [], 0
    allowed = set(common.NATIONS) | {common.UNKNOWN}
    for f in common.iter_files(common.DERIVED / "normalized", "*.jsonl"):
        rel = str(f.relative_to(common.ROOT))
        for rec in common.read_jsonl(f):
            n += 1
            oid = rec.get("observation_id")
            if rec["inferred"]["nation"]["value"] not in allowed:
                problems.append((rel, f"{oid}: bad nation"))
            for p in walk_empty_strings(rec.get("observed")):
                problems.append((rel, f"{oid}: empty string at {p} (use null)"))
            for name, fobj in rec["observed"].items():
                if fobj["value"] is not None and str(fobj["method"]).startswith("regex") and not fobj["evidence"]:
                    problems.append((rel, f"{oid}: {name} parsed without evidence"))
    summarise("normalized", problems, f"{n} normalised records valid")


def check_classifications():
    cpath = common.DERIVED / "dedup" / "vacancy_clusters.jsonl"
    rpath = common.DERIVED / "classified" / "rules_classification.jsonl"
    if not rpath.exists():
        return record("classifications", "PASS", "no classifications yet")
    texts = {c["cluster_id"]: (c["canonical"]["observed"]["description"]["value"] or "")
             for c in common.read_jsonl(cpath)} if cpath.exists() else {}
    problems, n = [], 0
    rel = str(rpath.relative_to(common.ROOT))
    for r in common.read_jsonl(rpath):
        n += 1
        text = texts.get(r["cluster_id"])
        if text is None:
            problems.append((rel, f"{r['cluster_id']}: cluster missing"))
            continue
        spans = list(r["role_family"].get("evidence", [])) + list(r["clinical_status"].get("evidence", []))
        spans += [e for t in r["task_categories"] for e in t["evidence"]] + r["ai"]["mentions"]
        for s in spans:
            if text[s["start"]:s["end"]] != s["text"]:
                problems.append((rel, f"{r['cluster_id']}: evidence offset mismatch '{s['text'][:40]}'"))
        fam = r["role_family"]["value"]
        if fam not in (common.UNKNOWN, "AMBIGUOUS"):
            if not any(e.get("field") == "description" for e in r["role_family"]["evidence"]):
                problems.append((rel, f"{r['cluster_id']}: {fam} without description evidence (title-only)"))
    summarise("classifications", problems, f"{n} rule classifications have valid evidence")


def check_genai():
    problems, n = [], 0
    for f in common.iter_files(common.DERIVED / "genai_runs", "*.jsonl"):
        rel = str(f.relative_to(common.ROOT))
        for r in common.read_jsonl(f):
            n += 1
            if r.get("status") != "ACCEPTED":
                continue
            stages = r.get("stages") or {"change_detection": r}
            for name, st in stages.items():
                if (st.get("span_check") or {}).get("n_missing", 1) != 0:
                    problems.append((rel, f"{r.get('cluster_id') or r.get('pair_id')}: {name} ACCEPTED with unverified spans"))
                if st.get("confidence_problems"):
                    problems.append((rel, f"{r.get('cluster_id') or r.get('pair_id')}: {name} bad confidence"))
    summarise("genai", problems, f"{n} GenAI records checked")


def git(*args):
    return subprocess.run(["git", *args], cwd=common.ROOT, capture_output=True, text=True, timeout=60)


def check_append_only(base):
    if git("rev-parse", "--verify", base).returncode != 0:
        return record("append_only", "WARN", f"git base '{base}' not available; check skipped", severity="medium")
    problems = []
    for name in ["runs.csv", "qa_log.csv", "change_log.csv"]:
        rel = f"logs/{name}"
        old = git("show", f"{base}:{rel}")
        if old.returncode != 0:
            continue
        current = (common.ROOT / rel).read_text(encoding="utf-8")
        if not current.replace("\r\n", "\n").startswith(old.stdout.replace("\r\n", "\n").rstrip("\n")):
            problems.append((rel, "existing lines were edited or deleted (logs are append-only)"))
    diff = git("diff", "--name-status", base, "--", "data/observations", "data/derived/genai_runs")
    for line in diff.stdout.splitlines():
        status, _, path = line.partition("\t")
        if status and status[0] in "MDR" and not path.endswith("README.md"):
            problems.append((path, f"append-only file changed (git status {status})"))
    summarise("append_only", problems, f"append-only areas unchanged since {base}")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Validate the repository.")
    ap.add_argument("--git-base", default="HEAD", help="commit to compare append-only files against")
    ap.add_argument("--no-log", action="store_true", help="do not write to logs/qa_log.csv")
    args = ap.parse_args()
    run_id = common.make_run_id("validate")
    started = common.utc_now_iso()
    arts = common.versioned_artefacts()
    for fn in [check_config, check_providers, check_log_headers, lambda: check_change_log(arts),
               lambda: check_version_drift(arts), check_observations, check_normalized,
               check_classifications, check_genai, lambda: check_append_only(args.git_base)]:
        try:
            fn()
        except Exception as exc:  # a crashing check is itself a failure
            record(getattr(fn, "__name__", "check"), "FAIL", f"check crashed: {exc!r}")
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    for r in RESULTS:
        print(f"[{r['status']}] {r['check_name']}: {r['details']} {r['file']}")
    if not args.no_log:
        for i, r in enumerate(RESULTS, 1):
            common.append_csv(common.LOGS / "qa_log.csv", common.QA_HEADER,
                              r | {"qa_id": f"{run_id}-{i:03d}", "run_id": run_id, "checked_at_utc": started})
        common.log_run(run_id, "validate", started, "FAIL" if fails else "PASS", "",
                       len(RESULTS), len(fails), f"{len(fails)} failing checks")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
