"""`tollgate-init`: write a starting policy and upstreams file into a folder.

A copy of the example policy ships inside the package, so someone who installed
Tollgate without the repository has something to start from.
"""

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

POLICY_FILE = "policy.yaml"
UPSTREAMS_FILE = "upstreams.json"
# The folder the example files let the agent read and write: the one they are written to.
HERE = "."
# The example policy's sandbox line, which points at a folder only the repository has.
REPO_SANDBOX_LINE = "      - examples/sandbox\n"

UPSTREAMS = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", HERE],
        }
    }
}


def starting_policy() -> str:
    """The packaged example policy, with its path sandbox set to the current folder."""
    text = (files("tollgate") / "examples" / "policy.example.yaml").read_text(encoding="utf-8")
    if REPO_SANDBOX_LINE not in text:
        raise RuntimeError("the packaged example policy no longer has its sandbox line")
    return text.replace(REPO_SANDBOX_LINE, f"      - {HERE}\n")


def write_files(folder: Path, force: bool) -> list[Path]:
    """Write both files and return their paths. Nothing is written if either exists."""
    contents = {
        folder / POLICY_FILE: starting_policy(),
        folder / UPSTREAMS_FILE: json.dumps(UPSTREAMS, indent=2) + "\n",
    }
    existing = [path for path in contents if path.exists()]
    if existing and not force:
        names = ", ".join(path.name for path in existing)
        raise FileExistsError(f"{names} already exists in {folder}. Pass --force to overwrite.")
    folder.mkdir(parents=True, exist_ok=True)
    for path, text in contents.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    return list(contents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tollgate-init",
        description="Write a starting policy.yaml and upstreams.json for Tollgate.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="where to write the two files (default: the current folder)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite files that exist")
    args = parser.parse_args(argv)
    try:
        written = write_files(args.dir, args.force)
    except FileExistsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    print(
        "\nThe filesystem server in upstreams.json, and the policy's path check, are both "
        "limited\nto the folder you run Tollgate from. Edit both files to suit your setup.\n\n"
        "Next:\n"
        "  1. Set TYPESAFE_API_KEY in your environment.\n"
        f"  2. tollgate --upstreams {UPSTREAMS_FILE} --policy {POLICY_FILE}\n"
        f"  3. tollgate-dashboard --policy {POLICY_FILE}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
