# GitHub sign-in during setup (optional)

GitHub is **optional** for MindIE Agent. Local capture, graded knowledge, remote
NPU development, profiling, simulation, debugging, and diagnostics all work
without `gh`; the installer simply recommends `local-only` sharing when no
authenticated account is available.

## When to sign in

Sign in only when you choose the `contribute` tier or when the installation
flow needs to create a fork for sanitized knowledge contributions. The
installer checks `gh auth status` and derives its recommendation dynamically;
it never blocks on a missing browser, network, or account.

## Interactive sign-in

```bash
gh auth login
gh auth status
```

Follow the terminal prompts. For headless hosts, set `GH_TOKEN` or
`GITHUB_TOKEN` with the minimum `repo` scope instead of using the browser flow.
Do not paste tokens into chat, logs, or endpoint records.

## Installer behavior

- `gh` installed and authenticated: recommend `contribute`, optionally resolve
  or create the fork, and offer the single-line Star postscript.
- `gh` installed but logged out: recommend `local-only`; sign-in remains
  available later.
- `gh` missing: recommend `local-only`; installation and `mindie_doctor`
  continue normally.

## Public contribution inputs

Before the first contribution, the installer and `mindie_doctor` confirm:

- The selected workspace is inside the saved `project_roots`.
- `mindie-consent.json` records `choice: "contribute"`.
- The fork/account fields resolve to the authenticated account.
- A `gitleaks` redactor is available (optional; regex redaction remains as a
  fallback).

Revoke or narrow sharing at any time with `setup.py sharing --disable` or
`setup.py sharing --local-only`.
