"""Dry-run-first, read-only Aiven MySQL to PostgreSQL data transfer utility."""

import argparse
import hashlib
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import mysql.connector
import psycopg
from psycopg import sql

from neon_target import NeonTargetError, verify_neon_target


PROGRESS_TABLE = "smartprint_migration_progress"
MAX_BATCH_SIZE = 10_000


class MigrationError(RuntimeError):
    """A safe-to-report migration failure that contains no row values."""


def _mysql_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _cell_bytes(value: object) -> bytes:
    if value is None:
        return b"null:"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return b"datetime:" + value.isoformat().encode("utf-8")
    if isinstance(value, date):
        return b"date:" + value.isoformat().encode("utf-8")
    if isinstance(value, Decimal):
        return b"decimal:" + str(value).encode("ascii")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"bytes:" + bytes(value)
    if isinstance(value, bool):
        return b"integer:" + (b"1" if value else b"0")
    if isinstance(value, int):
        return b"integer:" + str(value).encode("ascii")
    return f"{type(value).__name__}:".encode("ascii") + str(value).encode("utf-8")


def _update_digest(digest, row: tuple[object, ...]) -> None:
    for value in row:
        encoded = _cell_bytes(value)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def _read_source_metadata(cursor, database: str):
    cursor.execute(
        """
        SELECT table_name, engine
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (database,),
    )
    tables = cursor.fetchall()
    if not tables:
        raise MigrationError("The Aiven source database contains no base tables.")

    source_tables = {name: engine for name, engine in tables}
    if any((engine or "").lower() != "innodb" for engine in source_tables.values()):
        raise MigrationError("All source tables must use InnoDB for a consistent read-only snapshot.")

    cursor.execute(
        """
        SELECT table_name, column_name, extra
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (database,),
    )
    columns = {}
    auto_increment = {}
    for table_name, column_name, extra in cursor.fetchall():
        columns.setdefault(table_name, []).append(column_name)
        if "auto_increment" in (extra or "").lower():
            auto_increment.setdefault(table_name, []).append(column_name)

    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.key_column_usage
        WHERE table_schema = %s AND constraint_name = 'PRIMARY'
        ORDER BY table_name, ordinal_position
        """,
        (database,),
    )
    primary_keys = {}
    for table_name, column_name in cursor.fetchall():
        primary_keys.setdefault(table_name, []).append(column_name)

    cursor.execute(
        """
        SELECT table_name, referenced_table_name
        FROM information_schema.key_column_usage
        WHERE table_schema = %s AND referenced_table_name IS NOT NULL
        """,
        (database,),
    )
    dependencies = {table: set() for table in source_tables}
    for table_name, referenced_table in cursor.fetchall():
        if referenced_table in source_tables and referenced_table != table_name:
            dependencies[table_name].add(referenced_table)

    missing_keys = set(source_tables) - set(primary_keys)
    if missing_keys:
        raise MigrationError(
            "Source tables without primary keys cannot be safely resumed: "
            + ", ".join(sorted(missing_keys))
        )

    ordered_tables = []
    remaining = {table: set(required) for table, required in dependencies.items()}
    while remaining:
        ready = sorted(table for table, required in remaining.items() if not required)
        if not ready:
            raise MigrationError("The source schema has cyclic foreign-key dependencies.")
        for table in ready:
            ordered_tables.append(table)
            remaining.pop(table)
        for required in remaining.values():
            required.difference_update(ready)

    return ordered_tables, columns, primary_keys, auto_increment


def _read_target_metadata(cursor):
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """
    )
    tables = {row[0] for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT table_name, column_name, is_nullable, column_default, is_identity
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )
    columns = {}
    column_properties = {}
    for table, column, nullable, default, is_identity in cursor.fetchall():
        columns.setdefault(table, []).append(column)
        column_properties[(table, column)] = (nullable, default, is_identity)
    cursor.execute(
        """
        SELECT kcu.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_catalog = tc.constraint_catalog
         AND kcu.constraint_schema = tc.constraint_schema
         AND kcu.constraint_name = tc.constraint_name
         AND kcu.table_name = tc.table_name
        WHERE tc.table_schema = 'public'
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.table_name, kcu.ordinal_position
        """
    )
    primary_keys = {}
    for table, column in cursor.fetchall():
        primary_keys.setdefault(table, []).append(column)
    return tables, columns, column_properties, primary_keys


def _validate_schema(
    source_tables,
    source_columns,
    source_primary_keys,
    target_tables,
    target_columns,
    target_properties,
    target_primary_keys,
):
    errors = []
    for table in source_tables:
        if table not in target_tables:
            errors.append(f"{table}: missing target table")
            continue
        missing_columns = set(source_columns[table]) - set(target_columns.get(table, []))
        if missing_columns:
            errors.append(f"{table}: missing target columns {', '.join(sorted(missing_columns))}")
        if source_primary_keys[table] != target_primary_keys.get(table):
            errors.append(f"{table}: primary-key definition differs")
        for column in set(target_columns.get(table, [])) - set(source_columns[table]):
            nullable, default, is_identity = target_properties[(table, column)]
            if nullable == "NO" and default is None and is_identity != "YES":
                errors.append(f"{table}.{column}: target-only required column has no default")
    if errors:
        raise MigrationError("Schema preflight failed: " + "; ".join(errors))


