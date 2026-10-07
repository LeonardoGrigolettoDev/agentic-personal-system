#!/bin/bash
# Runs once, on an empty volume. Passwords are hex (gen-env.sh), so no quoting issues.
# Schema objects are NOT created here: they come from migrations/ (make migrate), so a fresh DB == migrations.
set -eo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<-EOSQL
	CREATE ROLE aios        LOGIN PASSWORD '${AIOS_DB_PASSWORD}';
	CREATE ROLE aios_reader LOGIN PASSWORD '${AIOS_READER_PASSWORD}';
	CREATE ROLE litellm     LOGIN PASSWORD '${LITELLM_DB_PASSWORD}';
	CREATE ROLE langfuse    LOGIN PASSWORD '${LANGFUSE_DB_PASSWORD}';
	CREATE DATABASE aios     OWNER aios;
	CREATE DATABASE litellm  OWNER litellm;
	CREATE DATABASE langfuse OWNER langfuse;
EOSQL
# extensions need superuser; create them up front so migrations run as the owner role
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d aios <<-EOSQL
	CREATE EXTENSION IF NOT EXISTS vector;
	CREATE EXTENSION IF NOT EXISTS pg_trgm;
	CREATE EXTENSION IF NOT EXISTS unaccent;
EOSQL
