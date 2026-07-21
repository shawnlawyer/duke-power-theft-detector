from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OmenDeployContractTest(unittest.TestCase):
    def test_omen_sync_preserves_remote_runtime_directory(self):
        deploy_script = ROOT / "scripts" / "omen-deploy.sh"
        script_text = deploy_script.read_text()

        self.assertIn("--exclude 'runtime/'", script_text)

    def test_docker_build_context_omits_runtime_data(self):
        dockerignore = ROOT / ".dockerignore"
        ignored_paths = dockerignore.read_text().splitlines()

        self.assertIn("runtime/", ignored_paths)
        self.assertIn("deploy/ec2/.env.production", ignored_paths)

    def test_omen_container_receives_security_environment(self):
        deploy_script = ROOT / "scripts" / "omen-deploy.sh"
        script_text = deploy_script.read_text()

        self.assertIn("-e POWER_ENV=$POWER_ENV_Q", script_text)
        self.assertIn("-e POWER_AUDIT_SIGNING_KEY=$POWER_AUDIT_SIGNING_KEY_Q", script_text)
        self.assertIn("-e POWER_DATA_ENCRYPTION_KEY=$POWER_DATA_ENCRYPTION_KEY_Q", script_text)
        self.assertIn("-e POWER_TRUST_PROXY=$POWER_TRUST_PROXY_Q", script_text)
        self.assertIn("-e POWER_STAFF_MFA_REQUIRED=$POWER_STAFF_MFA_REQUIRED_Q", script_text)
        self.assertIn("-e POWER_DATA_DELETION_ENABLED=$POWER_DATA_DELETION_ENABLED_Q", script_text)
        self.assertIn(
            "-e POWER_DATA_DELETION_POLICY_VERSION=$POWER_DATA_DELETION_POLICY_VERSION_Q",
            script_text,
        )
        self.assertIn("-e POWER_EMAIL_BACKEND=$POWER_EMAIL_BACKEND_Q", script_text)
        self.assertIn("-e POWER_EMAIL_FROM=$POWER_EMAIL_FROM_Q", script_text)
        self.assertIn("-e POWER_EMAIL_REGION=$POWER_EMAIL_REGION_Q", script_text)
        self.assertIn("-e POWER_BILLING_ENABLED=$POWER_BILLING_ENABLED_Q", script_text)


if __name__ == "__main__":
    unittest.main()
