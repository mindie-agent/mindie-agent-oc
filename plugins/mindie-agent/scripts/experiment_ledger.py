#!/usr/bin/env python3
"""OpenCode CLI and module entry point for the shared RSI experiment ledger (`mindie_harness.rsi.experiment_ledger`).

[Role]
Exposes the canonical `mindie_harness.rsi.experiment_ledger` implementation (`fingerprint`, `check_trials`,
`judge`, `gate`, `append`, `fold`, `render_progress_file`, `iter_ledger`, `read_ledger`) as a local script
entry point for the OpenCode harness plugin, falling back to `mindie_knowledge.graded.experiment_ledger`
only when running inside a pre-Wave-B legacy runtime generation.

[Invariants]
- All ledger schemas (`mindie-experiment-ledger/1`), verification rules, and `PROGRESS.md` recovery-preserving
  rendering live in `mindie_harness.rsi.experiment_ledger` (single source of truth).
- Rebinds `sys.modules[__name__]` to the canonical implementation so module attribute mutations in callers
  and tests apply directly to the shared module.

[Timeouts & Locks]
- Delegated to `mindie_harness.rsi.experiment_ledger` (15.0s Git subprocess budget, 5.0s lock budget, 64 KiB cap).

[Side-Effect Policy]
- Delegated to `mindie_harness.rsi.experiment_ledger`.
"""
from __future__ import annotations

import sys

try:
    from mindie_harness.rsi import experiment_ledger as _shared
except ImportError:
    from mindie_knowledge.graded import experiment_ledger as _shared

if __name__ == "__main__":
    raise SystemExit(_shared.main())

_alias = __name__
globals().update({k: v for k, v in _shared.__dict__.items() if k not in ("__name__", "__file__", "__spec__", "__loader__")})
sys.modules[_alias] = _shared
