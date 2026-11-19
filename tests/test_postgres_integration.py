import os
import unittest
import uuid
import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import psycopg
from psycopg import errors
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("SMARTPRINT_ENV", "development")

import main
from migrate_postgres import run_migrations

MYSQL_AVAILABLE = importlib.util.find_spec("mysql") is not None
if MYSQL_AVAILABLE:
    from migrate_aiven_to_neon import transfer_data
else:
    transfer_data = None


DATABASE_URL = os.getenv("SMARTPRINT_TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    DATABASE_URL,
    "Set SMARTPRINT_TEST_DATABASE_URL to an isolated Neon test-branch URL.",
)
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations(DATABASE_URL, dry_run=False)
        run_migrations(DATABASE_URL, dry_run=False)

    def setUp(self):
        self.connection = psycopg.connect(
            DATABASE_URL,
            connect_timeout=15,
            sslmode="require",
            options="-c timezone=UTC",
        )

    def tearDown(self):
        self.connection.rollback()
        self.connection.close()

    def test_application_connection_uses_postgresql_url(self):
        with patch.object(main, "DATABASE_URL", DATABASE_URL):
            connection = main.get_db_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                self.assertTrue(cursor.fetchone()[0])
        finally:
            connection.close()

    def test_owner_registration_and_case_insensitive_login(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/register/",
                "headers": [],
                "client": ("192.0.2.10", 12345),
                "server": ("testserver", 80),
                "scheme": "http",
                "query_string": b"",
                "http_version": "1.1",
            }
        )
        owner_id = None
        try:
            with (
                patch.object(main, "DATABASE_URL", DATABASE_URL),
                patch.object(main, "_check_registration_rate_limit"),
            ):
                registration = main.register_owner(
                    request,
                    shop_name="Integration test",
                    username=username,
                    password="test-password-123",
                )
            self.assertEqual(registration["status"], "success")
            owner_id = registration["owner_id"]

            with (
                patch.object(main, "DATABASE_URL", DATABASE_URL),
                patch.object(main, "_check_login_rate_limit"),
            ):
                login = main.login_owner(
                    request,
                    username=username.upper(),
                    password="test-password-123",
                )
            self.assertEqual(login.status_code, 200)
            self.assertIn(main.SESSION_COOKIE_NAME, login.headers["set-cookie"])
        finally:
            if owner_id is not None:
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM shop_owners WHERE owner_id = %s",
                        (owner_id,),
                    )
                self.connection.commit()

    def test_parameterized_crud_and_rollback(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shop_owners (shop_name, username, password)
                VALUES (%s, %s, %s)
                RETURNING owner_id
                """,
                ("Integration test", username, "test-only-hash"),
            )
            owner_id = cursor.fetchone()[0]
            cursor.execute(
                "SELECT username FROM shop_owners WHERE owner_id = %s",
                (owner_id,),
            )
            self.assertEqual(cursor.fetchone()[0], username)
        self.connection.rollback()

        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT owner_id FROM shop_owners WHERE username = %s",
                (username,),
            )
            self.assertIsNone(cursor.fetchone())

    def test_foreign_key_and_unique_constraints(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO shop_owners (shop_name, username, password) VALUES (%s, %s, %s)",
                ("Integration test", username, "test-only-hash"),
            )

        with self.assertRaises(errors.UniqueViolation):
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO shop_owners (shop_name, username, password) VALUES (%s, %s, %s)",
                        ("Duplicate", username.upper(), "test-only-hash"),
                    )

        with self.assertRaises(errors.ForeignKeyViolation):
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO customers (name, owner_id) VALUES (%s, %s)",
                        ("Orphan", -1),
                    )

    def test_sql_injection_payload_is_treated_as_data(self):
        payload = "' OR TRUE --"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT owner_id FROM shop_owners WHERE username = %s",
                (payload,),
            )
            self.assertIsNone(cursor.fetchone())

    def test_owner_login_lookup_is_case_insensitive(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO shop_owners (shop_name, username, password) VALUES (%s, %s, %s) RETURNING owner_id",
                ("Integration test", username, "test-only-hash"),
            )
            owner_id = cursor.fetchone()[0]
            cursor.execute(
                "SELECT owner_id FROM shop_owners WHERE LOWER(username) = LOWER(%s)",
                (username.upper(),),
            )
            self.assertEqual(cursor.fetchone()[0], owner_id)

    def test_rate_limit_upsert_blocks_at_configured_limit(self):
        scope_name = f"postgres-integration:{uuid.uuid4().hex}"
        bucket_key = main._rate_limit_key(scope_name)
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/test/",
                "headers": [],
                "client": ("192.0.2.10", 12345),
                "server": ("testserver", 80),
                "scheme": "http",
                "query_string": b"",
                "http_version": "1.1",
            }
        )
        try:
            with patch.object(main, "DATABASE_URL", DATABASE_URL):
                main._consume_rate_limit(
                    request,
                    endpoint="integration_test",
                    scopes=[(scope_name, 1, 60)],
                )
                with self.assertRaises(HTTPException) as raised:
                    main._consume_rate_limit(
                        request,
                        endpoint="integration_test",
                        scopes=[(scope_name, 1, 60)],
                    )
            self.assertEqual(raised.exception.status_code, 429)
            self.assertIn("Retry-After", raised.exception.headers)
        finally:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM security_rate_limits WHERE bucket_key = %s",
                    (bucket_key,),
                )
            self.connection.commit()

    def test_updated_at_trigger_and_utc_analytics_expressions(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO shop_owners (shop_name, username, password) VALUES (%s, %s, %s) RETURNING owner_id",
                ("Integration test", username, "test-only-hash"),
            )
            owner_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO printing_profiles (owner_id, profile_name)
                VALUES (%s, %s)
                RETURNING profile_id
                """,
                (owner_id, f"profile-{uuid.uuid4().hex}"),
            )
            profile_id = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE printing_profiles SET updated_at = %s WHERE profile_id = %s",
                ("2000-01-01T00:00:00+00:00", profile_id),
            )
            cursor.execute(
                "SELECT updated_at FROM printing_profiles WHERE profile_id = %s",
                (profile_id,),
            )
            self.assertGreater(cursor.fetchone()[0].year, 2000)

            order_id = f"pg-{uuid.uuid4().hex[:40]}"
            cursor.execute(
                """
                INSERT INTO orders (order_id, owner_id, created_at, total_price, order_status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (order_id, owner_id, "2024-01-01T00:30:00+00:00", 12.50, "Completed"),
            )
            cursor.execute(
                """
                SELECT EXTRACT(MONTH FROM created_at AT TIME ZONE 'UTC')::integer AS month,
                       EXTRACT(YEAR FROM created_at AT TIME ZONE 'UTC')::integer AS year,
                       SUM(total_price) AS monthly_revenue,
                       COUNT(order_id) AS order_count
                FROM orders
                WHERE owner_id = %s AND order_status = 'Completed'
                GROUP BY EXTRACT(YEAR FROM created_at AT TIME ZONE 'UTC'),
                         EXTRACT(MONTH FROM created_at AT TIME ZONE 'UTC')
                """,
                (owner_id,),
            )
            row = cursor.fetchone()
            self.assertEqual(row, (1, 2024, Decimal("12.50"), 1))

    def test_inventory_update_from_preserves_owner_scope(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO shop_owners (shop_name, username, password) VALUES (%s, %s, %s) RETURNING owner_id",
                ("Integration test", username, "test-only-hash"),
            )
            owner_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO branches (owner_id, branch_name) VALUES (%s, %s) RETURNING branch_id",
                (owner_id, "Integration branch"),
            )
            branch_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO inventory (branch_id, item_name) VALUES (%s, %s) RETURNING inventory_id",
                (branch_id, "Paper"),
            )
            inventory_id = cursor.fetchone()[0]
            cursor.execute(
                """
                UPDATE inventory AS i
                SET current_stock = %s
                FROM branches AS b
                WHERE b.branch_id = i.branch_id
                  AND i.inventory_id = %s
                  AND b.owner_id = %s
                """,
                (25, inventory_id, owner_id),
            )
            self.assertEqual(cursor.rowcount, 1)
            cursor.execute(
                "SELECT current_stock FROM inventory WHERE inventory_id = %s",
                (inventory_id,),
            )
            self.assertEqual(cursor.fetchone()[0], 25)

    def test_order_creation_uses_postgres_generated_ids_and_transaction(self):
        username = f"pg-test-{uuid.uuid4().hex}"
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO shop_owners (shop_name, username, password) VALUES (%s, %s, %s) RETURNING owner_id",
                ("Integration test", username, "test-only-hash"),
            )
            owner_id = cursor.fetchone()[0]
        self.connection.commit()

        try:
            with (
                patch.object(main, "DATABASE_URL", DATABASE_URL),
                patch.object(main, "_qr_for_order"),
                patch.object(main.asyncio, "create_task", side_effect=lambda task: task.close()),
            ):
                result = main._persist_customer_order(
                    [],
                    [],
                    payment_method="UPI",
                    is_forced=False,
                    customer_name="Integration customer",
                    customer_phone="5550100",
                    customer_email="integration@example.test",
                    owner_id=owner_id,
                    settings={
                        "paper_size": "A4",
                        "orientation": "Portrait",
                        "color_mode": "Auto",
                        "duplex": False,
                        "quality": "Standard",
                        "copies": 1,
                    },
                )
            self.assertEqual(result["status"], "success")
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT order_status, payment_status FROM orders WHERE order_id = %s",
                    (result["order_id"],),
                )
                self.assertEqual(cursor.fetchone(), ("Waiting", "Pending"))
        finally:
            self.connection.commit()
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute("DELETE FROM order_history WHERE owner_id = %s", (owner_id,))
                    cursor.execute("DELETE FROM orders WHERE owner_id = %s", (owner_id,))
                    cursor.execute("DELETE FROM customers WHERE owner_id = %s", (owner_id,))
                    cursor.execute("DELETE FROM branches WHERE owner_id = %s", (owner_id,))
                    cursor.execute("DELETE FROM shop_owners WHERE owner_id = %s", (owner_id,))

    @unittest.skipUnless(MYSQL_AVAILABLE, "Install requirements-migration.txt for transfer tests.")
    def test_aiven_transfer_is_resumable_and_verifies_checksums(self):
        source_row = (
            900123,
            "Transfer integration",
            f"pg-transfer-{uuid.uuid4().hex}",
            "test-only-hash",
            datetime(2024, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None),
        )
        source = FakeMySQLConnection(source_row)
        source_database = f"transfer-test-{uuid.uuid4().hex}"
        generated_owner_id = None
        try:
            transfer_data(
                source,
                self.connection,
                source_database=source_database,
                batch_size=1,
                dry_run=False,
            )
            transfer_data(
                source,
                self.connection,
                source_database=source_database,
                batch_size=1,
                dry_run=False,
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT shop_name FROM shop_owners WHERE owner_id = %s",
                    (source_row[0],),
                )
                self.assertEqual(cursor.fetchone()[0], source_row[1])
                cursor.execute(
                    """
                    INSERT INTO shop_owners (shop_name, username, password)
                    VALUES (%s, %s, %s)
                    RETURNING owner_id
                    """,
                    ("Sequence integration", f"pg-sequence-{uuid.uuid4().hex}", "test-only-hash"),
                )
                generated_owner_id = cursor.fetchone()[0]
                self.assertGreater(generated_owner_id, source_row[0])
        finally:
            self.connection.commit()
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute("DELETE FROM shop_owners WHERE owner_id = %s", (source_row[0],))
                    if generated_owner_id is not None:
                        cursor.execute(
                            "DELETE FROM shop_owners WHERE owner_id = %s",
                            (generated_owner_id,),
                        )
                    cursor.execute(
                        """
                        DELETE FROM smartprint_migration_progress
                        WHERE source_database = %s AND table_name = 'shop_owners'
                        """,
                        (source_database,),
                    )


class FakeMySQLCursor:
    def __init__(self, source_row):
        self.source_row = source_row
        self.rows = []

    def execute(self, query, _params=()):
        normalized = " ".join(query.split())
        if "FROM information_schema.tables" in normalized:
            self.rows = [("shop_owners", "InnoDB")]
        elif "FROM information_schema.columns" in normalized:
            self.rows = [
                ("shop_owners", "owner_id", "auto_increment"),
                ("shop_owners", "shop_name", ""),
                ("shop_owners", "username", ""),
                ("shop_owners", "password", ""),
                ("shop_owners", "created_at", ""),
            ]
        elif "FROM information_schema.key_column_usage" in normalized:
            if "constraint_name = 'PRIMARY'" in normalized:
                self.rows = [("shop_owners", "owner_id")]
            else:
                self.rows = []
        elif normalized.startswith("SELECT COUNT(*) FROM"):
            self.rows = [(1,)]
        elif normalized.startswith("SELECT") and "FROM `shop_owners`" in normalized:
            self.rows = [self.source_row]
        else:
            raise AssertionError(f"Unexpected source query: {normalized}")
        self.offset = 0

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def fetchmany(self, size):
        rows = self.rows[self.offset : self.offset + size]
        self.offset += len(rows)
        return rows

    def close(self):
        pass


class FakeMySQLConnection:
    def __init__(self, source_row):
        self.source_row = source_row

    def cursor(self):
        return FakeMySQLCursor(self.source_row)


if __name__ == "__main__":
    unittest.main()
