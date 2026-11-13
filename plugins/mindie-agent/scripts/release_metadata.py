#!/usr/bin/env python3
"""Validate OpenCode (`oc`) release compatibility and emit `mindie-plugin-release/1` JSON.

[Role]
Validates `harness-compatibility.json` (`validate_compatibility`), verifies via AST
and filesystem inspection that every registered `code_paths` and `test_ids` entry
exists in the repository, and outputs `mindie-plugin-release.json`.

[Invariants]
- Every `used_interfaces` entry in `harness-compatibility.json` covers `supported`
  and points to existing source files/symbols and pytest test functions.

[Timeouts & Locks]
- Purely synchronous local JSON and AST validation; zero subprocesses or locks.

[Side-Effect Policy]
- Strictly read-only; prints validated JSON metadata to stdout.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from oc_native_compat import ensure_oc_native_support
from paths import read_json

ensure_oc_native_support()

REPOSITORY = "mindie-agent/mindie-agent-oc"


def _has_python_symbol(path: Path, symbol: str) -> bool:
    nodes = ast.parse(path.read_text(encoding="utf-8")).body
    for segment in symbol.split("."):
        selected = next(
            (
                node
                for node in nodes
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == segment
            ),
            None,
        )
        if selected is None:
            return False
        nodes = selected.body
    return True


def compatibility(source: Path) -> dict:
    from mindie_knowledge.releases import validate_compatibility

    return validate_compatibility(
        read_json(source.resolve() / "harness-compatibility.json"),
        harness_name="opencode",
    )


def check_registry(source: Path, tests: Path) -> dict:
    source_root = source.resolve()
    tests_root = tests.resolve()
    value = compatibility(source_root)
    for interface in value["used_interfaces"]:
        row = value["interface_registry"][interface]
        for reference in row["code_paths"]:
            filename, sep, symbol = reference.partition(":")
            target = (source_root / filename).resolve()
            if not target.is_relative_to(source_root) or not target.is_file():
                raise ValueError(f"registered code path missing for {interface}: {reference}")
            if sep and target.suffix == ".py" and not _has_python_symbol(target, symbol):
                raise ValueError(f"registered Python symbol missing for {interface}: {reference}")
        for reference in row["test_ids"]:
            filename, sep, symbol = reference.partition("::")
            rel = Path(filename).relative_to("tests") if Path(filename).parts[:1] == ("tests",) else Path(filename)
            target = (tests_root / rel).resolve()
            if not sep or not target.is_relative_to(tests_root) or not target.is_file() or not _has_python_symbol(target, symbol):
                raise ValueError(f"registered contract test missing for {interface}: {reference}")
    return value


def metadata(source: Path, revision: str) -> dict:
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release source requires a full 40-char immutable commit SHA")
    source_root = source.resolve()
    contract = read_json(source_root / "product-contract.json")
    payload = dict(
        schema="mindie-plugin-release/1",
        version=contract["native_version"],
        source=dict(repository=REPOSITORY, sha=revision),
        compatibility=compatibility(source_root),
    )
    from mindie_knowledge.releases import validate_release_metadata

    return validate_release_metadata(payload, repository=REPOSITORY, harness_name="opencode")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tests", type=Path)
    args = parser.parse_args(argv)
    if args.tests is not None:
        check_registry(args.source, args.tests)
    print(json.dumps(metadata(args.source, args.revision), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
