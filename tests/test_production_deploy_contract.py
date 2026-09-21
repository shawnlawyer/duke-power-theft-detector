from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
