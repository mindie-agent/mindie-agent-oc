"""Static contract validation for the OpenCode adapter (`contract_validation.py`).

[Role]
Validates a candidate payload before it may become the active OpenCode profile:
`product-contract.json`/`plugin.json` version agreement, the
`harness-compatibility.json` schema, every declared support interval, and the
coverage of each `used_interfaces` entry by its `interface_registry` bound.

[Invariants]
- Pure, read-only validation of one source tree; nothing is written, staged, or
  published, and the active `oc.json` is never consulted.
- Interval math is SemVer-correct: prerelease endpoints require
  `include_prerelease`, duplicates are rejected, and a used interface must be
  covered by a declared interval that is at least as wide.
- When `mindie_knowledge` is importable the shared `releases.validate_compatibility`
  authority is also consulted; otherwise local validation still decides.

[Timeouts & Locks]
- Synchronous JSON reads and arithmetic only: no subprocess, no lock, no network.

[Side-Effect Policy]
- Strictly read-only; returns a `mindie-candidate-check/1` summary dict or raises
  `ValueError` describing the first contract violation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from oc_native_compat import ensure_oc_native_support
from paths import read_json, repo_root

_SEMVER_TUPLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
_IFACE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")


def _parse_ver_key(raw: object) -> tuple[int, int, int, int, str]:
    if not isinstance(raw, str):
        raise ValueError("invalid version string in compatibility interval")
    m = _SEMVER_TUPLE_RE.fullmatch(raw.strip())
    if m is None:
        raise ValueError(f"invalid semantic version in compatibility interval: {raw!r}")
    pre = m.group(4) or ""
    # In SemVer, prerelease < normal release (`0 < 1`).
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre)


def _validate_intervals_local(value: object) -> list[tuple[tuple, tuple, bool, bool]]:
    if not isinstance(value, list) or not (1 <= len(value) <= 32):
        raise ValueError("compatibility supported intervals must be a non-empty list")
    out: list[tuple[tuple, tuple, bool, bool]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"minimum", "maximum", "maximum_inclusive", "include_prerelease"}:
            raise ValueError("invalid compatibility interval schema")
        inc = item["maximum_inclusive"]
        pre = item["include_prerelease"]
        if type(inc) is not bool or type(pre) is not bool:
            raise ValueError("compatibility interval flags must be booleans")
        vmin = _parse_ver_key(item["minimum"])
        vmax = _parse_ver_key(item["maximum"])
        if vmin > vmax or (vmin == vmax and not inc):
            raise ValueError("compatibility interval minimum exceeds maximum")
        if (vmin[3] == 0 or vmax[3] == 0) and not pre:
            raise ValueError("prerelease endpoint requires include_prerelease=true")
        parsed_iv = (vmin, vmax, inc, pre)
        if parsed_iv in out:
            raise ValueError("duplicate compatibility interval")
        out.append(parsed_iv)
    return out


def _interval_covers(bound: tuple[tuple, tuple, bool, bool], target: tuple[tuple, tuple, bool, bool]) -> bool:
    bmin, bmax, binc, bpre = bound
    tmin, tmax, tinc, tpre = target
    return (
        bmin <= tmin
        and (bmax > tmax or (bmax == tmax and (binc or not tinc)))
        and (bpre or not tpre)
    )


def validate_contracts(source_root: str | Path | None = None) -> dict:
    """Validate one candidate payload tree; raises `ValueError` on a violation."""
    root = Path(source_root).resolve() if source_root is not None else repo_root()
    contract = read_json(root / "product-contract.json")
    compat = read_json(root / "harness-compatibility.json")
    manifest = read_json(root / "plugins" / "mindie-agent" / "plugin.json")
    if contract.get("schema") != "mindie-product-contract/1" or contract.get("harness") != "opencode":
        raise ValueError("product-contract.json schema or harness mismatch for opencode")
    native_ver = contract.get("native_version")
    if not isinstance(native_ver, str) or not re.fullmatch(r"\d+\.\d+\.\d+", native_ver):
        raise ValueError("product-contract.json requires a formal SemVer native_version")
    if native_ver != manifest.get("version"):
        raise ValueError("plugin.json version does not match product-contract.json native_version")
    if compat.get("schema") != "mindie-harness-compatibility/1" or compat.get("harness") != "opencode":
        raise ValueError("harness-compatibility.json schema or harness mismatch for opencode")

    top_intervals = _validate_intervals_local(compat.get("supported"))
    used_ifaces = compat.get("used_interfaces")
    registry = compat.get("interface_registry")
    if (
        not isinstance(used_ifaces, list)
        or not used_ifaces
        or len(set(used_ifaces)) != len(used_ifaces)
        or any(not isinstance(i, str) or not _IFACE_ID_RE.fullmatch(i) for i in used_ifaces)
    ):
        raise ValueError("harness-compatibility.json used_interfaces is invalid")
    if not isinstance(registry, dict) or not registry:
        raise ValueError("harness-compatibility.json interface_registry is empty or invalid")
    declared_bounds: dict[str, list[tuple[tuple, tuple, bool, bool]]] = {}
    for ident, entry in registry.items():
        if not isinstance(ident, str) or not _IFACE_ID_RE.fullmatch(ident):
            raise ValueError("invalid interface identifier in interface_registry")
        if not isinstance(entry, dict) or set(entry) != {"supported", "code_paths", "test_ids", "evidence_refs"}:
            raise ValueError(f"invalid interface_registry entry for {ident}")
        declared_bounds[ident] = _validate_intervals_local(entry["supported"])
        for field in ("code_paths", "test_ids", "evidence_refs"):
            vals = entry[field]
            if not isinstance(vals, list) or not vals or any(not isinstance(v, str) or not v.strip() for v in vals):
                raise ValueError(f"interface_registry[{ident}].{field} must be non-empty strings")
    for ident in used_ifaces:
        if ident not in declared_bounds:
            raise ValueError(f"used interface {ident!r} is missing from interface_registry")
        for target_iv in top_intervals:
            if not any(_interval_covers(b, target_iv) for b in declared_bounds[ident]):
                raise ValueError(f"interface {ident!r} does not cover declared harness support interval")

    if importlib.util.find_spec("mindie_knowledge") is not None:
        ensure_oc_native_support()
        from mindie_knowledge import releases

        releases.validate_compatibility(compat, harness_name="opencode")

    return dict(
        schema="mindie-candidate-check/1",
        status="validated",
        harness="opencode",
        native_version=native_ver,
    )


__all__ = ["validate_contracts"]
