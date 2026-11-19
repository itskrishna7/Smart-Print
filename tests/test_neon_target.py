import os
import json
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess

from neon_target import NeonTargetError, verify_neon_target


PROJECT_ID = "weathered-unit-58487898"
BRANCH_ID = "br-still-feather-azeis3n0"
ENDPOINT_HOST = "ep-example.c-3.ap-southeast-1.aws.neon.tech"


def endpoint_response(host=ENDPOINT_HOST, project_id=PROJECT_ID, branch_id=BRANCH_ID):
    return json.dumps(
        {
            "endpoints": [
                {
                    "host": host,
                    "project_id": project_id,
                    "branch_id": branch_id,
                    "type": "read_write",
                    "hosts": {
                        "read_write_host": host,
                        "read_write_pooled_host": "ep-example-pooler.c-3.ap-southeast-1.aws.neon.tech",
                    },
                }
            ]
        }
    )


class NeonTargetTests(unittest.TestCase):
    def setUp(self):
        self.environment_patch = patch.dict(
            os.environ,
            {
                "SMARTPRINT_NEON_TEST_PROJECT_ID": PROJECT_ID,
                "SMARTPRINT_NEON_TEST_BRANCH_ID": BRANCH_ID,
                "SMARTPRINT_NEON_PRODUCTION_PROJECT_ID": "production-project",
                "SMARTPRINT_NEON_PRODUCTION_BRANCH_ID": "br-production",
            },
        )
        self.environment_patch.start()

    def tearDown(self):
        self.environment_patch.stop()

    @patch("neon_target.subprocess.run")
    def test_accepts_url_for_selected_branch_endpoint(self, run):
        run.return_value = CompletedProcess([], 0, endpoint_response(), "")

        verify_neon_target(
            f"postgresql://user:password@{ENDPOINT_HOST}/db?sslmode=require",
            "test",
            PROJECT_ID,
            BRANCH_ID,
        )

        self.assertIn(f"/projects/{PROJECT_ID}/branches/{BRANCH_ID}/endpoints", run.call_args.args[0])

    @patch("neon_target.subprocess.run")
    def test_rejects_url_for_different_branch_before_migration(self, run):
        run.return_value = CompletedProcess(
            [],
            0,
            endpoint_response(branch_id="br-another-branch"),
            "",
        )

        with self.assertRaisesRegex(NeonTargetError, "no changes were made"):
            verify_neon_target(
                f"postgresql://user:password@{ENDPOINT_HOST}/db?sslmode=require",
                "test",
                PROJECT_ID,
                BRANCH_ID,
            )

    @patch("neon_target.subprocess.run")
    def test_rejects_pooled_connection_even_when_api_lists_pooled_host(self, run):
        run.return_value = CompletedProcess([], 0, endpoint_response(), "")
        pooled_host = "ep-example-pooler.c-3.ap-southeast-1.aws.neon.tech"

        with self.assertRaisesRegex(NeonTargetError, "direct, unpooled"):
            verify_neon_target(
                f"postgresql://user:password@{pooled_host}/db?sslmode=require",
                "test",
                PROJECT_ID,
                BRANCH_ID,
            )

    @patch("neon_target.subprocess.run")
    def test_fails_closed_when_neon_cli_cannot_verify_target(self, run):
        run.return_value = CompletedProcess([], 1, "", "sensitive CLI error")

        with self.assertRaisesRegex(NeonTargetError, "Check Neon CLI authentication"):
            verify_neon_target(
                f"postgresql://user:password@{ENDPOINT_HOST}/db?sslmode=require",
                "test",
                PROJECT_ID,
                BRANCH_ID,
            )

    @patch("neon_target.subprocess.run")
    def test_rejects_branch_not_in_selected_environment_allowlist(self, run):
        with self.assertRaisesRegex(NeonTargetError, "do not match the test target allowlist"):
            verify_neon_target(
                f"postgresql://user:password@{ENDPOINT_HOST}/db?sslmode=require",
                "test",
                PROJECT_ID,
                "br-production",
            )

        run.assert_not_called()