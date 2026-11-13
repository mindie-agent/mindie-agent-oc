# Domain Tooling & NPU Discipline

## 1. Graded Knowledge Tools (`knowledge_cli.py`)

- `knowledge_query`: Searches graded `L1–L4` units and task blocks (`token_budget` `1..64000`, default `8000` in standard/reuse mode and `24000` in `mode="explore"`). Returns bounded `cards` and deduplicated `foundation_cards`.
- `knowledge_explain`: Reads exact `mindie://<domain>/units/<id>@<rev>` references and expands `requires` once (`token_budget` `1..64000`, default `16000` in reuse/diagnose and `24000` in `mode="explore"`).
- `knowledge_feedback`: Records `discovered`, `loaded`, `applied`, or `evaluated` (`supported` | `refuted` | `inconclusive`) usage events.
- `knowledge_distill`: Distills verified milestone (`task_state="verified_milestone"`) or completed (`task_state="completed"`) evidence into graded `L1–L4` units. `L4` mutators require a verified leave-one-out ablation receipt.
- `experiment_ledger.py` & `rsi_loop.py`: Evaluate 4-stage RSI candidate verification gates (`fingerprint`, `gate`, `judge`, `append`, `render`) and manage resumable `profile -> consult -> propose -> measure -> learn` rounds over `experiment-ledger.jsonl` and `PROGRESS.md`.

## 2. Remote NPU Execution & Coordinator Leases

- Acquire NPU and port leases through `coordinator` before running device workloads (`vram_free_mb >= Expected VRAM`; respect Ascend A3 910C dual-Die `Phy-ID 0..15` package boundaries).
- Track long-running jobs in `PROGRESS.md` under `in_flight_runs` before yielding or ending the turn.
