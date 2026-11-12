"""Normalise raw observations into a common vacancy schema.

Deterministic only: no GenAI. Every value is either
  * observed  - copied from a source field, or parsed by a regular
                expression from source text, with the exact evidence span; or
  * inferred  - a judgement (nation, provider match) with method and confidence.
Missing values are null. Nothing is guessed.

Input : data/observations/<source>/<date>/*.jsonl
Output: data/derived/normalized/<source>/<date>.jsonl
        Rebuilt from all observations on every run (not committed to git).
        A "sighting" (advert seen again, unchanged) reuses the stored
        advert's content and records which observation it came from.
"""
from __future__ import annotations

import datetime as dt
import re

import common

SCHEMA = "normalized_vacancy/1"

# ------------------------------------------------------------------ helpers
def field(value, method, source_field=None, evidence=None):
    return {"value": value, "method": method, "source_field": source_field, "evidence": evidence}


def missing(source_field=None):
    return field(None, "not_found", source_field, None)


# ------------------------------------------------------------------ pay
AMOUNT_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(\s?[kK]\b)?")
PERIOD_PATTERNS = {
    "hour": re.compile(r"\b(per|an|a|/)\s*(hour|hr)\b|\bhourly\b|\bph\b", re.I),
    "year": re.compile(r"\b(per|a|an|/)\s*(annum|year|yr)\b|\bp\.?\s?a\.?\b|\bannual(ly)?\b|\bper annum\b", re.I),
    "day": re.compile(r"\b(per|a|/)\s*day\b|\bdaily\b", re.I),
    "session": re.compile(r"\b(per|a|/)\s*session\b", re.I),
}


def _amount(m: re.Match) -> float:
    value = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
    if m.group(3):
        value *= 1000
    return value


def parse_pay(text):
    """Return (value_dict or None, evidence span or None)."""
    if not text:
        return None, None
    amounts = list(AMOUNT_RE.finditer(text))
    if not amounts:
        return None, None
    first = amounts[:2]
    values = [_amount(m) for m in first]
    periods = {p for p, rx in PERIOD_PATTERNS.items() if rx.search(text)}
    period = periods.pop() if len(periods) == 1 else None  # ambiguous -> null
    start = first[0].start()
    end = first[-1].end()
    for rx in PERIOD_PATTERNS.values():  # extend evidence to include the period words
        m = rx.search(text, end)
        if m and m.start() - end < 40:
            end = max(end, m.end())
    value = {
        "min": min(values), "max": max(values), "currency": "GBP",
        "period": period,
        "pro_rata": bool(re.search(r"pro[\s-]?rata", text, re.I)),
        "amounts_found": len(amounts),
    }
    return value, text[start:end]


# ------------------------------------------------------------------ band
BAND_TOKEN = r"(?:8\s*[A-Da-d]|9|[1-8])"
BAND_RE = re.compile(rf"\bBand\s*({BAND_TOKEN}(?:\s*(?:/|-|to|or|&|and)\s*(?:Band\s*)?{BAND_TOKEN})*)\b", re.I)


def parse_band(text):
    if not text:
        return None, None
    m = BAND_RE.search(text)
    if not m:
        return None, None
    tokens = re.findall(BAND_TOKEN, m.group(1))
    bands = []
    for t in tokens:
        t = re.sub(r"\s+", "", t).upper()
        if t not in bands:
            bands.append(t)
    return bands, m.group(0)


# ------------------------------------------------------------------ hours
HOURS_DESC_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:hours|hrs)\s*(?:per|a|each|/)\s*week", re.I)
NUM_RE = re.compile(r"\d{1,2}(?:\.\d{1,2})?")


def parse_hours(text, from_hours_field: bool):
    """From a dedicated hours field any number 1-60 is accepted.
    From free text the phrase must say 'per week'."""
    if not text:
        return None, None
    if from_hours_field:
        nums = [(m, float(m.group(0))) for m in NUM_RE.finditer(text)]
        nums = [(m, v) for m, v in nums if 0 < v <= 60]
        if not nums:
            return None, None  # e.g. "Various" -> null, never guessed
        vals = [v for _, v in nums[:2]]
        span = text[nums[0][0].start():nums[min(1, len(nums) - 1)][0].end()]
        return {"min": min(vals), "max": max(vals)}, span
    m = HOURS_DESC_RE.search(text)
    if not m:
        return None, None
    v = float(m.group(1))
    if not 0 < v <= 60:
        return None, None
    return {"min": v, "max": v}, m.group(0)


