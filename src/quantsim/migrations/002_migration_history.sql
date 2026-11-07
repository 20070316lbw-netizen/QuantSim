CREATE TABLE main.schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name VARCHAR NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT current_timestamp,
    CHECK (version = 1 OR applied_at IS NOT NULL)
);
