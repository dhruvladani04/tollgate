"""Print the latest audit records: `python -m tollgate.audit`."""

import argparse
import sys
from pathlib import Path

from tollgate.audit.report import render
from tollgate.audit.store import AuditStore


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tollgate.audit", description="Print the latest audit records."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(".tollgate/audit.sqlite"),
        metavar="FILE",
        help="audit database (default: %(default)s)",
    )
    parser.add_argument(
        "--last", type=int, default=20, metavar="N", help="how many records to print"
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no audit database at {args.db}", file=sys.stderr)
        return 1
    with AuditStore(args.db) as store:
        records = store.recent(args.last)
        total = store.count()
    if not records:
        print("the audit log is empty")
        return 0
    print(render(records))
    print(f"\n{len(records)} of {total} records, oldest first. Scores are on a 0-3 scale.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
