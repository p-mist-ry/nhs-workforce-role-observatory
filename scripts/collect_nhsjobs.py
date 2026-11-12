"""Collect adverts from NHS Jobs (England and Wales).

This is a thin wrapper. All logic, including the safety checks, lives in
collect_core.py. Until config/sources.yaml contains a documented endpoint
for source_id 'nhsjobs', this script logs BLOCKED_NOT_CONFIGURED and
fetches nothing.
"""
import collect_core

if __name__ == "__main__":
    collect_core.main("nhsjobs")
