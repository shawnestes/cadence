-- unsub_server/sql/create_role.sql
-- Least-privilege role for the standalone unsub server.
-- Run as a superuser/owner: psql -v pw='<generated password>' -f create_role.sql
CREATE ROLE unsub_server LOGIN PASSWORD :'pw';

GRANT CONNECT ON DATABASE cadence TO unsub_server;
GRANT USAGE ON SCHEMA public TO unsub_server;

GRANT SELECT (id, tenant, email, status), UPDATE (status, updated_at)
  ON recipients TO unsub_server;
GRANT SELECT, INSERT ON suppressions TO unsub_server;
GRANT SELECT, INSERT ON unsub_outbox TO unsub_server;
