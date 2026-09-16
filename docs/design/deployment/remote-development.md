# Remote development on Ubuntu 24.04

This runbook provisions a blank Ubuntu 24.04 LTS server as an isolated Trader
development host and self-hosted GitHub Actions runner. It supports the project
issue workflow documented in `AGENTS.md`. It does not start TradeExecutor.

## Required inputs

The initial SSH account must use key authentication and have passwordless or
interactive `sudo` access. Keep all credential files outside the repository.

Copy `scripts/development/development.conf.example` and set:

```bash
DEV_SERVER_HOST="<server-ip-or-hostname>"
DEV_SERVER_SSH_PORT="22"
DEV_SERVER_USER="<ubuntu-sudo-user>"
DEV_SERVER_SSH_KEY_PATH="<local-path-to-private-ssh-key>"
EXPECTED_UBUNTU_VERSION="24.04"
GITHUB_REPOSITORY="https://github.com/mirceapaciu/trader.git"
GITHUB_TOKEN_FILE="<local-path-to-github-token-file>"
```

The GitHub token must be able to clone and push the repository, manage
self-hosted runners, and create pull requests. The bootstrap uploads it to a
root-owned staging directory and installs it with mode `0600`.

## Install

From a Bash environment on the administrator's machine:

```bash
bash scripts/development/remote-bootstrap.sh /secure/path/development.conf
```

The launcher uploads the versioned setup bundle using `scp`, then invokes the
remote bootstrap over SSH. The bootstrap verifies Ubuntu 24.04, installs system
tools, Docker, Node 22, uv, GitHub CLI, Bubblewrap, the Ubuntu 24.04 Bubblewrap
AppArmor profile required by the Codex Linux sandbox, and a uv-managed Python
3.14 environment. It creates the `trader-dev` account, clones the repository
under `/opt/trader-dev/repository`, starts PostgreSQL and Redis, registers the
`trader-dev` runner, and verifies Bubblewrap user/network namespace creation
before running unit tests, infrastructure integration tests, and the frontend
build. It does not authenticate Codex on your behalf.

After bootstrap, authenticate the dedicated runner account using the device
flow and your ChatGPT subscription:

```bash
sudo -u trader-dev /opt/trader-dev/setup-codex-subscription.sh
```

The command shows a device code and verification URL. Complete it in your
browser, then confirm the CLI reports the ChatGPT login. The issue workflow
uses that stored login through `codex exec`; it does not use an OpenAI API key.

The scripts are idempotent. Re-running bootstrap updates the manual checkout,
preserves database volumes and generated credentials, and reuses the registered
runner. It does not reset a dirty manual checkout; clean or preserve that work
before updating.

## Operations

Run these on the server as `trader-dev`, passing the configuration path only if
it differs from `/opt/trader-dev/development.conf`:

```bash
/opt/trader-dev/status.sh
/opt/trader-dev/stop-services.sh
/opt/trader-dev/start-services.sh
/opt/trader-dev/update.sh
/opt/trader-dev/verify.sh /opt/trader-dev/development.conf
```

Reset is intentionally explicit because it deletes the development PostgreSQL
and Redis volumes before recreating them:

```bash
/opt/trader-dev/reset-services.sh --confirm-delete-development-data
```

Forward the monitoring UI ports when running it manually:

```bash
ssh -i <private-key> -L 8090:127.0.0.1:8090 -L 5174:127.0.0.1:5174 <user>@<server>
```

To rerun the integration tests independently:

```bash
cd /opt/trader-dev/repository
uv run pytest -m integration
```

Paid LLM evaluations remain opt-in. Production credentials must never be copied
to this host.

## Issue-to-PR workflow

`.github/workflows/implement-issue.yml` runs only for the `codex:ready` label or
a manual retry. It requires exactly one `Project issue: YYMMDD-XX` line, a `new`
index entry on `main`, a matching detail file, and all required sections. It
creates or reuses `codex/YYMMDD-XX`, asks Codex to implement the detail, verifies
the result outside Codex's sandbox, marks the project issue resolved only after
that verification succeeds, and creates or updates one pull request.

Configure the `CI / unit-and-frontend` check as a required branch-protection
check and require human review before merging. A merge to `main` triggers the
existing Haas production deployment, so do this before adding `codex:ready` to
an issue.

If a run fails, correct the issue record or implementation cause and use the
manual workflow with the same GitHub issue number. The branch and pull request
are reused. Runner logs are available in GitHub Actions and through the runner's
systemd service journal.

Codex subscription usage exhaustion is handled separately from ordinary
failures. The implementation workflow commits and pushes the current working
tree to the issue branch, applies the `codex:retry` label to the GitHub issue,
and fails the run so the interruption remains visible. The
`retry-codex-issues.yml` workflow checks twice an hour for that label, removes
it, and dispatches the implementation workflow with the same GitHub issue
number. If the allowance has not reset, the new run checkpoints again and
reapplies the label; after a successful verified implementation, the label is
removed. Do not remove the label manually unless automatic retries should stop.

If a Codex run reports `bwrap: loopback: Failed RTM_NEWADDR`, rerun the bootstrap.
It installs and reloads the Ubuntu 24.04 `bwrap-userns-restrict` AppArmor profile
without disabling the host-wide unprivileged-user-namespace restriction.
