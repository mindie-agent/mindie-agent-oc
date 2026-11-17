#!/usr/bin/env python3
"""OpenCode CLI and module entry point for the shared RSI loop coordinator (`mindie_harness.rsi.rsi_loop`).

[Role]
Exposes the canonical `mindie_harness.rsi.rsi_loop` implementation (`init`, `next_step`, `complete`, `finish`)
as a local script entry point for the OpenCode harness plugin, falling back to `mindie_knowledge.graded.rsi_loop`
only when running inside a pre-Wave-B legacy runtime generation.

[Invariants]
- All `mindie-rsi-loop/1` state transitions, composable step rules, receipt-honest `record`/`learn` recovery,
  and stagnation route tracking live in `mindie_harness.rsi.rsi_loop` (single source of truth).
- Rebinds `sys.modules[__name__]` to the canonical implementation so module attribute mutations in callers
  and tests apply directly to the shared module.

[Timeouts & Locks]
- Delegated to `mindie_harness.rsi.rsi_loop` and `mindie_harness.rsi.experiment_ledger`.

[Side-Effect Policy]
- Delegated to `mindie_harness.rsi.rsi_loop`.
"""
from __future__ import annotations

import sys

try:
    from mindie_harness.rsi import rsi_loop as _shared
except ImportError:
    from mindie_knowledge.graded import rsi_loop as _shared

if __name__ == "__main__":
    raise SystemExit(_shared.main())

_alias = __name__
globals().update({k: v for k, v in _shared.__dict__.items() if k not in ("__name__", "__file__", "__spec__", "__loader__")})
sys.modules[_alias] = _shared
