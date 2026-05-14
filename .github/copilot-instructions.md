# Copilot Instructions for Trader

## Project Context
- This repository contains an AI-powered Trading Bot.
- Core domains:
  - Fetch news
  - Infere trading recommendations
  - Execute trades via IBKR API
  - UI for monitoring and manual overrides
- Main source directory: src
- Tests directory: tests

## Design Documentation Map
- System overview and architecture decisions: `docs/design/overview.md`
- Data model index: `docs/design/data-model.md`
- Component-owned data model specs: `docs/design/news-fetcher/data-model.md`, `docs/design/analyzer-worker/data-model.md`, `docs/design/decision-executor/data-model.md`, `docs/design/shared/data-model.md`
- Configuration index: `docs/design/configuration.md`
- Component-owned configuration specs: `docs/design/news-fetcher/configuration.md`, `docs/design/analyzer-worker/configuration.md`, `docs/design/decision-executor/configuration.md`, `docs/design/shared/configuration.md`

When implementing features, prefer updating the smallest relevant design file above instead of adding implementation details to the overview.

## Architecture Boundaries
- UI layer: src/ui
- Service layer: src/services
- Repository layer: src/repositories
- Utilities and config: src/utils, src/config.py

When making changes, keep logic in the correct layer:
- UI files should orchestrate user interactions, not implement heavy business logic.
- Services should hold business rules and calculations.
- Repositories should handle database access only.
- Workflow code should coordinate steps and state transitions.

## Data and Storage
- SQLite

## Coding Guidelines
- Use Python 3.13 compatible code.
- Prefer small, focused functions with clear names.
- Reuse existing utilities and services before adding new abstractions.
- Avoid introducing global mutable state.
- Handle external API/network failures gracefully with actionable logs.
- Keep logging informative and concise; avoid noisy repeated warnings.
- Add comments only for non-obvious logic.

## Testing Requirements
- Add or update tests for all behavior changes.
- Prefer unit tests first; use integration tests when behavior depends on real APIs.
- Test commands:
  - Unit tests: uv run pytest -m "not integration"
  - Integration tests: uv run pytest -m integration
  - Full suite: uv run pytest

## Performance and Cost Awareness
- External APIs are rate limited and may incur costs.
- Minimize repeated fetching and redundant LLM calls when possible.

## Security and Secrets
- Never hardcode secrets.
- Read secrets from environment variables.
- Preserve existing environment variable conventions documented in README.

## Documentation Expectations
When behavior changes, update relevant docs in docs or README as needed.

## Issue Management

### Creating new issues for bugs or features

For non-trivial bug fixes or features, create an entry in docs/issues/issues-index.md with the status=new and a detail file under docs/issues/issues-detail with:
- clear problem statement
- verified evidence
- expected behavior
- acceptance criteria
- test plan

### Fixing the issue

When fixing an issue, update the corresponding detail file with:
- the root cause of the issue
- the steps taken to resolve it
- any remaining risks or follow-up actions

Once the issue is fixed, update the status in issues-index.md to resolved.

## Change Discipline
- Keep edits minimal and scoped to the requested task.
- Do not refactor unrelated modules in the same change.
- Preserve existing public interfaces unless explicitly requested to change them.
