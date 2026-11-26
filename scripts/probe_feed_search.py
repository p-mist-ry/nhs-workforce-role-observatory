"""Ask NHS Jobs' own search which live adverts mention each AI term.

The documented `keyword` parameter of the Self-Serve search searches the job
title and the FULL advert text, excluding the 'About us' section (NHSBSA
Self-Serve Job Adverts API guide, V1.07). So, without reading any advert
pages, we can learn which adverts mention a term somewhere in their duties
or requirements, while leaving out employer boilerplate in 'About us'.

What this does NOT give: the matching text itself. How the search treats
multi-word terms (phrase, all words, or any word) is not documented, so
results are stored as UNVERIFIED search matches. They are checked against
full advert text once that is available, and are never counted in the
headline AI indicator.

Settings: config/pipeline.yaml -> feed_search_probes
Output (append-only): probes/<source_id>/<YYYY-MM-DD>/<run_id>.jsonl
One record per term: the exact request URLs, statuses and the advert ids
returned. No advert text is stored.
"""
from __future__ import annotations

import time
import urllib.error
import xml.etree.ElementTree as ET

import collect_core as cc
import common


def probe_term(src, defaults, settings, term, first_request):
    fmap = src.get("field_map") or {}
    delay = float(defaults.get("min_seconds_between_requests", 3))
    ids, urls, statuses = [], [], []
    complete, error = False, None
    for page in range(1, int(settings.get("max_pages_per_term", 20)) + 1):
        params = dict(settings.get("params") or {})
        params["keyword"] = term["keyword"]
        params["page"] = page
        url = cc.build_url(src["endpoint"], params)
        if defaults.get("respect_robots_txt", True) and not cc.robots_allows(url, defaults["user_agent"]):
            raise cc.Blocked(f"robots.txt disallows {url}")
        if not (first_request and page == 1):
            time.sleep(delay)
        try:
            status, body = cc.fetch(url, defaults)
            records = cc.parse_records(body, src)
        except (urllib.error.URLError, ET.ParseError, OSError) as exc:
            error = repr(exc)
            break
        urls.append(url)
        statuses.append(status)
        new = 0
        for rec in records:
            rid = cc.map_record(rec, fmap).get("source_record_id")
            if rid and rid not in ids:
                ids.append(rid)
                new += 1
        if not records or new == 0:
            complete = True
            break
    return {"source_record_ids": ids, "request_urls": urls, "http_statuses": statuses,
            "complete": complete, "error": error}


def main():
    run_id = common.make_run_id("probe_feed_search")
    started = common.utc_now_iso()
    settings = (common.load_yaml(common.CONFIG / "pipeline.yaml") or {}).get("feed_search_probes") or {}
    source_id = settings.get("source_id", "nhsjobs")
    try:
        if settings.get("enabled") is not True:
            raise cc.Blocked("feed_search_probes.enabled is not true")
        src, defaults = cc.load_source(source_id)
        problems = cc.preflight(src, defaults)
        if problems:
            raise cc.Blocked("source not configured: " + "; ".join(problems))
        storage = common.storage_problem()
        if storage:
            raise cc.Blocked(storage)
        terms = settings.get("terms") or []
        if not terms:
            raise cc.Blocked("feed_search_probes.terms is empty")
        day = common.today_utc()
        out = []
        for i, term in enumerate(terms):
            res = probe_term(src, defaults, settings, term, first_request=(i == 0))
            out.append({
                "schema": "feed_search_probe/1",
                "observation_id": common.sha256_text(f"{source_id}|probe|{term['id']}|{run_id}")[:24],
                "run_id": run_id, "source_id": source_id, "retrieved_at_utc": started,
                "observed_date": day, "probe_id": term["id"], "keyword": term["keyword"],
                "n_matches": len(res["source_record_ids"]),
                "verification": "UNVERIFIED_SEARCH_MATCH",
                **res,
            })
            print(f"{term['id']}: {len(res['source_record_ids'])} adverts"
                  + ("" if res["complete"] else " (incomplete)"))
        path = common.PROBES / source_id / day / f"{run_id}.jsonl"
        common.write_jsonl(path, out, exclusive=True)
        failed = [o["probe_id"] for o in out if o["error"]]
        capped = [o["probe_id"] for o in out if not o["complete"] and not o["error"]]
        status = "PARTIAL" if failed else "OK"
        msg = f"{len(out)} terms probed"
        if failed:
            msg += f"; failed: {', '.join(failed)}"
        if capped:
            msg += f"; stopped at max_pages_per_term: {', '.join(capped)}"
        common.log_run(run_id, "probe_feed_search", started, status, source_id, len(terms), len(out), msg)
    except cc.Blocked as exc:
        common.log_run(run_id, "probe_feed_search", started, "BLOCKED", source_id, 0, 0, str(exc))
        print(f"BLOCKED: {exc}")


if __name__ == "__main__":
    main()
