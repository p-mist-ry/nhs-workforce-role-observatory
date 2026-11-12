"""Generic, fail-safe collector for documented recruitment feeds.

Nothing is fetched unless config/sources.yaml has every required field
for the source. Supported formats: xml, rss, atom, json. HTML scraping is
deliberately not supported: it is brittle and often outside site terms.

Output (append-only, one new file per run):
  data/observations/<source_id>/<YYYY-MM-DD>/<run_id>.jsonl

To keep the repository small, the full advert is stored only the first
time it is seen, or when its content changes. On later days a small
"sighting" record (schema raw_sighting/1) points to the stored copy.

Personal contact details (e-mails, phone numbers) are redacted before
anything is written. The SHA-256 of the unredacted record is kept so the
record's integrity can still be demonstrated.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET

import common

CANONICAL_FIELDS = [
    "source_record_id", "url", "title", "employer", "location", "postcode",
    "pay_text", "band_text", "hours_text", "contract_text", "working_pattern_text",
    "closing_date_text", "posted_date_text", "staff_group_text", "description",
]
REQUIRED_MAPPED = ["title", "url"]


class Blocked(Exception):
    """Collection refused for a safety or configuration reason."""


# ------------------------------------------------------------ configuration
def load_source(source_id: str) -> tuple[dict, dict]:
    cfg = common.load_yaml(common.CONFIG / "sources.yaml")
    for src in cfg.get("sources", []):
        if src.get("source_id") == source_id:
            return src, cfg.get("defaults", {})
    raise Blocked(f"source_id '{source_id}' not found in config/sources.yaml")


def preflight(src: dict, defaults: dict) -> list[str]:
    """Return a list of problems. Empty list means safe to collect."""
    problems = []
    if src.get("enabled") is not True:
        problems.append("enabled is not true")
    for key in ["endpoint", "documentation_url", "terms_url", "terms_reviewed_on", "format"]:
        if not src.get(key):
            problems.append(f"{key} is not filled in")
    endpoint = src.get("endpoint") or ""
    if endpoint and not endpoint.startswith("https://"):
        problems.append("endpoint must start with https://")
    if src.get("format") and src["format"] not in {"xml", "rss", "atom", "json"}:
        problems.append("format must be xml, rss, atom or json")
    if src.get("format") in {"xml", "json"} and not src.get("record_path"):
        problems.append("record_path is required for xml and json formats")
    fmap = src.get("field_map") or {}
    for f in REQUIRED_MAPPED:
        if not fmap.get(f):
            problems.append(f"field_map.{f} is not mapped")
    unknown = set(fmap) - set(CANONICAL_FIELDS)
    if unknown:
        problems.append(f"field_map has unknown canonical fields: {sorted(unknown)}")
    ua = defaults.get("user_agent", "")
    if not ua or "CHANGE_ME" in ua:
        problems.append("defaults.user_agent still contains CHANGE_ME - add a contact e-mail")
    return problems


# ------------------------------------------------------------ fetching
def robots_allows(url: str, user_agent: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception as exc:  # network failure: be conservative
        raise Blocked(f"could not read {robots_url}: {exc}") from exc
    return rp.can_fetch(user_agent, url)


def fetch(url: str, defaults: dict) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": defaults["user_agent"]})
    limit = int(defaults.get("max_response_bytes", 20_000_000))
    with urllib.request.urlopen(req, timeout=int(defaults.get("timeout_seconds", 30))) as resp:
        body = resp.read(limit + 1)
        if len(body) > limit:
            raise Blocked("response larger than max_response_bytes")
        return resp.status, body


def build_url(endpoint: str, params: dict) -> str:
    if not params:
        return endpoint
    sep = "&" if urllib.parse.urlsplit(endpoint).query else "?"
    return endpoint + sep + urllib.parse.urlencode(params, doseq=True)


# ------------------------------------------------------------ parsing
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def element_to_dict(el: ET.Element) -> dict:
    """Flatten an XML element: child tag -> text (lists if repeated)."""
    out: dict = {}
    for k, v in el.attrib.items():
        out["@" + _strip_ns(k)] = v
    for child in el:
        key = _strip_ns(child.tag)
        if len(child):
            val = element_to_dict(child)
        else:
            val = (child.text or "").strip() or None
            if child.attrib and val is None:
                val = {"@" + _strip_ns(a): b for a, b in child.attrib.items()}
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(val)
        else:
            out[key] = val
    return out


def parse_records(body: bytes, src: dict) -> list[dict]:
    fmt = src["format"]
    if fmt == "json":
        data = json.loads(body.decode("utf-8"))
        node = data
        for part in str(src["record_path"]).split("."):
            if part:
                node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, list):
            raise Blocked(f"record_path '{src['record_path']}' did not lead to a list")
        return [r for r in node if isinstance(r, dict)]
    root = ET.fromstring(body)  # expat in Python 3.12 limits entity expansion
    if fmt == "rss":
        items = root.findall("./channel/item")
    elif fmt == "atom":
        items = [e for e in root.iter() if _strip_ns(e.tag) == "entry"]
    else:
        path = str(src["record_path"])
        items = [e for e in root.iter() if _strip_ns(e.tag) == path] if "/" not in path \
            else root.findall(path)
    return [element_to_dict(i) for i in items]


def get_path(record: dict, path: str):
    node = record
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    if isinstance(node, list):
        node = " | ".join(str(x) for x in node if x is not None)
    if isinstance(node, dict):
        node = json.dumps(node, ensure_ascii=False, sort_keys=True)
    return common.clean_text(node)


def map_record(record: dict, field_map: dict) -> dict:
    return {f: (get_path(record, field_map[f]) if field_map.get(f) else None)
            for f in CANONICAL_FIELDS}


def content_index(source_id: str) -> dict:
    """{(record_key, content_sha): observation_id} for full records already stored."""
    index = {}
    for f in common.iter_files(common.OBSERVATIONS / source_id, "*.jsonl"):
        for r in common.read_jsonl(f):
            if r.get("schema") == "raw_observation/1" and r.get("record_key"):
                index[(r["record_key"], r["content_sha256_original"])] = r["observation_id"]
    return index


# ------------------------------------------------------------ main
def collect(source_id: str, dry_run: bool = False, strict: bool = False) -> int:
    script = f"collect_{source_id}"
    run_id = common.make_run_id(script)
    started = common.utc_now_iso()
    try:
        src, defaults = load_source(source_id)
        problems = preflight(src, defaults)
        if problems:
            msg = "NOT COLLECTED (safe stop): " + "; ".join(problems)
            print(msg)
            common.log_run(run_id, script, started, "BLOCKED_NOT_CONFIGURED", source_id, 0, 0, msg)
            return 2 if strict else 0
        if dry_run:
            print("Preflight passed. Dry run: nothing fetched.")
            common.log_run(run_id, script, started, "DRY_RUN_OK", source_id, 0, 0, "preflight passed")
            return 0

        pag = src.get("pagination") or {"mode": "none"}
        pages = [None]
        if pag.get("mode") == "page_param":
            if not pag.get("param"):
                raise Blocked("pagination.param is required when mode is page_param")
            start = int(pag.get("start", 1))
            pages = list(range(start, start + int(pag.get("max_pages", 1))))

        delay = float(defaults.get("min_seconds_between_requests", 3))
        retrieved = []
        for i, page in enumerate(pages):
            params = dict(src.get("params") or {})
            if page is not None:
                params[pag["param"]] = page
            url = build_url(src["endpoint"], params)
            if defaults.get("respect_robots_txt", True) and not robots_allows(url, defaults["user_agent"]):
                raise Blocked(f"robots.txt disallows {url}")
            if i:
                time.sleep(delay)
            status, body = fetch(url, defaults)
            records = parse_records(body, src)
            print(f"page {page}: HTTP {status}, {len(records)} records")
            for rec in records:
                retrieved.append((url, status, rec))
            if not records:
                break

        day = common.today_utc()
        known = content_index(source_id)
        out, n_full = [], 0
        for request_url, status, rec in retrieved:
            original_sha = common.sha256_json(rec)
            redacted, n_red = common.redact_structure(rec)
            mapped = map_record(redacted, src.get("field_map") or {})
            key = mapped.get("source_record_id") or mapped.get("url") or original_sha
            obs_id = common.sha256_text(f"{source_id}|{key}|{run_id}")[:24]
            earlier = known.get((key, original_sha))
            if earlier:
                out.append({
                    "schema": "raw_sighting/1", "observation_id": obs_id, "run_id": run_id,
                    "source_id": source_id, "retrieved_at_utc": started, "observed_date": day,
                    "request_url": request_url, "http_status": status,
                    "record_url": mapped.get("url"), "source_record_id": mapped.get("source_record_id"),
                    "content_sha256_original": original_sha, "same_content_as": earlier,
                })
                continue
            known[(key, original_sha)] = obs_id
            n_full += 1
            out.append({
                "schema": "raw_observation/1",
                "observation_id": obs_id,
                "record_key": key,
                "run_id": run_id,
                "source_id": source_id,
                "retrieved_at_utc": started,
                "observed_date": day,
                "request_url": request_url,
                "http_status": status,
                "record_url": mapped.get("url"),
                "source_record_id": mapped.get("source_record_id"),
                "content_sha256_original": original_sha,
                "redactions": n_red,
                "mapped": mapped,
                "raw": redacted,
                "versions": common.version_stamp(),
            })
        path = common.OBSERVATIONS / source_id / day / f"{run_id}.jsonl"
        n = common.write_jsonl(path, out, exclusive=True) if out else 0
        status_text = "OK" if n else "OK_EMPTY"
        common.log_run(run_id, script, started, status_text, source_id, len(retrieved), n,
                       (f"wrote observations/{source_id}/{day}/{path.name}: {n_full} new or changed, "
                        f"{n - n_full} seen again") if n else "no records returned")
        print(f"{status_text}: {n} records")
        return 0
    except Blocked as exc:
        common.log_run(run_id, script, started, "BLOCKED", source_id, 0, 0, str(exc))
        print(f"BLOCKED: {exc}")
        return 2 if strict else 0
    except (urllib.error.URLError, ET.ParseError, json.JSONDecodeError, OSError) as exc:
        common.log_run(run_id, script, started, "ERROR", source_id, 0, 0, repr(exc))
        print(f"ERROR: {exc!r}")
        return 1


def main(source_id: str) -> None:
    ap = argparse.ArgumentParser(description=f"Collect {source_id} adverts (fail-safe).")
    ap.add_argument("--dry-run", action="store_true", help="check configuration only")
    ap.add_argument("--strict", action="store_true", help="exit non-zero when blocked")
    args = ap.parse_args()
    raise SystemExit(collect(source_id, dry_run=args.dry_run, strict=args.strict))