def _count_source_rows(cursor, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {_mysql_identifier(table)}")
    return int(cursor.fetchone()[0])


def _count_target_rows(cursor, table: str) -> int:
    cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
    return int(cursor.fetchone()[0])


def _source_row_cursor(source_connection, table: str, columns: list[str], primary_key: list[str]):
    statement = (
        "SELECT "
        + ", ".join(_mysql_identifier(column) for column in columns)
        + " FROM "
        + _mysql_identifier(table)
        + " ORDER BY "
        + ", ".join(_mysql_identifier(column) for column in primary_key)
    )
    cursor = source_connection.cursor()
    cursor.execute(statement)
    return cursor


def _target_digest(cursor, table: str, columns: list[str], primary_key: list[str], batch_size: int):
    digest = hashlib.sha256()
    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, primary_key)),
    )
    cursor.execute(query)
    while rows := cursor.fetchmany(batch_size):
        for row in rows:
            _update_digest(digest, row)
    return digest.hexdigest()


def _reset_identity_sequences(cursor, table: str, columns: list[str]) -> None:
    for column in columns:
        cursor.execute("SELECT pg_get_serial_sequence(%s, %s)", (f"public.{table}", column))
        sequence = cursor.fetchone()[0]
        if not sequence:
            continue
        cursor.execute(sql.SQL("SELECT MAX({}) FROM {}").format(
            sql.Identifier(column), sql.Identifier(table)
        ))
        maximum = cursor.fetchone()[0]
        if maximum is None:
            cursor.execute("SELECT setval(%s, 1, false)", (sequence,))
        else:
            cursor.execute("SELECT setval(%s, %s, true)", (sequence, maximum))


