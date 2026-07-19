# Production Env Templates

These templates are sanitized from the Haas production layout. They are meant to
bootstrap another production-like Docker Compose host without copying Haas
secrets or host-only drift.

Use them by copying each `*.template` file into the target env directory and
removing the `.template` suffix:

```bash
mkdir -p /home/gh-runner/trader-env
cp deploy/env-templates/prod/*.template /home/gh-runner/trader-env/
for f in /home/gh-runner/trader-env/*.template; do mv "$f" "${f%.template}"; done
```

Then fill every `<REQUIRED_...>` placeholder before deploying.

Notes:

- `IBKR_HOST=host.docker.internal` and `IBKR_PORT=4002` match a host-running
  paper IB Gateway from Docker containers.
- The Compose file overrides container network addresses for Postgres, Redis,
  and Monitoring UI runtime binding.
- Do not commit filled env files.
