#!/usr/bin/env python3
"""OpenCode CLI and module entry point for the shared RSI experiment ledger (`mindie_knowledge.graded.experiment_ledger`).

[Role]
Exposes the canonical `mindie_knowledge.graded.experiment_ledger` implementation
(`fingerprint`, `check_trials`, `judge`, `gate`, `append`, `fold`,
`render_progress_file`, `iter_ledger`, `read_ledger`) as a local script entry
point for the OpenCode harness plugin.

[Invariants]
- All ledger schemas (`mindie-experiment-ledger/1`), 4-gate RSI evaluation rules,
  and `PROGRESS.md` recovery-preserving rendering live in
  `mindie_knowledge.graded.experiment_ledger` (single source of truth).
- Rebinds `sys.modules[__name__]` to `mindie_knowledge.graded.experiment_ledger`
  so module attribute mutations in callers and tests apply directly to the shared
  implementation.

[Timeouts & Locks]
- Delegated to `mindie_knowledge.graded.experiment_ledger` (15.0s Git subprocess
  budget, 5.0s lock budget, 64 KiB output cap).

[Side-Effect Policy]
- Delegated to `mindie_knowledge.graded.experiment_ledger`.
"""
from __future__ import annotations

import sys
from mindie_knowledge.graded import experiment_ledger as _shared

if __name__ == "__main__":
    raise SystemExit(_shared.main())

_alias = __name__
globals().update({k: v for k, v in _shared.__dict__.items() if k not in ("__name__", "__file__", "__spec__", "__loader__")})
sys.modules[_alias] = _shared
