"""`tollgate-approve`: see the calls Tollgate is holding, and approve or deny them."""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from tollgate.approve.prompt import preview_arguments
from tollgate.approve.queue import APPROVALS_FILE, ApprovalError, ApprovalQueue, HeldCall


def render(held: Sequence[HeldCall]) -> str:
    """One block per held call: what it is, why it is held and how long is left."""
    if not held:
        return "nothing is waiting for approval"
    now = datetime.now(UTC)
    blocks = []
    for call in held:
        lines = [
            f"#{call.id}  {call.tool}  (from {call.upstream})  waiting "
            f"{(now - call.created).total_seconds():.0f} s, "
            f"{max((call.expires - now).total_seconds(), 0):.0f} s left",
            *(f"    why: {reason}" for reason in call.reasons),
            f"    arguments: {preview_arguments(call.arguments)}",
        ]
        if call.user_request:
            lines.append(f"    user asked: {call.user_request}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tollgate-approve",
        description="See the tool calls Tollgate is holding, and approve or deny them.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tollgate"),
        metavar="DIR",
        help="Tollgate's state folder (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show the calls waiting for an answer")
    for name, summary in (("approve", "let a held call run"), ("deny", "refuse a held call")):
        command = commands.add_parser(name, help=summary)
        command.add_argument("id", type=int, help="the number shown by the list command")
    args = parser.parse_args(argv)

    path = args.state_dir / APPROVALS_FILE
    if not path.exists():
        if args.command == "list":
            print(render([]))
            return 0
        print(f"error: no approval queue at {path}", file=sys.stderr)
        return 1

    with ApprovalQueue(path) as queue:
        if args.command == "list":
            print(render(queue.pending()))
            return 0
        try:
            call = queue.answer(args.id, approve=args.command == "approve")
        except ApprovalError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    print(f"{call.status} #{call.id} ({call.tool})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
