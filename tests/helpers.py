"""Shared test helpers. All test data here is synthetic, written for tests only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def field(value, method="source_field"):
    return {"value": value, "method": method, "source_field": None, "evidence": None}


def make_normalized(oid, source_id, title, employer, description, observed_date,
                    record_id=None, closing=None, nation="England", pay_min=None):
    return {
        "schema": "normalized_vacancy/1", "observation_id": oid, "source_id": source_id,
        "observed_date": observed_date, "record_url": f"https://example.org/{source_id}/{record_id or oid}",
        "source_record_id": record_id, "content_sha256_original": oid,
        "observed": {
            "title": field(title), "employer": field(employer), "description": field(description),
            "closing_date": field(closing), "pay": field({"min": pay_min} if pay_min else None),
        },
        "inferred": {"nation": {"value": nation}},
    }


def make_cluster(cid, title, description, employer="Example NHS Trust", first_seen="2026-09-01",
                 nation="England"):
    return {
        "cluster_id": cid, "canonical_observation_id": "o_" + cid, "first_seen": first_seen,
        "last_seen": first_seen,
        "canonical": {"record_url": f"https://example.org/{cid}",
                      "observed": {"title": field(title), "description": field(description),
                                   "employer": field(employer)},
                      "inferred": {"nation": {"value": nation}}},
    }
