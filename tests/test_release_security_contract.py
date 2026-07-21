from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseSecurityContractTest(unittest.TestCase):
    def test_container_uses_digest_pinned_base_and_hashed_lock(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertRegex(
            dockerfile.splitlines()[0],
            r"^FROM python:3\.11-slim@sha256:[0-9a-f]{64}$",
            "Docker base must be immutable",
        )
        self.assertIn("COPY requirements.lock .", dockerfile)
        self.assertIn("pip install --no-cache-dir --require-hashes -r requirements.lock", dockerfile)
        self.assertIn("python -m pip uninstall --yes setuptools wheel pip", dockerfile)

    def test_security_scan_fails_on_high_or_critical_findings(self):
        scan_script = (ROOT / "scripts" / "security-scan.sh").read_text()

        self.assertRegex(scan_script, r"aquasec/trivy@sha256:[0-9a-f]{64}")
        self.assertRegex(scan_script, r"python:3\.11-slim@sha256:[0-9a-f]{64}")
        self.assertIn("python -m pytest", scan_script)
        self.assertIn("python -m pip_audit -r requirements.lock --disable-pip --no-deps", scan_script)
        self.assertIn('TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-/tmp/home-energy-watch-trivy-cache}"', scan_script)
        self.assertIn("--exit-code 1", scan_script)
        self.assertIn("--severity HIGH,CRITICAL", scan_script)

    def test_release_workflow_is_read_only_and_pins_actions(self):
        workflow = (ROOT / ".github" / "workflows" / "release-security.yml").read_text()

        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertRegex(workflow, r"uses: actions/checkout@[0-9a-f]{40}")
        self.assertIn("run: ./scripts/security-scan.sh", workflow)
        self.assertNotIn("secrets.", workflow)


if __name__ == "__main__":
    unittest.main()
