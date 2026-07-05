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


if __name__ == "__main__":
    unittest.main()
