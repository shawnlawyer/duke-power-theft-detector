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

    def test_container_runs_as_fixed_non_root_user(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        user_lines = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("USER ")]

        self.assertTrue(user_lines, "Dockerfile must set USER")
        self.assertEqual(user_lines[-1], "USER 10001:10001")
        self.assertIn("useradd --system --uid 10001 --gid 10001", dockerfile)
        # Only the data directories are handed to the app user, not /app.
        self.assertIn("chown -R 10001:10001 /data /srv/home-energy-watch", dockerfile)
        self.assertNotRegex(dockerfile, r"chown[^\n]*\s/app\b")
        # USER must come after COPY . . so the source stays root-owned.
        self.assertLess(dockerfile.index("COPY . ."), dockerfile.index("USER 10001:10001"))

    def test_container_has_stdlib_healthcheck(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("HEALTHCHECK", dockerfile)
        healthcheck = dockerfile[dockerfile.index("HEALTHCHECK"):].split("\n\n", 1)[0]
        self.assertIn("/health", healthcheck)
        self.assertIn("urllib.request", healthcheck)
        self.assertNotIn("curl", healthcheck)

    def test_gunicorn_recycles_workers_and_keeps_env_tuning(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        cmd = [line for line in dockerfile.splitlines() if line.startswith("CMD ")][-1]

        self.assertIn("exec gunicorn", cmd)
        self.assertIn("--workers ${POWER_WEB_CONCURRENCY:-2}", cmd)
        self.assertIn("--timeout ${POWER_GUNICORN_TIMEOUT:-120}", cmd)
        self.assertIn("--max-requests 1000", cmd)
        self.assertIn("--max-requests-jitter 100", cmd)
        self.assertIn("--graceful-timeout 30", cmd)

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
        for uses in re.findall(r"uses:\s*(\S+)", workflow):
            self.assertRegex(uses, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", f"action not pinned by SHA: {uses}")

    def test_release_workflow_runs_lint_and_postgres_smoke(self):
        workflow = (ROOT / ".github" / "workflows" / "release-security.yml").read_text()

        self.assertIn("\n  lint:\n", workflow)
        self.assertIn("ruff check --select F --ignore F401,F841 .", workflow)
        self.assertIn("\n  postgres:\n", workflow)
        self.assertIn("image: postgres:16", workflow)
        self.assertIn("tests/test_postgres_smoke.py", workflow)
        self.assertIn("POWER_TEST_DATABASE_URL=", workflow)
        self.assertIn("POWER_TEST_REQUIRE_DATABASE=1", workflow)


if __name__ == "__main__":
    unittest.main()
