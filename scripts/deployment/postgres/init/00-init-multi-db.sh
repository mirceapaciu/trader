#!/usr/bin/env bash
set -euo pipefail

PROD_DB="${POSTGRES_DB:-trader}"
TEST_DB="${POSTGRES_TEST_DB:-${PROD_DB}_test}"
PGUSER="${POSTGRES_USER:-trader}"

psql -v ON_ERROR_STOP=1 --username "$PGUSER" --dbname postgres <<-EOSQL
SELECT format('CREATE DATABASE %I', '${TEST_DB}')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${TEST_DB}')\gexec
EOSQL

init_schemas() {
  local db_name="$1"
  psql -v ON_ERROR_STOP=1 --username "$PGUSER" --dbname "$db_name" <<-'EOSQL'
CREATE SCHEMA IF NOT EXISTS news_fetcher;
CREATE SCHEMA IF NOT EXISTS analyzer_worker;
CREATE SCHEMA IF NOT EXISTS trade_executor;
CREATE SCHEMA IF NOT EXISTS shared;
EOSQL
}

init_schemas "$PROD_DB"

if [[ "$TEST_DB" != "$PROD_DB" ]]; then
  init_schemas "$TEST_DB"
fi
