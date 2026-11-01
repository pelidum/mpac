import asyncio
import os
import subprocess

import asyncpg
import harness
import pytest

DOMAIN_TABLES = frozenset(
    {
        "attachments",
        "backends",
        "benchmarks",
        "test_items",
        "test_run_answers",
        "test_runs",
        "tests",
        "user_tpm_windows",
        "users",
    }
)

BLOB_URI_INDEX = (
    "attachments",
    "attachments_blob_uri_idx",
    "CREATE INDEX attachments_blob_uri_idx ON public.attachments USING btree (((proto_jsonb ->> 'uri'::text)))",
)


CATALOG_QUERIES = {
    "columns": """
        SELECT table_name, ordinal_position, column_name, data_type, is_nullable,
               column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
        ORDER BY tablename, indexname
    """,
    "constraints": """
        SELECT rel.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname = 'public'
        ORDER BY rel.relname, con.conname
    """,
}


def _query(pg, db_name, sql, *args):
    async def run():
        conn = await harness.connect(pg, db_name)
        try:
            return [tuple(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(run())


def _catalog(pg, db_name, tables):
    return {
        kind: [row for row in _query(pg, db_name, sql) if row[0] in tables]
        for kind, sql in CATALOG_QUERIES.items()
    }


def _tables(pg, db_name):
    rows = _query(
        pg,
        db_name,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    )
    return {row[0] for row in rows}


def _applied_versions(pg, db_name):
    rows = _query(
        pg,
        db_name,
        "SELECT version FROM _sqlx_migrations WHERE success ORDER BY version",
    )
    return [row[0] for row in rows]


def _mpac(pg, db_name, *args):
    return subprocess.Popen(
        [harness.runfile("MPAC_CLI_BIN"), *args, *harness.db_flags(pg, db_name)],
        env=os.environ | {"MPAC_DB_PASSWORD": pg.password},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _migrate(pg, db_name):
    process = _mpac(pg, db_name, "migrate")
    output, _ = process.communicate(timeout=120)
    assert process.returncode == 0, output
    return output


@pytest.fixture(scope="module")
def pg():
    with harness.postgres() as pg:
        yield pg


def test_fresh_migration_creates_every_table(pg):
    with harness.database(pg) as db_name:
        _migrate(pg, db_name)

        assert DOMAIN_TABLES <= _tables(pg, db_name)
        assert _applied_versions(pg, db_name)[0] == 1


def test_migrate_is_idempotent(pg):
    with harness.database(pg) as db_name:
        _migrate(pg, db_name)
        first = _catalog(pg, db_name, tables=_tables(pg, db_name))
        versions = _applied_versions(pg, db_name)

        _migrate(pg, db_name)

        assert _catalog(pg, db_name, tables=_tables(pg, db_name)) == first
        assert _applied_versions(pg, db_name) == versions


def test_concurrent_migrations_are_safe(pg):
    with harness.database(pg) as db_name:
        processes = [_mpac(pg, db_name, "migrate") for _ in range(4)]
        results = [(p.wait(timeout=120), p.stdout.read()) for p in processes]

        assert [code for code, _ in results] == [0] * 4, results
        assert _applied_versions(pg, db_name)[0] == 1


def test_server_runs_on_a_migrated_database(pg):
    with harness.database(pg) as db_name:
        _migrate(pg, db_name)
        with harness.server(pg, db_name) as address:
            assert address


def test_queue_schema_defaults_and_constraints(pg):
    with harness.database(pg) as db_name:
        _migrate(pg, db_name)
        job = _query(
            pg,
            db_name,
            "INSERT INTO jobs (kind, run_id, payload) VALUES ('answer_item', 'r', '\\x08')"
            " RETURNING status, attempts, max_attempts, run_after <= NOW(), id IS NOT NULL",
        )
        assert job == [("queued", 0, 3, True, True)]

        rejected = [
            "INSERT INTO jobs (kind, payload, status) VALUES ('x', '', 'bogus')",
            "INSERT INTO jobs (kind, payload, status) VALUES ('x', '', 'leased')",
            "INSERT INTO jobs (kind, payload, status, leased_by) VALUES ('x', '', 'leased', 'w1')",
            "INSERT INTO jobs (kind, payload, leased_by) VALUES ('x', '', 'w1')",
            "INSERT INTO jobs (kind, payload, leased_until) VALUES ('x', '', NOW())",
            "INSERT INTO jobs (kind, payload, status, leased_by, leased_until) VALUES ('x', '', 'done', 'w1', NOW())",
            "INSERT INTO jobs (kind, payload, max_attempts) VALUES ('x', '', 0)",
            "INSERT INTO jobs (kind) VALUES ('x')",
            "INSERT INTO run_counters (run_id, total, remaining) VALUES ('r', 1, 2)",
            "INSERT INTO run_counters (run_id, total, remaining) VALUES ('r', 1, -1)",
        ]
        for sql in rejected:
            with pytest.raises(asyncpg.IntegrityConstraintViolationError):
                _query(pg, db_name, sql)

        leased = _query(
            pg,
            db_name,
            "UPDATE jobs SET status = 'leased', leased_by = 'w1',"
            " leased_until = NOW() + interval '1 minute' RETURNING status",
        )
        assert leased == [("leased",)]

        lease_checks = [
            row[1]
            for row in _query(pg, db_name, CATALOG_QUERIES["constraints"])
            if row[0] == "jobs" and "leased_by" in row[3]
        ]
        assert lease_checks == ["jobs_lease_check"]


def test_blob_cleanup_queue_and_reference_index(pg):
    with harness.database(pg) as db_name:
        _migrate(pg, db_name)
        assert "blob_cleanup" in _tables(pg, db_name)
        assert BLOB_URI_INDEX in _query(pg, db_name, CATALOG_QUERIES["indexes"])
        _query(
            pg,
            db_name,
            "INSERT INTO blob_cleanup (key) VALUES ('blobs/orphan') ON CONFLICT DO NOTHING",
        )
        _query(
            pg,
            db_name,
            "INSERT INTO blob_cleanup (key) VALUES ('blobs/orphan') ON CONFLICT DO NOTHING",
        )
        assert _query(
            pg, db_name, "SELECT key, created_at IS NOT NULL FROM blob_cleanup"
        ) == [("blobs/orphan", True)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
