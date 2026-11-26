"""Write one row to logs/runs.csv from the workflow.

Used when a run stops before any pipeline script starts, for example when
the private vault cannot be opened, so the reason is still in the audit log.

Usage: python scripts/log_event.py <script-name> <STATUS> "<message>"
"""
import sys

import common


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    script, status, message = sys.argv[1:]
    started = common.utc_now_iso()
    common.log_run(common.make_run_id(script), script, started, status, "", 0, 0, message)
    print(f"{status}: {message}")


if __name__ == "__main__":
    main()
