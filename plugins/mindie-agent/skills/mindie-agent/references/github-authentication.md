# GitHub sign-in during setup

Use this flow for installation, first-use configuration or an authentication
failure. Keep the user in the existing `/mindie-agent` conversation; ordinary
tasks do not need another login check or choice.

1. Install `gh` if missing. Run
   `gh api --hostname github.com user --jq .login` with a bounded wait.
   Reuse a working expected account, including one supplied by `GH_TOKEN`,
   and proceed to the reuse checks in step 6 without another login prompt.
   Use `gh auth status` for diagnosis, not as the sole gate: an unrelated
   expired saved account does not invalidate the active account.
2. If login is needed, prefer token login when the user reports an enterprise
   network, proxy/custom CA settings are present, or browser authorization is
   blocked. Otherwise default to browser login. These are hints, not a claim
   that every corporate network blocks browser login; respect an explicit
   user choice. Inspect only setting presence, never print proxy credentials.
   Network timeouts, proxy 407 and certificate failures need transport repair,
   not a replacement GitHub token. Keep TLS verification enabled.
3. Browser choice: run
   `gh auth login --hostname github.com --git-protocol https --web` in a
   user-operable terminal. If authorization is blocked, offer the token route.
4. Token choice: reuse an already-supplied token source. Otherwise ask only
   whether the user has a token or needs to create one. For creation, open
   [GitHub's token page](https://github.com/settings/tokens/new?scopes=repo%2Cread%3Aorg%2Cgist&description=MindIE%20Agent).
   The standard persistent flow uses a classic PAT with `repo`, `read:org`
   and `gist`, as required by `gh`; expiry and SSO authorization follow the
   user's organization policy. If policy requires a fine-grained PAT, use it
   with permissions for the actual repositories and operations.
5. Receive the token through `gh`, not chat. In a user-operable terminal run
   `gh auth login --hostname github.com --git-protocol https` and select
   `Paste an authentication token`; let the user paste at the hidden prompt.
   If interactive input is unavailable, accept a private token-file path and
   feed its contents to the following command through stdin:
   `gh auth login --hostname github.com --git-protocol https --with-token`.
   On PowerShell use `Get-Content -Raw -LiteralPath $TokenFile | gh auth login --hostname github.com --git-protocol https --with-token`
   and check `$LASTEXITCODE`; on POSIX use input redirection `< "$TOKEN_FILE"`.
   Bind the path variable to the user-supplied path. Never put token contents
   in command arguments, chat, logs, repositories or MindIE settings. Let `gh`
   manage storage without `--insecure-storage`. A stale `GH_TOKEN` or
   `GITHUB_TOKEN` overrides saved login: repair that caller's credential
   source rather than repeatedly logging in under the stale environment.
6. Verify the expected account again and ensure `git`'s directory is on `PATH`
   (since `gh` shells out to `git`). Never modify user or system gitconfig files
   on disk (`~/.gitconfig` or `/etc/gitconfig`) or rewrite persistent Git
   credential helpers; instead, inject GitHub credential and transport settings
   into the active process environment via `GIT_CONFIG_COUNT`
   (`credential.https://github.com.provider=github` and
   `credential.https://github.com.helper=!gh auth git-credential`) and
   `GCM_INTERACTIVE=never` so GUI dialogs cannot block non-interactive commands.
   If Windows `schannel` behind a corporate proxy fails with
   `CRYPT_E_NO_REVOCATION_CHECK`, append `http.sslBackend=schannel` and
   `http.schannelCheckRevoke=false` in the process-scoped `GIT_CONFIG_*` pairs
   while keeping `http.sslVerify=true`, and read the actual target repository
   before continuing. For saved login, also verify in a child
   process without `GH_TOKEN`/`GITHUB_TOKEN`, using the same user and
   `GH_CONFIG_DIR`. Handle 401 as credential expiry/rejection and investigate
   repository access, permissions or SSO for 403/404. Do not create test
   PRs/Issues or replay an uncertain write to test authentication.

Future Git/API operations reuse the same `gh` credentials. Keep existing
contribution consent, account binding and project scope. If the account differs,
resolve that mismatch before changing an existing contribution destination.

For environment-only authentication (preferred by `gh` for fine-grained PATs),
the user's existing secret injection must supply every actual caller.
Knowledge already forwards its one-use launch environment. The diagnostic
service's generated network file excludes tokens; independent workers require
saved `gh` login or their existing explicit private environment-file binding.
A successful terminal request does not establish authentication after restart.
Use the existing setup/status flow to verify the worker context; report the
account and completed setup steps, never the token. Detailed transport ownership:
[enterprise network design](https://github.com/mindie-agent/mindie-agent/blob/main/docs/enterprise-network.en.md).
