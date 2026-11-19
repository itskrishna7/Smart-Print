import os
import subprocess
import sys
import unittest
import importlib.util
from unittest.mock import patch

os.environ["SMARTPRINT_ENV"] = "development"

import main
import migrate_postgres
from neon_target import NeonTargetError

MYSQL_AVAILABLE = importlib.util.find_spec("mysql") is not None
if MYSQL_AVAILABLE:
    import migrate_aiven_schema as migrate
    import migrate_aiven_to_neon as transfer_migration
else:
    migrate = None
    transfer_migration = None


class MigrationCursor:
    def __init__(self):
        self.statements = []
        self.last_query = ""
        self.updated_password = None

    def execute(self, query, params=()):
        self.last_query = query
        self.statements.append((query, params))
        if query.startswith("UPDATE shop_owners SET password"):
            self.updated_password = params[0]

    def fetchone(self):
        return ("password",)

    def fetchall(self):
        if self.last_query == "SELECT owner_id, password FROM shop_owners":
            return [(17, "legacy-password")]
        return []

    def close(self):
        pass


class MigrationConnection:
    def __init__(self):
        self.fake_cursor = MigrationCursor()
        self.committed = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class MigrationTests(unittest.TestCase):
    def test_importing_application_does_not_connect_or_run_schema_migrations(self):
        environment = os.environ.copy()
        environment["SMARTPRINT_ENV"] = "development"
        result = subprocess.run(
            [sys.executable, "-c", "import main"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_application_refuses_to_start_without_explicit_environment_mode(self):
        environment = os.environ.copy()
        environment.pop("SMARTPRINT_ENV", None)
        result = subprocess.run(
            [sys.executable, "-c", "import main"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SMARTPRINT_ENV must be explicitly set", result.stderr)

    def test_postgres_schema_apply_requires_explicit_target_environment(self):
        result = subprocess.run(
            [sys.executable, "migrate.py", "--apply"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--apply requires an explicit --target-environment", result.stderr)

    def test_postgres_schema_apply_rejects_mismatched_target_before_writes(self):
        argv = [
            "migrate.py",
            "--apply",
            "--target-environment",
            "test",
            "--neon-project-id",
            "test-project",
            "--neon-branch-id",
            "br-test",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL_UNPOOLED": "postgresql://test",
                    "SMARTPRINT_NEON_TEST_PROJECT_ID": "test-project",
                    "SMARTPRINT_NEON_TEST_BRANCH_ID": "br-test",
                },
            ),
            patch.object(
                migrate_postgres,
                "verify_neon_target",
                side_effect=NeonTargetError("target mismatch"),
            ),
            patch.object(migrate_postgres, "run_migrations") as run_migrations,
        ):
            self.assertEqual(migrate_postgres.main(), 1)

        run_migrations.assert_not_called()

    @unittest.skipUnless(MYSQL_AVAILABLE, "Install requirements-migration.txt for transfer-tool tests.")
    def test_aiven_transfer_apply_requires_explicit_target_environment(self):
        result = subprocess.run(
            [sys.executable, "migrate_aiven_to_neon.py", "--apply"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--apply requires an explicit --target-environment", result.stderr)

    @unittest.skipUnless(MYSQL_AVAILABLE, "Install requirements-migration.txt for transfer-tool tests.")
    def test_aiven_transfer_rejects_mismatched_target_before_connecting(self):
        argv = [
            "migrate_aiven_to_neon.py",
            "--apply",
            "--target-environment",
            "test",
            "--neon-project-id",
            "test-project",
            "--neon-branch-id",
            "br-test",
        ]
        environment = {
            "DATABASE_URL_UNPOOLED": "postgresql://test",
            "SMARTPRINT_DB_NAME": "source",
            "SMARTPRINT_DB_HOST": "source.example",
            "SMARTPRINT_DB_USER": "readonly",
            "SMARTPRINT_DB_PASSWORD": "test-only",
            "SMARTPRINT_DB_SSL_CA": "source-ca.pem",
            "SMARTPRINT_NEON_TEST_PROJECT_ID": "test-project",
            "SMARTPRINT_NEON_TEST_BRANCH_ID": "br-test",
        }
        with (
            patch.object(sys, "argv", argv),
            patch.dict(os.environ, environment),
            patch.object(
                transfer_migration,
                "verify_neon_target",
                side_effect=NeonTargetError("target mismatch"),
            ),
            patch.object(transfer_migration.mysql.connector, "connect") as connect,
        ):
            self.assertEqual(transfer_migration.main(), 1)

        connect.assert_not_called()

    @unittest.skipUnless(MYSQL_AVAILABLE, "Install requirements-migration.txt for the legacy Aiven migration test.")
    def test_migrations_are_additive_and_hash_legacy_passwords(self):
        connection = MigrationConnection()
        with (
            patch.dict(
                os.environ,
                {
                    "SMARTPRINT_DB_HOST": "localhost",
                    "SMARTPRINT_DB_PORT": "3306",
                    "SMARTPRINT_DB_NAME": "smart_print_test",
                    "SMARTPRINT_MIGRATION_DB_USER": "migration_user",
                    "SMARTPRINT_MIGRATION_DB_PASSWORD": "",
                    "SMARTPRINT_ENV": "development",
                },
                clear=False,
            ),
            patch.object(migrate.mysql.connector, "connect", return_value=connection),
        ):
            migrate.run_migrations()

        statements = [query.upper() for query, _ in connection.fake_cursor.statements]
        self.assertTrue(connection.committed)
        self.assertFalse(any("DROP TABLE" in query for query in statements))
        self.assertFalse(any("CREATE DATABASE" in query for query in statements))
        self.assertTrue(
            any("CREATE TABLE IF NOT EXISTS SECURITY_RATE_LIMITS" in query for query in statements)
        )
        self.assertTrue(
            main._verify_password("legacy-password", connection.fake_cursor.updated_password)[0]
        )


if __name__ == "__main__":
    unittest.main()
