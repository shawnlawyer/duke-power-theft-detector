from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OmenDeployContractTest(unittest.TestCase):
    def script_text(self):
        return (ROOT / "scripts" / "omen-deploy.sh").read_text()

    def test_omen_sync_preserves_remote_runtime_directory(self):
        script_text = self.script_text()

        self.assertIn("--exclude 'runtime/'", script_text)
        self.assertIn("--exclude '.tmp-*'", script_text)

    def test_docker_build_context_omits_runtime_data(self):
        dockerignore = ROOT / ".dockerignore"
        ignored_paths = dockerignore.read_text().splitlines()

        self.assertIn("runtime/", ignored_paths)
        self.assertIn("deploy/ec2/.env.production", ignored_paths)
        for entry in (".context", ".DS_Store", "**/.DS_Store", "_to_delete/", ".tmp-deploy/"):
            self.assertIn(entry, ignored_paths)
        # scripts/security-scan.sh runs pytest inside the image.
        self.assertNotIn("tests/", ignored_paths)
        self.assertNotIn("tests", ignored_paths)

    def test_gitignore_covers_local_clutter(self):
        ignored_paths = (ROOT / ".gitignore").read_text().splitlines()

        for entry in (".context", ".DS_Store", "**/.DS_Store", "_to_delete/", ".tmp-deploy/"):
            self.assertIn(entry, ignored_paths)

    def test_omen_container_receives_security_environment_via_env_file(self):
        script_text = self.script_text()

        for name in (
            "POWER_ENV",
            "POWER_APP_SECRET",
            "POWER_AUDIT_SIGNING_KEY",
            "POWER_DATA_ENCRYPTION_KEY",
            "POWER_TRUST_PROXY",
            "POWER_STAFF_MFA_REQUIRED",
            "POWER_DATA_DELETION_ENABLED",
            "POWER_DATA_DELETION_POLICY_VERSION",
            "POWER_EMAIL_BACKEND",
            "POWER_EMAIL_FROM",
            "POWER_EMAIL_REGION",
            "POWER_BILLING_ENABLED",
            "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET",
        ):
            self.assertIn(f'env_line {name} "${name}"', script_text)
        self.assertIn("--env-file $REMOTE_ENV_FILE_Q", script_text)

    def test_secrets_never_go_on_the_command_line(self):
        script_text = self.script_text()

        self.assertNotIn("-e STRIPE_SECRET_KEY", script_text)
        self.assertNotIn("-e STRIPE_WEBHOOK_SECRET", script_text)
        self.assertNotIn("-e POWER_APP_SECRET", script_text)
        self.assertNotIn("-e POWER_DATA_ENCRYPTION_KEY", script_text)
        self.assertNotIn("-e POWER_AUDIT_SIGNING_KEY", script_text)

    def test_env_file_is_private_and_removed(self):
        script_text = self.script_text()

        self.assertIn("umask 077", script_text)
        self.assertIn("chmod 600", script_text)
        self.assertIn("rm -f $REMOTE_ENV_FILE_Q", script_text)
        self.assertIn("trap cleanup_local_env_file EXIT", script_text)

    def test_ssh_pins_host_keys(self):
        script_text = self.script_text()

        self.assertNotIn("StrictHostKeyChecking=no", script_text)
        self.assertIn("StrictHostKeyChecking=accept-new", script_text)
        self.assertIn("UserKnownHostsFile", script_text)
        # The key path is shell-quoted inside the rsync -e string.
        self.assertIn('RSYNC_SSH="ssh -i $(quote "$OMEN_SSH_KEY")', script_text)
        self.assertNotIn('-e "ssh -i $OMEN_SSH_KEY', script_text)

    def test_deploy_refuses_weak_app_secret(self):
        script_text = self.script_text()

        self.assertIn("require_app_secret", script_text)
        self.assertIn('DEFAULT_APP_SECRET="local-power-data-only"', script_text)
        self.assertIn('"${#POWER_APP_SECRET}" -lt 32', script_text)
        self.assertNotIn('POWER_APP_SECRET="${POWER_APP_SECRET:-local-power-data-only}"', script_text)
        example = (ROOT / ".env.omen.example").read_text()
        self.assertNotIn("POWER_APP_SECRET=local-power-data-only", example)


if __name__ == "__main__":
    unittest.main()
