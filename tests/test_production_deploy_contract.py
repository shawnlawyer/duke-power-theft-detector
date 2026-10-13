from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProductionDeployContractTest(unittest.TestCase):
    def test_production_commands_are_confirmation_gated(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn("require_production_confirmation", script_text)
        self.assertIn("deploy)", script_text)
        self.assertIn("rollback)", script_text)
        self.assertIn("--confirm-production", script_text)

    def test_archive_contains_only_the_clean_merged_commit(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn("require_release_commit", script_text)
        self.assertIn("status --porcelain --untracked-files=all", script_text)
        self.assertIn('RELEASE_BRANCH="${PRODUCTION_RELEASE_BRANCH:-main}"', script_text)
        self.assertIn('RELEASE_REMOTE_REF="${PRODUCTION_RELEASE_REMOTE_REF:-origin/main}"', script_text)
        self.assertIn('git -C "$ROOT_DIR" archive --format=tar.gz --output="$archive" HEAD', script_text)
        self.assertNotIn('tar -C "$ROOT_DIR"', script_text)

    def test_deploy_backs_up_restarts_service_and_checks_health(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn("home-energy-watch-$timestamp.tgz", script_text)
        self.assertIn('sudo systemctl restart "$service"', script_text)
        self.assertIn('sudo systemctl is-active --quiet "$service"', script_text)
        self.assertIn("verify_health", script_text)
        self.assertIn("https://app.homeenergywatch.com/health", script_text)

    def test_script_does_not_enable_shell_tracing(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertNotIn("set -x", script_text)

    def test_scp_destination_is_not_sent_with_literal_shell_quotes(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn('remote_copy "$archive" "$remote_archive"', script_text)
        self.assertNotIn('remote_copy "$archive" "$(remote_quote "$remote_archive")"', script_text)

    def test_remote_checkout_sync_uses_host_privilege_boundary(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn("sudo rsync -a --delete", script_text)

    def test_deploy_installs_and_starts_daily_utility_sync_timer(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()
        timer_text = (ROOT / "deploy" / "ec2" / "home-energy-watch-utility-sync.timer").read_text()
        service_text = (ROOT / "deploy" / "ec2" / "home-energy-watch-utility-sync.service").read_text()

        self.assertIn("home-energy-watch-utility-sync.timer", script_text)
        self.assertIn("systemctl enable --now home-energy-watch-utility-sync.timer", script_text)
        self.assertIn("OnCalendar=*-*-* 02:15:00 America/New_York", timer_text)
        self.assertIn("python app.py --sync-utilities", (ROOT / "deploy" / "ec2" / "home-energy-watch-utility-sync.sh").read_text())
        self.assertIn("Type=oneshot", service_text)

    def test_deploy_builds_once_tagged_by_sha_and_keeps_previous(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn('git -C "$ROOT_DIR" rev-parse --short=12 HEAD', script_text)
        self.assertIn('docker build -t "$image_repo:$release_sha" .', script_text)
        self.assertIn('docker tag "$current_ref" "$image_repo:previous"', script_text)
        self.assertIn('docker tag "$image_repo:$release_sha" "$image_repo:current"', script_text)
        self.assertNotIn("docker build -t home-energy-watch:latest .", script_text)

    def test_deploy_rolls_back_image_on_failed_health(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn("if switch_remote_image && check_health; then", script_text)
        self.assertIn("rollback_image_after_failed_deploy", script_text)
        rollback = script_text[script_text.index("rollback_image_after_failed_deploy() {"):]
        rollback = rollback[: rollback.index("\n}\n")]
        self.assertIn('docker tag "$image_repo:previous" "$image_repo:current"', rollback)
        self.assertIn('sudo systemctl restart "$service"', rollback)
        self.assertIn("check_health", rollback)
        # Both outcomes of an automatic rollback must still fail the deploy.
        self.assertEqual(rollback.count("die "), 2)
        self.assertIn("rolled back to the previous image", rollback)
        self.assertIn("did NOT restore health", rollback)

    def test_source_rollback_rebuilds_and_promotes_to_current(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()
        rollback = script_text[script_text.index("<<'REMOTE_ROLLBACK'"):]
        rollback = rollback[: rollback.index("\nREMOTE_ROLLBACK\n")]

        self.assertIn('docker tag "$rollback_tag" "$image_repo:current"', rollback)
        self.assertIn('sudo systemctl restart "$service"', rollback)

    def test_deploy_chowns_bind_mounts_for_non_root_container(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()
        readme = (ROOT / "deploy" / "ec2" / "README.md").read_text()

        self.assertIn('CONTAINER_UID="10001"', script_text)
        self.assertIn('sudo chown -R "$container_uid:$container_uid" "$data_dir"', script_text)
        self.assertLess(
            script_text.index('sudo chown -R "$container_uid:$container_uid"'),
            script_text.index('sudo systemctl restart "$service"'),
        )
        self.assertIn("sudo chown -R 10001:10001 /opt/home-energy-watch/input /opt/home-energy-watch/output", readme)

    def test_optional_rds_snapshot_before_deploy(self):
        script_text = (ROOT / "scripts" / "deploy-production.sh").read_text()

        self.assertIn('RDS_INSTANCE_ID="${HEW_RDS_INSTANCE_ID:-}"', script_text)
        self.assertIn("aws rds create-db-snapshot", script_text)
        self.assertIn("aws rds wait db-snapshot-available", script_text)
        self.assertIn("Skipping pre-deploy RDS snapshot because HEW_RDS_INSTANCE_ID is not set", script_text)
        deploy_case = script_text[script_text.index("  deploy)\n    require_production_confirmation"):]
        self.assertLess(deploy_case.index("snapshot_rds_before_deploy"), deploy_case.index("deploy_archive"))

    def test_direct_launcher_does_not_rebuild_or_leak_secrets(self):
        direct = (ROOT / "deploy" / "ec2" / "home-energy-watch-direct.sh").read_text()

        self.assertIn("DEFAULT_IMAGE=home-energy-watch:current", direct)
        self.assertIn('if ! docker image inspect "$HEW_IMAGE"', direct)
        build_lines = [line for line in direct.splitlines() if "docker build" in line]
        self.assertEqual(len(build_lines), 1)
        self.assertTrue(build_lines[0].startswith("  "), "docker build must only run inside the missing-image branch")
        self.assertIn('--env-file "$RUNTIME_ENV_FILE"', direct)
        self.assertNotIn("-e STRIPE_SECRET_KEY", direct)
        self.assertNotIn("-e POWER_APP_SECRET", direct)
        self.assertIn('POWER_BIND_ADDRESS="${POWER_BIND_ADDRESS:-}"', direct)

    def test_direct_launcher_behavior_with_stub_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            log = work / "docker.log"
            bindir = work / "bin"
            bindir.mkdir()
            stub = bindir / "docker"
            stub.write_text(
                "#!/bin/sh\n"
                'echo "docker $*" >> "$STUB_LOG"\n'
                'if [ "$1" = image ] && [ "$2" = inspect ]; then [ -n "$IMAGE_EXISTS" ] && exit 0; exit 1; fi\n'
                'if [ "$1" = run ]; then prev=""; for a; do if [ "$prev" = --env-file ]; then cp "$a" "$STUB_DIR/web.env"; fi; prev=$a; done; fi\n'
                "exit 0\n"
            )
            stub.chmod(0o755)
            root = work / "checkout"
            (root / "deploy" / "ec2").mkdir(parents=True)
            env_file = root / "deploy" / "ec2" / ".env.production"
            script = (ROOT / "deploy" / "ec2" / "home-energy-watch-direct.sh").read_text()
            script = script.replace("/home/ubuntu/home-energy-watch", str(root))
            script = script.replace("/opt/home-energy-watch", str(work / "opt"))
            script = script.replace("/run/home-energy-watch", str(work / "run"))
            launcher = work / "direct.sh"
            launcher.write_text(script)

            def run(env_text, image_exists):
                env_file.write_text(env_text)
                log.write_text("")
                env = dict(
                    os.environ,
                    PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                    STUB_LOG=str(log),
                    STUB_DIR=str(work),
                    IMAGE_EXISTS="1" if image_exists else "",
                )
                result = subprocess.run(["bash", str(launcher)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return log.read_text()

            base_env = 'POWER_ENV=production\nPOWER_APP_SECRET="quoted secret value"\nSTRIPE_SECRET_KEY=sk_test_x\n'
            calls = run(base_env, image_exists=True)
            self.assertNotIn("docker build", calls)
            self.assertIn(" -p 80:8000 ", calls)
            self.assertIn("home-energy-watch:current", calls)
            self.assertNotIn("sk_test_x", calls)
            self.assertNotIn("quoted secret value", calls)
            container_env = (work / "web.env").read_text().splitlines()
            self.assertIn("POWER_APP_SECRET=quoted secret value", container_env)
            self.assertIn("STRIPE_SECRET_KEY=sk_test_x", container_env)
            self.assertEqual(oct((work / "run" / "web.env").stat().st_mode & 0o777), "0o600")

            calls = run(base_env, image_exists=False)
            self.assertIn("docker build -t home-energy-watch:current", calls)

            calls = run(base_env + "POWER_BIND_ADDRESS=127.0.0.1\nPOWER_WEB_PORT=8080\n", image_exists=True)
            self.assertIn(" -p 127.0.0.1:8080:8000 ", calls)

    def test_utility_sync_uses_current_image_with_legacy_fallback(self):
        sync = (ROOT / "deploy" / "ec2" / "home-energy-watch-utility-sync.sh").read_text()

        self.assertIn("DEFAULT_IMAGE=home-energy-watch:current", sync)
        self.assertIn("LEGACY_IMAGE=home-energy-watch:latest", sync)
        self.assertIn('--env-file "$ENV_FILE"', sync)

    def test_rds_cutover_runbook_pauses_sync_and_has_no_live_identifiers(self):
        runbook = (ROOT / "deploy" / "ec2" / "RDS_ENCRYPTED_MULTIAZ_CUTOVER.md").read_text()

        self.assertGreaterEqual(runbook.count("sudo systemctl stop home-energy-watch-utility-sync.timer"), 2)
        self.assertGreaterEqual(runbook.count("sudo systemctl stop home-energy-watch-utility-sync.service"), 2)
        rollback = runbook[runbook.index("## Rollback"):]
        self.assertIn("sudo systemctl stop home-energy-watch'", rollback)
        self.assertIn("<EC2_HOST>", runbook)
        self.assertIn("<KMS_KEY_ID>", runbook)
        self.assertIn("<DB_SECURITY_GROUP_ID>", runbook)
        self.assertNotRegex(runbook, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(runbook, r"sg-[0-9a-f]{8,}")
        self.assertNotRegex(runbook, r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        self.assertNotIn(".pem", runbook)

    def test_compose_prod_is_marked_unused_and_matches_direct_env(self):
        compose = (ROOT / "deploy" / "ec2" / "docker-compose.prod.yml").read_text()

        self.assertTrue(compose.startswith("# NOT USED BY PRODUCTION SYSTEMD."))
        for name in ("POWER_BEDROCK_ENABLED", "POWER_BEDROCK_REGION", "POWER_BEDROCK_MODEL_ID"):
            self.assertIn(f"{name}:", compose)

    def test_production_env_example_matches_annual_pricing(self):
        example = (ROOT / "deploy" / "ec2" / ".env.production.example").read_text()

        self.assertNotIn("$19.99", example)
        self.assertIn("$239.88/year", example)
        self.assertIn("POWER_BIND_ADDRESS=", example)
        self.assertIn("HEW_RDS_INSTANCE_ID", example)
        self.assertIn("sslmode=verify-full", example)


if __name__ == "__main__":
    unittest.main()
