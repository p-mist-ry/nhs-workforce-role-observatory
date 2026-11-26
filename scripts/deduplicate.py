"""Group observations into adverts, and adverts into vacancy clusters.

Level 1 - advert: the same source record seen on several days is ONE advert.
          A re-appearance after `readvert_gap_days` is a NEW advert.
Level 2 - vacancy cluster: adverts for the same vacancy posted on more than
          one site (or posted twice identically on one site) are ONE cluster.

Counting rule used everywhere downstream: one cluster = one unit of
advertised demand. Nothing is deleted; every link records its method.

Input : derived/normalized/*.jsonl (advert versions with their sighting dates)
Output: derived/dedup/vacancy_clusters.jsonl (rebuilt each run)

Only adverts with the same employer and the same title, posted within the
re-advert window of each other, are compared, so the run stays fast as the
archive grows.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

import common


def shingles(text: str, k: int = 5) -> set:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(words) < k:
        return set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def val(rec, name):
    return (rec.get("observed", {}).get(name) or {}).get("value")


def days_between(a: str, b: str) -> int:
    return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def record_dates(r):
    return r.get("observed_dates") or ([r["observed_date"]] if r.get("observed_date") else [])


def build_adverts(records, gap_days):
    by_key = defaultdict(list)
    for r in records:
        if r.get("source_record_id"):
            key = f"{r['source_id']}|id|{r['source_record_id']}"
        elif r.get("record_url"):
            key = f"{r['source_id']}|url|{r['record_url']}"
        else:
            key = f"{r['source_id']}|sha|{r.get('content_sha256_original')}"
        for d in record_dates(r):
            by_key[key].append((d, r))
    adverts = []
    for key, items in by_key.items():
        items.sort(key=lambda x: (x[0], x[1]["observation_id"]))
        current, episode = [], 0
        for d, r in items:
            if current and days_between(current[-1][0], d) > gap_days:
                adverts.append((f"{key}|ep{episode}", current))
                current, episode = [], episode + 1
            current.append((d, r))
        if current:
            adverts.append((f"{key}|ep{episode}", current))
    return adverts


def advert_summary(key, items):
    """items: (date, advert version) pairs for one advert episode, in date order."""
    first = items[0][1]
    ids = []
    for _d, r in items:
        if r["observation_id"] not in ids:
            ids.append(r["observation_id"])
    return {
        "advert_key": key,
        "source_id": first["source_id"],
        "source_record_id": first.get("source_record_id"),
        "record_url": first.get("record_url"),
        "first_seen": items[0][0],
        "last_seen": items[-1][0],
        "n_observations": len({d for d, _r in items}),
        "observation_ids": ids,
        "rep": first,
    }


TRUNCATION_MARKS = ("...", "\u2026")
MIN_FULL_TEXT_CHARS = 300


def full_text(text):
    """Return the text only if it looks like a complete advert, else None."""
    if not text:
        return None
    text = text.strip()
    if text.endswith(TRUNCATION_MARKS) or len(text) < MIN_FULL_TEXT_CHARS:
        return None
    return text


def add_days(date_str, n):
    return (dt.date.fromisoformat(date_str) + dt.timedelta(days=n)).isoformat()


def link_adverts(ads, min_sim, gap_days):
    """Return list of (i, j, method, similarity)."""
    links = []
    blocks = defaultdict(list)       # same normalised title AND same employer
    for i, a in enumerate(ads):      # ads are already sorted by first_seen
        tkey = common.norm_key(val(a["rep"], "title"))
        ekey = common.norm_key(val(a["rep"], "employer"))
        if tkey and ekey:
            blocks[(tkey, ekey)].append(i)
    cache = {}

    def sh(i):
        if i not in cache:
            cache[i] = shingles(val(ads[i]["rep"], "description"))
        return cache[i]

    for idxs in blocks.values():
        for x in range(len(idxs)):
            a = ads[idxs[x]]
            # later adverts can only match if posted within the re-advert gap
            # or while this advert was still live
            limit = max(add_days(a["first_seen"], gap_days), a["last_seen"])
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                b = ads[j]
                if b["first_seen"] > limit:
                    break
                cd_a, cd_b = val(a["rep"], "closing_date"), val(b["rep"], "closing_date")
                if cd_a and cd_b and cd_a != cd_b:
                    continue
                if a["source_id"] != b["source_id"]:
                    sim = jaccard(sh(i), sh(j))
                    if sim >= min_sim:
                        links.append((i, j, "cross_source_text_similarity", round(sim, 3)))
                    elif not sh(i) and not sh(j):
                        pay_a, pay_b = val(a["rep"], "pay"), val(b["rep"], "pay")
                        if cd_a and cd_b and pay_a and pay_b and pay_a.get("min") == pay_b.get("min"):
                            links.append((i, j, "cross_source_exact_fields_no_text", None))
                else:
                    # Same site, different advert ids: only merge when the FULL
                    # text is identical. Truncated feed extracts are not enough,
                    # because an employer's separate adverts often open alike.
                    da, db = full_text(val(a["rep"], "description")), full_text(val(b["rep"], "description"))
                    if da and db and da == db and cd_a and cd_a == cd_b:
                        links.append((i, j, "same_source_identical_text", 1.0))
    return links


def cluster(records, min_sim, gap_days, priority):
    ads = [advert_summary(k, o) for k, o in build_adverts(records, gap_days)]
    ads.sort(key=lambda a: (a["first_seen"], priority.get(a["source_id"], 99), a["advert_key"]))
    links = link_adverts(ads, min_sim, gap_days)
    uf = UnionFind(len(ads))
    for i, j, _m, _s in links:
        uf.union(i, j)
    groups = defaultdict(list)
    for i in range(len(ads)):
        groups[uf.find(i)].append(i)
    link_by_root = defaultdict(list)
    for i, j, m, s in links:
        link_by_root[uf.find(i)].append({"a": ads[i]["advert_key"], "b": ads[j]["advert_key"],
                                         "method": m, "similarity": s})
    out = []
    for root, members in groups.items():
        canon = ads[members[0]]  # sorted: earliest, then source priority
        rep = canon["rep"]
        nations = sorted({ads[m]["rep"]["inferred"]["nation"]["value"] for m in members})
        out.append({
            "schema": "vacancy_cluster/1",
            "cluster_id": "vc_" + common.sha256_text(canon["advert_key"])[:16],
            "canonical_observation_id": rep["observation_id"],
            "canonical_source_id": canon["source_id"],
            "first_seen": min(ads[m]["first_seen"] for m in members),
            "last_seen": max(ads[m]["last_seen"] for m in members),
            "n_adverts": len(members),
            "n_sources": len({ads[m]["source_id"] for m in members}),
            "member_nations": nations,
            "members": [{k: v for k, v in ads[m].items() if k != "rep"} for m in members],
            "dedup_links": link_by_root.get(root, []),
            "canonical": {k: rep[k] for k in ["observation_id", "source_id", "record_url",
                                              "observed_date", "observed", "inferred"]},
            "versions": common.version_stamp(),
        })
    out.sort(key=lambda c: (c["first_seen"], c["cluster_id"]))
    return out


def main():
    run_id = common.make_run_id("deduplicate")
    started = common.utc_now_iso()
    pcfg = common.load_yaml(common.CONFIG / "pipeline.yaml")["dedup"]
    priority = {s["source_id"]: s.get("priority", 99)
                for s in common.load_yaml(common.CONFIG / "sources.yaml")["sources"]}
    records = []
    for f in common.iter_files(common.DERIVED / "normalized", "*.jsonl"):
        records.extend(common.read_jsonl(f))
    clusters = cluster(records, float(pcfg["min_text_similarity"]), int(pcfg["readvert_gap_days"]), priority)
    n = common.write_jsonl(common.DERIVED / "dedup" / "vacancy_clusters.jsonl", clusters)
    common.log_run(run_id, "deduplicate", started, "OK" if records else "OK_EMPTY", "",
                   len(records), n, f"{len(records)} observations -> {n} vacancy clusters")
    print(f"{len(records)} observations -> {n} vacancy clusters")


if __name__ == "__main__":
    main()
