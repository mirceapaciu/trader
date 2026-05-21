# Trader

AI-powered trading bot platform that:

- Fetches financial news
- Infers trading recommendations
- Executes trades through Interactive Brokers (IBKR) integrations
- Provides a UI for monitoring and manual overrides

The project one-pager is here: [Project one-pager](docs/one-pager.md)

## Project Status

Active development.

## Repository Structure

- src: application code
- tests: automated tests
- docs: design, architecture, configuration, and data model specifications
- scripts/deployment: local infrastructure helpers for PostgreSQL and Redis

For authoritative module ownership and boundaries, use the design docs instead of this summary.

## Core Architecture

Architecture boundaries and detailed ownership are maintained in

- [docs/design/overview](./docs/design/overview.md)

## Prerequisites

- Python 3.13
- uv package manager
- Docker (if you want to use the deployment scripts for PostgreSQL and Redis)

## Local Development

1. Start PostgreSQL:
   - PowerShell: scripts/deployment/postgres/start.ps1
   - Bash: scripts/deployment/postgres/start.sh
2. Start Redis:
   - PowerShell: scripts/deployment/redis/start.ps1
   - Bash: scripts/deployment/redis/start.sh
3. Install dependencies with your preferred Python workflow (uv recommended).
4. Run tests.

## Test Commands

- Unit tests: uv run pytest -m "not integration"
- Integration tests: uv run pytest -m integration
- Full suite: uv run pytest

## Licensing

This project uses a dual-license model:

- Open source license: GNU Affero General Public License v3.0 (AGPL-3.0-only)
- Commercial licensing: available via separate agreement

See LICENSE and COMMERCIAL-LICENSE.md for details.

## Disclaimer

Use of this software is at your own risk. The authors and contributors are not
liable for financial loss or any other damages resulting from use of this
project.

See DISCLAIMER.md for the full legal and financial disclaimer.

## Commercial Use

If your intended use is not compliant with AGPL obligations, contact the project owner for a commercial license agreement.