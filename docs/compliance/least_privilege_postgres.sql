-- Least-privilege runtime role for the Floorwatch services.
-- Run as the database owner. Replace the password, then point the services'
-- DATABASE_URL at this role (keep the owner credentials for migrations only).

CREATE ROLE floorwatch_app LOGIN PASSWORD 'CHANGE-ME-long-random';

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO floorwatch_app;

-- Services create their own tables on first start (CREATE TABLE IF NOT EXISTS),
-- so either run them once as the owner first, or temporarily grant CREATE:
-- GRANT CREATE ON SCHEMA public TO floorwatch_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO floorwatch_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO floorwatch_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO floorwatch_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO floorwatch_app;

-- Defence in depth: cap runaway queries and idle transactions for this role.
ALTER ROLE floorwatch_app SET statement_timeout = '30s';
ALTER ROLE floorwatch_app SET idle_in_transaction_session_timeout = '60s';

-- No superuser, no CREATEDB/CREATEROLE, no DROP/TRUNCATE on tables owned by someone else.