def transfer_data(
    source_connection,
    target_connection,
    *,
    source_database: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    source_cursor = source_connection.cursor()
    try:
        tables, source_columns, source_primary_keys, auto_increment = _read_source_metadata(
            source_cursor, source_database
        )
        with target_connection.cursor() as target_cursor:
            (
                target_tables,
                target_columns,
                target_properties,
                target_primary_keys,
            ) = _read_target_metadata(target_cursor)
            _validate_schema(
                tables,
                source_columns,
                source_primary_keys,
                target_tables,
                target_columns,
                target_properties,
                target_primary_keys,
            )
        target_connection.commit()

        print("Table preflight (source rows / target rows):")
        for table in tables:
            source_count = _count_source_rows(source_cursor, table)
            with target_connection.cursor() as target_cursor:
                target_count = _count_target_rows(target_cursor, table)
            print(f"  {table}: {source_count} / {target_count}")
        if dry_run:
            print("Dry run only: no target rows or schema were changed.")
            return
        target_connection.commit()

        with target_connection.transaction():
            with target_connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {PROGRESS_TABLE} (
                        source_database TEXT NOT NULL,
                        table_name TEXT NOT NULL,
                        source_count BIGINT NOT NULL,
                        source_sha256 CHAR(64) NOT NULL,
                        destination_count BIGINT NOT NULL,
                        completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (source_database, table_name)
                    )
                    """
                )

        for table in tables:
            source_count = _count_source_rows(source_cursor, table)
            columns = source_columns[table]
            primary_key = source_primary_keys[table]
            with target_connection.transaction():
                with target_connection.cursor() as target_cursor:
                    target_count = _count_target_rows(target_cursor, table)
                    target_cursor.execute(
                        f"""
                        SELECT source_count, source_sha256, destination_count
                        FROM {PROGRESS_TABLE}
                        WHERE source_database = %s AND table_name = %s
                        """,
                        (source_database, table),
                    )
                    progress = target_cursor.fetchone()

                    if progress:
                        if target_count != progress[2] or target_count != source_count:
                            raise MigrationError(
                                f"{table}: saved migration progress no longer matches table counts"
                            )
                        source_cursor_rows = _source_row_cursor(
                            source_connection, table, columns, primary_key
                        )
                        source_digest = hashlib.sha256()
                        try:
                            while rows := source_cursor_rows.fetchmany(batch_size):
                                for row in rows:
                                    _update_digest(source_digest, row)
                        finally:
                            source_cursor_rows.close()
                        destination_digest = _target_digest(
                            target_cursor, table, columns, primary_key, batch_size
                        )
                        if (
                            source_digest.hexdigest() != progress[1]
                            or destination_digest != progress[1]
                        ):
                            raise MigrationError(f"{table}: saved rows differ from the source snapshot")
                        print(f"  {table}: already verified; skipped")
                        continue

                    if target_count != 0:
                        raise MigrationError(
                            f"{table}: target contains untracked rows; refusing to merge or overwrite"
                        )

                    source_rows = _source_row_cursor(
                        source_connection, table, columns, primary_key
                    )
                    insert_statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(map(sql.Identifier, columns)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                    )
                    source_digest = hashlib.sha256()
                    transferred = 0
                    try:
                        while rows := source_rows.fetchmany(batch_size):
                            for row in rows:
                                _update_digest(source_digest, row)
                            target_cursor.executemany(insert_statement, rows)
                            transferred += len(rows)
                    finally:
                        source_rows.close()

                    if transferred != source_count:
                        raise MigrationError(f"{table}: source row count changed during transfer")

                    _reset_identity_sequences(
                        target_cursor, table, auto_increment.get(table, [])
                    )
                    destination_count = _count_target_rows(target_cursor, table)
                    destination_digest = _target_digest(
                        target_cursor, table, columns, primary_key, batch_size
                    )
                    source_sha256 = source_digest.hexdigest()
                    if destination_count != source_count or destination_digest != source_sha256:
                        raise MigrationError(f"{table}: post-transfer count or checksum mismatch")

                    target_cursor.execute(
                        f"""
                        INSERT INTO {PROGRESS_TABLE}
                            (source_database, table_name, source_count, source_sha256, destination_count)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            source_database,
                            table,
                            source_count,
                            source_sha256,
                            destination_count,
                        ),
                    )
            print(f"  {table}: transferred and verified {source_count} rows")
    finally:
        source_cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write verified rows to Neon.")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--target-environment",
        choices=("test", "production"),
        help="Required with --apply; production requires --confirm-production.",
    )
    parser.add_argument("--neon-project-id", help="Neon project ID owning the target branch.")
    parser.add_argument("--neon-branch-id", help="Neon branch ID targeted by this transfer.")
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="Explicitly authorize writes to the production target.",
    )
    args = parser.parse_args()
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        parser.error(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}.")
    if args.apply and not args.target_environment:
        parser.error("--apply requires an explicit --target-environment.")
    if args.apply and args.target_environment == "production" and not args.confirm_production:
        parser.error("production requires --confirm-production.")
    if args.confirm_production and args.target_environment != "production":
        parser.error("--confirm-production is only valid for a production target.")
    if args.apply and not args.neon_project_id:
        parser.error("--apply requires --neon-project-id.")
    if args.apply and not args.neon_branch_id:
        parser.error("--apply requires --neon-branch-id.")

    source_database = os.getenv("SMARTPRINT_DB_NAME", "").strip()
    source_host = os.getenv("SMARTPRINT_DB_HOST", "").strip()
    source_user = os.getenv("SMARTPRINT_DB_USER", "").strip()
    source_password = os.getenv("SMARTPRINT_DB_PASSWORD")
    source_ca = os.getenv("SMARTPRINT_DB_SSL_CA", "").strip()
    target_url = os.getenv("DATABASE_URL_UNPOOLED", "").strip()
    if not all((source_database, source_host, source_user, source_password, source_ca)):
        parser.error(
            "Set SMARTPRINT_DB_HOST, SMARTPRINT_DB_USER, SMARTPRINT_DB_PASSWORD, "
            "SMARTPRINT_DB_NAME, and SMARTPRINT_DB_SSL_CA."
        )
    if not target_url:
        parser.error("Set DATABASE_URL_UNPOOLED to the direct Neon PostgreSQL URL.")
    if args.apply:
        try:
            verify_neon_target(
                target_url,
                args.target_environment,
                args.neon_project_id,
                args.neon_branch_id,
            )
        except NeonTargetError as error:
            print(f"Data migration blocked: {error}", file=sys.stderr)
            return 1

    source_connection = None
    target_connection = None
    try:
        source_connection = mysql.connector.connect(
            host=source_host,
            port=int(os.getenv("SMARTPRINT_DB_PORT", "3306")),
            user=source_user,
            password=source_password,
            database=source_database,
            ssl_ca=source_ca,
            ssl_verify_cert=True,
            ssl_verify_identity=True,
            connection_timeout=15,
        )
        source_connection.start_transaction(
            isolation_level="REPEATABLE READ",
            readonly=True,
            consistent_snapshot=True,
        )
        target_connection = psycopg.connect(
            target_url,
            connect_timeout=15,
            sslmode="require",
            options="-c timezone=UTC",
        )
        transfer_data(
            source_connection,
            target_connection,
            source_database=source_database,
            batch_size=args.batch_size,
            dry_run=not args.apply,
        )
    except MigrationError as error:
        print(f"Data migration failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"Data migration failed: {type(error).__name__}; "
            "database details, row values, and credentials were omitted.",
            file=sys.stderr,
        )
        return 1
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.rollback()
            source_connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
