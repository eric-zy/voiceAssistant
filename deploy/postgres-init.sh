#!/bin/sh
# Runs once, when the PostgreSQL container first creates its data directory (docker-entrypoint-initdb.d).
# Creates the role the app connects as. It owns the database, so it can create its tables, but it is not a
# superuser and cannot bypass row-level security: that is what keeps one user's rows away from another's.
set -eu
: "${VOICE_DB_PASSWORD:?set VOICE_DB_PASSWORD in .env}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$VOICE_DB_PASSWORD" -v dbname="$POSTGRES_DB" <<'SQL'
CREATE ROLE voice_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'app_password';
ALTER DATABASE :"dbname" OWNER TO voice_app;
ALTER SCHEMA public OWNER TO voice_app;
SQL
