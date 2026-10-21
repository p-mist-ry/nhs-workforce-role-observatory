"""Shared helpers for the NHS Workforce Role Change Observatory.

Every script imports this module. It provides:
  * repository paths
  * the pipeline (code) version and artefact fingerprints for audit
  * append-only CSV logging
  * JSON-lines reading and writing
  * deterministic redaction of personal contact details

IMPORTANT: any edit to any file in /scripts must bump PIPELINE_VERSION
and add a row to logs/change_log.csv with artefact = code.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PIPELINE_VERSION = "0.1.0"

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
DATA = ROOT / "data"
OBSERVATIONS = DATA / "observations"
DERIVED = DATA / "derived"
DASHBOARD = DATA / "dashboard"
LOGS = ROOT / "logs"
PROMPTS = ROOT / "prompts"
DOCS_DATA = ROOT / "docs" / "data"

NATIONS = ["England", "Scotland", "Wales", "Northern Ireland"]
UNKNOWN = "UNKNOWN"

RUNS_HEADER = ["run_id", "started_at_utc", "finished_at_utc", "script", "source_id",
               "status", "records_in", "records_out", "message", "pipeline_version",
               "git_sha", "artefact_fingerprints"]
QA_HEADER = ["qa_id", "run_id", "checked_at_utc", "check_name", "status", "severity",
             "file", "details"]
CHANGE_HEADER = ["change_id", "date_utc", "artefact", "old_version", "new_version",
                 "author", "description", "rationale"]


# ---------------------------------------------------------------- time / ids
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def today_utc() -> str:
    return utc_now().date().isoformat()


def make_run_id(script: str) -> str:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    gh = os.environ.get("GITHUB_RUN_ID", "local")
    return f"{stamp}_{gh}_{script}"


def git_sha() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- hashing
def normalise_for_hash(text: str) -> str:
    """Ignore line-ending and trailing-whitespace differences (browser copy/paste)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------- config
def load_yaml(path: Path):
    try:
        import yaml  # PyYAML is the only third-party dependency
    except ImportError:
        sys.exit("PyYAML is not installed. The workflow installs it from requirements.txt.")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_providers() -> list[dict]:
    path = CONFIG / "providers.csv"
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- versions
PROMPT_HEADER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def prompt_metadata(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta = {}
    m = PROMPT_HEADER_RE.match(text.lstrip("\ufeff"))
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta


def versioned_artefacts() -> dict:
    """Return {artefact_name: (version, sha256_12)} for everything rule 10 covers."""
    out = {}
    yaml_files = {"taxonomy": "taxonomy.yaml", "ai_lexicon": "ai_lexicon.yaml",
                  "sources": "sources.yaml", "pipeline_config": "pipeline.yaml"}
    for name, fname in yaml_files.items():
        path = CONFIG / fname
        if path.exists():
            text = path.read_text(encoding="utf-8")
            data = load_yaml(path) or {}
            out[name] = (str(data.get("version", "MISSING")),
                         sha256_text(normalise_for_hash(text))[:12])
    for path in sorted(PROMPTS.glob("*.md")):
        meta = prompt_metadata(path)
        name = "prompt_" + meta.get("prompt_id", path.stem)
        out[name] = (meta.get("version", "MISSING"),
                     sha256_text(normalise_for_hash(path.read_text(encoding="utf-8")))[:12])
    code_text = "".join(normalise_for_hash(p.read_text(encoding="utf-8"))
                        for p in sorted((ROOT / "scripts").glob("*.py")))
    out["code"] = (PIPELINE_VERSION, sha256_text(code_text)[:12])
    return out


def fingerprint_string() -> str:
    return "|".join(f"{k}@{v}:{h}" for k, (v, h) in sorted(versioned_artefacts().items()))


def version_stamp() -> dict:
    arts = versioned_artefacts()
    return {k: v for k, (v, _h) in arts.items()} | {"git_sha": git_sha()}


# ---------------------------------------------------------------- append-only CSV
def append_csv(path: Path, header: list[str], row: dict) -> None:
    """Append one row. Never rewrites existing content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    if not new_file:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_newline = fh.read(1) != b"\n"
    else:
        needs_newline = False
    with open(path, "a", encoding="utf-8", newline="") as fh:
        if needs_newline:
            fh.write("\n")
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in header})


def log_run(run_id: str, script: str, started: str, status: str, source_id: str = "",
            records_in=None, records_out=None, message: str = "") -> None:
    append_csv(LOGS / "runs.csv", RUNS_HEADER, {
        "run_id": run_id, "started_at_utc": started, "finished_at_utc": utc_now_iso(),
        "script": script, "source_id": source_id, "status": status,
        "records_in": records_in, "records_out": records_out,
        "message": message[:500], "pipeline_version": PIPELINE_VERSION,
        "git_sha": git_sha(), "artefact_fingerprints": fingerprint_string(),
    })


# ---------------------------------------------------------------- JSON lines
def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no}: invalid JSON ({exc})") from exc


def write_jsonl(path: Path, records, exclusive: bool = False) -> int:
    """Write records. exclusive=True refuses to overwrite (append-only areas)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    n = 0
    with open(path, mode, encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
            n += 1
    return n


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------- text helpers
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# UK phone numbers: 01/02/03/07/08 numbers and +44 forms, with common separators
PHONE_RE = re.compile(r"(?<![\d£])(?:\+44\s?\(?0?\)?\s?|\(?0)\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")


def redact_contacts(text):
    """Remove e-mail addresses and phone numbers. Returns (text, n_redactions)."""
    if not isinstance(text, str):
        return text, 0
    text, n1 = EMAIL_RE.subn("[REDACTED_EMAIL]", text)
    text, n2 = PHONE_RE.subn("[REDACTED_PHONE]", text)
    return text, n1 + n2


def redact_structure(obj):
    """Redact every string inside a nested dict/list. Returns (obj, n)."""
    total = 0
    if isinstance(obj, str):
        return redact_contacts(obj)
    if isinstance(obj, list):
        out = []
        for item in obj:
            new, n = redact_structure(item)
            out.append(new)
            total += n
        return out, total
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            new, n = redact_structure(v)
            out[k] = new
            total += n
        return out, total
    return obj, 0


def clean_text(value):
    """Turn empty or whitespace-only strings into None (never invent values)."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = re.sub(r"[ \t\u00a0]+", " ", value).strip()
    return value or None


def norm_key(text) -> str:
    """Lower-case, '&' -> 'and', punctuation removed. For matching only."""
    if not text:
        return ""
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+|\s+[\u2022\u25cf\u25aa\-\*]\s+")


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """Split text into (start, end, sentence) using offsets into the original."""
    if not text:
        return []
    out, pos = [], 0
    for m in SENTENCE_SPLIT_RE.finditer(text):
        seg = text[pos:m.start()]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            out.append((pos + lead, m.start() - (len(seg) - len(seg.rstrip())), seg.strip()))
        pos = m.end()
    seg = text[pos:]
    if seg.strip():
        lead = len(seg) - len(seg.lstrip())
        out.append((pos + lead, len(text) - (len(seg) - len(seg.rstrip())), seg.strip()))
    return out


def iter_files(base: Path, pattern: str):
    if not base.exists():
        return []
    return sorted(p for p in base.rglob(pattern) if p.is_file())
