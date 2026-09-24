import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = PROJECT_ROOT / "bin" / "yougile-compose"
INSTALLER = PROJECT_ROOT / "bin" / "install-compose-commands"


class ComposeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "music-verifier"
        self.repository.mkdir()
        (self.repository / "compose.yaml").write_text(
            "name: music-verifier\n", encoding="utf-8"
        )
        self.environment_file = self.root / "music-verifier.env"
        self.environment_file.write_text("WEBHOOK_SECRET=not-a-real-secret\n", encoding="utf-8")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.capture = self.root / "docker-arguments"
        self._write_executable(
            self.fake_bin / "docker",
            "#!/usr/bin/env bash\n"
            'printf "%s\\t" "$@" >>"$CAPTURE_FILE"\n'
            'printf "\\n" >>"$CAPTURE_FILE"\n'
            'if [[ " $* " == *" --columns "* && -n "${MOCK_SELECTION:-}" ]]; then\n'
            '  printf "YOUGILE_RESOLVE_SELECTED_COLUMN_IDS=%s\\n" "$MOCK_SELECTION"\n'
            'fi\n',
        )
        self._write_executable(
            self.fake_bin / "sudo",
            '#!/usr/bin/env bash\nexec "$@"\n',
        )
        self._write_executable(
            self.fake_bin / "id",
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == "-u" ]]; then echo 0; '
            'else exec /usr/bin/id "$@"; fi\n',
        )
        self.environment = os.environ.copy()
        self.environment.update({
            "CAPTURE_FILE": str(self.capture),
            "PATH": f"{self.fake_bin}:{self.environment['PATH']}",
            "YOUGILE_ENV_FILE": str(self.environment_file),
            "YOUGILE_REPO_ROOT": str(self.repository),
        })

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run_command(self, command_name: str, *arguments: str) -> subprocess.CompletedProcess:
        command = self.root / command_name
        command.symlink_to(DISPATCHER)
        return subprocess.run(
            [str(command), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def _captured_calls(self) -> list[list[str]]:
        return [
            line.rstrip("\t").split("\t")
            for line in self.capture.read_text(encoding="utf-8").splitlines()
        ]

    def test_start_executes_docker_compose_command(self):
        result = self._run_command("yougile-start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._captured_calls(),
            [[
                "compose", "--project-name", "music-verifier",
                "--project-directory", str(self.repository),
                "--env-file", str(self.environment_file), "up", "-d",
            ]],
        )

    def test_status_executes_docker_compose_command(self):
        result = self._run_command("yougile-status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._captured_calls()[0][-2:], ["ps", "-a"])

    def test_full_status_includes_container_processes(self):
        result = self._run_command("yougile-status", "--full")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[-2:] for call in self._captured_calls()], [["ps", "-a"], [str(self.environment_file), "top"]])

    def test_installed_symlink_uses_its_own_project_by_default(self):
        self.environment.pop("YOUGILE_REPO_ROOT")
        result = self._run_command("yougile-status")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self._captured_calls()[0]
        self.assertEqual(call[call.index("--project-directory") + 1], str(PROJECT_ROOT))

    def test_cisnet_build_context_is_inside_project(self):
        template = (PROJECT_ROOT / ".env").read_text(encoding="utf-8")
        self.assertIn("CISNET_BUILD_CONTEXT=./cisnet-playwright", template)
        self.assertTrue((PROJECT_ROOT / "cisnet-playwright" / "Dockerfile").is_file())

    def test_resolve_columns_updates_only_host_env_after_selection(self):
        column = "22222222-2222-4222-8222-222222222222"
        self.environment["YOUGILE_REPO_ROOT"] = str(PROJECT_ROOT)
        self.environment["MOCK_SELECTION"] = column
        result = self._run_command("yougile-resolve", "--columns")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"YOUGILE_ALLOWED_COLUMN_IDS={column}", result.stdout)
        self.assertIn("WEBHOOK_SECRET=not-a-real-secret", self.environment_file.read_text())
        self.assertEqual(self._captured_calls()[0][-5:], [
            "-T", "receiver", "python", "src/yougile_resolve.py", "--columns"
        ])

    def test_resolve_columns_cancel_does_not_touch_host_env(self):
        self.environment["YOUGILE_REPO_ROOT"] = str(PROJECT_ROOT)
        before = self.environment_file.read_bytes()
        result = self._run_command("yougile-resolve", "--columns")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.environment_file.read_bytes(), before)

    def test_reset_removes_containers_before_exact_data_volume(self):
        result = self._run_command(
            "yougile-reset-data", "--execute", "RESET MUSIC-VERIFIER DATA"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._captured_calls()
        self.assertEqual(calls[0][-2:], ["down", "--remove-orphans"])
        self.assertEqual(calls[1], ["volume", "rm", "music-verifier_music-data"])

    def test_reset_requires_exact_confirmation_without_calling_docker(self):
        result = self._run_command("yougile-reset-data", "wrong confirmation")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.capture.exists())

    def test_reset_without_arguments_only_prints_plan(self):
        result = self._run_command("yougile-reset-data")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("music-verifier_music-data", result.stdout)
        self.assertFalse(self.capture.exists())

    def test_recognize_wav_passes_input_flag_and_cleans_temporary_copy(self):
        wav = self.root / "sample.wav"
        wav.write_bytes(b"test")
        result = self._run_command("recognize-wav", "-i", str(wav), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._captured_calls()
        self.assertEqual(calls[0][-3:-1], ["cp", str(wav)])
        self.assertEqual(calls[2][-4:-2], ["src/recognize_wav.py", "-i"])
        self.assertEqual(calls[2][-1], "--force")
        self.assertEqual(calls[3][-4:-2], ["rm", "-f"])

    def test_installer_creates_only_expected_symlinks(self):
        target = self.root / "commands"
        environment = self.environment | {
            "YOUGILE_COMMAND_DIR": str(target),
            "YOUGILE_REPO_ROOT": str(PROJECT_ROOT),
        }
        result = subprocess.run(
            [str(INSTALLER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {
            "yougile-start", "yougile-stop", "yougile-restart", "yougile-status",
            "yougile-logs", "yougile-httplogs", "yougile-cisnetlogs",
            "yougile-runs", "yougile-resolve", "yougile-cisnet", "yougile-env",
            "yougile-reset-data", "recognize-wav",
        }
        self.assertEqual({entry.name for entry in target.iterdir()}, expected)
        self.assertTrue(
            all(entry.resolve() == DISPATCHER for entry in target.iterdir())
        )

    def test_cisnet_browser_uses_image_user(self):
        compose_text = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('user: "pwuser"', compose_text)
        self.assertNotIn('user: "1000:1000"', compose_text)
        self.assertNotIn("init: true", compose_text)


if __name__ == "__main__":
    unittest.main()