# ------------------------------------------------------------------ dates
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
DATE_PATTERNS = [
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")),
    ("uk_numeric", re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")),
    ("uk_words", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")),
]


def parse_date(text):
    """UK conventions only (day before month). Returns (ISO date, span)."""
    if not text:
        return None, None
    found = []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "iso":
                    d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "uk_numeric":
                    d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                else:
                    month = MONTHS.get(m.group(2).lower())
                    if not month:
                        continue
                    d = dt.date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                continue
            if 2000 <= d.year <= 2100:
                found.append((m.start(), d.isoformat(), m.group(0)))
    if not found:
        return None, None
    found.sort()
    return found[0][1], found[0][2]


# ------------------------------------------------------------------ contract / pattern
CONTRACT_TERMS = [
    ("permanent", r"\bpermanent\b"), ("fixed_term", r"fixed[\s-]?term"),
    ("bank", r"\bbank\b"), ("secondment", r"secondment"), ("locum", r"\blocum\b"),
    ("apprenticeship", r"apprentice"), ("training", r"\btraining (post|contract)\b"),
    ("waiting_list", r"waiting list"),
]
PATTERN_TERMS = [
    ("full_time", r"full[\s-]?time"), ("part_time", r"part[\s-]?time"),
    ("flexible", r"flexible"), ("job_share", r"job[\s-]?share"),
    ("compressed_hours", r"compressed"), ("term_time", r"term[\s-]?time"),
]


def parse_terms(text, terms):
    if not text:
        return None, None
    hits, spans = [], []
    for label, rx in terms:
        m = re.search(rx, text, re.I)
        if m:
            hits.append(label)
            spans.append(m.group(0))
    return (hits or None), ("; ".join(spans) or None)


# ------------------------------------------------------------------ postcode / nation
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2})(\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
SCOTLAND_AREAS = {"AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW", "KY", "ML", "PA", "PH", "ZE"}
WALES_AREAS = {"CF", "LL", "NP", "SA"}
NI_AREAS = {"BT"}
CROSS_BORDER_AREAS = {"CH", "HR", "LD", "SY", "TD"}   # contain more than one nation
ENGLAND_AREAS = set("""AL B BA BB BD BH BL BN BR BS CA CB CM CO CR CT CV CW DA DE DH DL DN DT DY
E EC EN EX FY GL GU HA HD HG HP HU HX IG IP KT L LA LE LN LS LU M ME MK N NE NG NN NR NW OL OX
PE PL PO PR RG RH RM S SE SG SK SL SM SN SO SP SR SS ST SW TA TF TN TQ TR TS TW UB W WA WC WD
WF WN WR WS WV YO""".split())


def find_postcode(text):
    if not text:
        return None
    m = POSTCODE_RE.search(text.upper())
    return f"{m.group(1)}{m.group(2)} {m.group(3)}" if m else None


def nation_from_postcode(postcode):
    """Returns (nation or None, note)."""
    if not postcode:
        return None, "no postcode"
    area = re.match(r"[A-Z]+", postcode).group(0)
    if area in CROSS_BORDER_AREAS:
        return None, f"postcode area {area} crosses a national border"
    for nation, areas in (("Scotland", SCOTLAND_AREAS), ("Wales", WALES_AREAS),
                          ("Northern Ireland", NI_AREAS), ("England", ENGLAND_AREAS)):
        if area in areas:
            return nation, f"postcode area {area}"
    return None, f"postcode area {area} not in UK nations list (e.g. Crown Dependency)"


def build_provider_index(providers):
    index = {}
    for p in providers:
        names = [p.get("provider_name")] + (p.get("aliases") or "").split("|")
        for n in names:
            k = common.norm_key(n)
            if k:
                index[k] = p
    return index


def decide_nation(source_scope, provider, postcode):
    candidates = []
    if provider and provider.get("nation") in common.NATIONS:
        conf = "high" if provider.get("verification_status") == "VERIFIED" else "medium"
        candidates.append((provider["nation"], "provider_register", conf, provider.get("provider_id")))
    if source_scope and len(source_scope) == 1:
        candidates.append((source_scope[0], "single_nation_source", "high", "sources.yaml nation_scope"))
    pc_nation, pc_note = nation_from_postcode(postcode)
    if pc_nation:
        if source_scope and pc_nation not in source_scope:
            pc_note += f" (outside source scope {source_scope})"
        candidates.append((pc_nation, "postcode_area", "medium", pc_note))
    distinct = {c[0] for c in candidates}
    if not candidates:
        return {"value": common.UNKNOWN, "method": "none", "confidence": None,
                "basis": [pc_note], "conflict": False}
    if len(distinct) > 1:
        return {"value": common.UNKNOWN, "method": "conflict", "confidence": None,
                "basis": [f"{c[1]}={c[0]} ({c[3]})" for c in candidates], "conflict": True}
    best = candidates[0]
    return {"value": best[0], "method": best[1], "confidence": best[2],
            "basis": [f"{c[1]}={c[0]} ({c[3]})" for c in candidates], "conflict": False}


# ------------------------------------------------------------------ main transform
def normalise(raw: dict, source_scope, provider_index) -> dict:
    m = raw.get("mapped") or {}
    obs = {}
    for name in ["title", "employer", "location", "staff_group_text", "description"]:
        val = common.clean_text(m.get(name))
        obs[name] = field(val, "source_field", name, None) if val else missing(name)

    pc = find_postcode(m.get("postcode")) or find_postcode(m.get("location"))
    obs["postcode"] = field(pc, "regex", "postcode|location", pc) if pc else missing("postcode|location")

    desc = m.get("description")
    pay, ev = parse_pay(m.get("pay_text"))
    obs["pay"] = field(pay, "regex", "pay_text", ev) if pay else missing("pay_text")
    band, ev = parse_band(m.get("band_text")) if m.get("band_text") else parse_band(m.get("pay_text"))
    if band:
        obs["afc_band"] = field(band, "regex", "band_text|pay_text", ev)
    else:
        band, ev = parse_band(desc)
        obs["afc_band"] = field(band, "regex_description", "description", ev) if band else missing("band_text")
    hours, ev = parse_hours(m.get("hours_text"), True)
    if hours:
        obs["hours_per_week"] = field(hours, "regex", "hours_text", ev)
    else:
        hours, ev = parse_hours(desc, False)
        obs["hours_per_week"] = field(hours, "regex_description", "description", ev) if hours else missing("hours_text")
    ct, ev = parse_terms(m.get("contract_text"), CONTRACT_TERMS)
    obs["contract_type"] = field(ct, "regex", "contract_text", ev) if ct else missing("contract_text")
    wp, ev = parse_terms(m.get("working_pattern_text"), PATTERN_TERMS)
    obs["working_pattern"] = field(wp, "regex", "working_pattern_text", ev) if wp else missing("working_pattern_text")
    for name, src_field in [("closing_date", "closing_date_text"), ("posted_date", "posted_date_text")]:
        d, ev = parse_date(m.get(src_field))
        obs[name] = field(d, "regex", src_field, ev) if d else missing(src_field)

    provider = provider_index.get(common.norm_key(obs["employer"]["value"]))
    inferred = {
        "provider": ({"provider_id": provider["provider_id"], "provider_type": provider["provider_type"],
                      "nhs_funded": provider.get("nhs_funded") or None,
                      "verification_status": provider.get("verification_status"),
                      "method": "exact_name_or_alias_match"} if provider else
                     {"provider_id": None, "provider_type": None, "nhs_funded": None,
                      "verification_status": None, "method": "no_match"}),
        "nation": decide_nation(source_scope, provider, pc),
    }
    key_fields = ["title", "employer", "description", "pay", "closing_date"]
    return {
        "schema": SCHEMA,
        "observation_id": raw["observation_id"],
        "run_id": raw.get("run_id"),
        "source_id": raw["source_id"],
        "observed_date": raw.get("observed_date"),
        "retrieved_at_utc": raw.get("retrieved_at_utc"),
        "record_url": raw.get("record_url"),
        "request_url": raw.get("request_url"),
        "source_record_id": raw.get("source_record_id"),
        "content_sha256_original": raw.get("content_sha256_original"),
        "observed": obs,
        "inferred": inferred,
        "quality": {"missing_key_fields": [k for k in key_fields if obs[k]["value"] is None]},
        "versions": common.version_stamp(),
    }


def main():
    run_id = common.make_run_id("normalize")
    started = common.utc_now_iso()
    sources = {s["source_id"]: s for s in common.load_yaml(common.CONFIG / "sources.yaml")["sources"]}
    pindex = build_provider_index(common.load_providers())
    by_id, n_in, n_out, unresolved = {}, 0, 0, 0
    src_dirs = sorted(p for p in common.OBSERVATIONS.iterdir() if p.is_dir()) if common.OBSERVATIONS.exists() else []
    for src_dir in src_dirs:
        scope = sources.get(src_dir.name, {}).get("nation_scope") or []
        for ddir in sorted(d for d in src_dir.iterdir() if d.is_dir()):
            records = []
            for f in sorted(ddir.glob("*.jsonl")):
                for raw in common.read_jsonl(f):
                    n_in += 1
                    if raw.get("schema") == "raw_sighting/1":
                        base = by_id.get(raw.get("same_content_as"))
                        if base is None:
                            unresolved += 1
                            continue
                        rec = dict(base)
                        for k in ["observation_id", "run_id", "observed_date", "retrieved_at_utc",
                                  "request_url", "record_url"]:
                            rec[k] = raw.get(k)
                        rec["content_from_observation_id"] = base["observation_id"]
                    else:
                        rec = normalise(raw, scope, pindex)
                        rec["content_from_observation_id"] = None
                    by_id[rec["observation_id"]] = rec
                    records.append(rec)
            out = common.DERIVED / "normalized" / src_dir.name / f"{ddir.name}.jsonl"
            n_out += common.write_jsonl(out, records)
    status = "OK" if n_in else "OK_EMPTY"
    msg = f"{unresolved} sightings could not be resolved" if unresolved else "all observations normalised"
    common.log_run(run_id, "normalize", started, status if not unresolved else "WARN", "", n_in, n_out, msg)
    print(f"{status}: {n_in} observations -> {n_out} normalised ({msg})")


if __name__ == "__main__":
    main()
