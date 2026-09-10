"""実 Docker を呼ばず、Linux release 入口全体の門禁・順序を検査する。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from test_deploy import CONFIGS, DAEMON, IMAGES, PROJECT, FakeContext, container

ROOT = Path(__file__).resolve().parents[2]
FILES = ("compose.yaml", "Makefile", ".env.example", "scripts/compose.sh", "scripts/deploy.sh")


class ReleaseTests(unittest.TestCase):
    """外部 state を持たない fixture release を invocation ごとに用意する。"""

    def setUp(self) -> None:
        """本物の Shell と Python validator を fake Docker の前後で実行する。"""

        self.temporary = tempfile.TemporaryDirectory(prefix="skillmind release test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        for name in FILES:
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / "runtime.env").write_text("FIXTURE=not-a-secret\n")
        self.context = FakeContext()
        self.context.config["services"]["api"]["environment"]["SKILLMIND_CONTEXT_PATH"] = (
            "/skillmind"
        )
        self.context.config["services"]["web"]["build"] = {
            "args": {"SKILLMIND_CONTEXT_PATH": "/skillmind"}
        }
        self.context.rows.extend(
            (
                container("api", 5, state="exited"),
                container("web", 6, state="exited"),
                container("worker", 7, state="exited"),
            )
        )
        self.context.rows[4]["environment"].append("SKILLMIND_CONTEXT_PATH=/skillmind")
        self.state = {"config": self.context.config, "containers": self.context.rows}
        self.save_state()
        manifest = [
            {"Config": f"{name}.json", "RepoTags": [f"skillmind/{name}:0.1.0"]} for name in CONFIGS
        ]
        with tarfile.open(self.root / "images.tar", "w") as archive:
            for name, body in [
                ("manifest.json", json.dumps(manifest).encode()),
                *[(f"{name}.json", body) for name, body in CONFIGS.items()],
            ]:
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                archive.addfile(entry, io.BytesIO(body))
        (self.root / "release.env").write_text(
            f"FORMAT=1\nBACKEND_IMAGE_ID={IMAGES['backend']}\nWEB_IMAGE_ID={IMAGES['web']}\n"
            "PLATFORM=linux/amd64\nCONTEXT_PATH=/skillmind\nVERSION=fixture\n"
        )
        self.digest = self.checksums()
        fake = ROOT / "scripts/tests/fake_release_docker.py"
        (self.root / "docker").write_text(
            f"#!{sys.executable}\nimport runpy\n"
            f"runpy.run_path({str(fake)!r}, run_name='__main__')\n"
        )
        (self.root / "docker").chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": f"{self.root}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT / "scripts/tests") + os.pathsep + str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SKM_FAKE_ROOT": str(self.root),
            "ENV_FILE": str(self.root / "runtime.env"),
            "COMPOSE_PROJECT_NAME": PROJECT,
            "DAEMON_ID": DAEMON,
            "RELEASE_SHA256": self.digest,
            "MAINTENANCE_CONFIRMED": "1",
        }

    def save_state(self) -> None:
        """fixture の観測値を fake executable と共有する。"""

        (self.root / "state.json").write_text(json.dumps(self.state))

    def checksums(self) -> str:
        """実 exporter と同じ whitelist/checksum 書式を作る。"""

        data = "".join(
            f"{hashlib.sha256((self.root / name).read_bytes()).hexdigest()}  {name}\n"
            for name in (*FILES, "release.env", "images.tar")
        )
        (self.root / "SHA256SUMS").write_text(data)
        return hashlib.sha256(data.encode()).hexdigest()

    def execute(self, phase: str = "all", **environment: str) -> subprocess.CompletedProcess[str]:
        """標準出力/診断を捕捉し、失敗時の秘密漏洩も検査する。"""

        result = subprocess.run(
            ["sh", "scripts/deploy.sh", phase],
            cwd=self.root,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotIn("fixture-private-error", result.stdout + result.stderr)
        return result

    def trace(self) -> list[list[str]]:
        """副作用の有無を command trace から判定する。"""

        path = self.root / "trace.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_full_deploy_stops_before_worker_and_preserves_runtime_env(self) -> None:
        """load/migrate/API/Web のみを順序通り実行する。"""

        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        starts = [args for args in self.trace() if "up" in args]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][-2:], ["api", "web"])
        self.assertEqual((self.root / "runtime.env").read_text(), "FIXTURE=not-a-secret\n")

    def test_missing_approval_or_checksum_does_not_call_docker(self) -> None:
        """未承認清単を package 内の値だけで自動承認しない。"""

        for values in ({"MAINTENANCE_CONFIRMED": ""}, {"RELEASE_SHA256": "0" * 64}):
            self.assertNotEqual(self.execute(**values).returncode, 0)
        self.assertEqual(self.trace(), [])

    def test_worker_requires_separate_approval(self) -> None:
        """維持窓の確認を Worker 起動の承認に流用しない。"""

        self.assertNotEqual(self.execute("worker").returncode, 0)
        self.assertEqual(self.trace(), [])

    def test_modified_package_file_is_rejected_before_docker(self) -> None:
        """転送中に壊れた Compose を load 前に拒否する。"""

        (self.root / "compose.yaml").write_text("tampered")
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertEqual(self.trace(), [])

    def test_compose_rejects_nonimmutable_pins_and_target_overrides(self) -> None:
        """通常入口でも短縮 image ID と別 Compose file の混入を拒否する。"""

        for args, env in (
            (["config", "--file", "other.yaml"], {}),
            (["config", "--quiet"], {"BACKEND_IMAGE_ID": "other:latest"}),
        ):
            result = subprocess.run(
                ["sh", "scripts/compose.sh", *args],
                cwd=self.root,
                env={**self.environment, **env},
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.trace(), [])

    def test_platform_mismatch_is_rejected_before_load(self) -> None:
        """amd64 と arm64 を同じ Linux という理由で混用しない。"""

        self.state["platform"] = "linux/aarch64"
        self.save_state()
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertNotIn(["image", "load"], self.trace())

    def test_active_orphan_prevents_load(self) -> None:
        """Compose service 一覧外の writer も拒否する。"""

        self.state["containers"].append(container("orphan", 8))
        self.save_state()
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertNotIn(["image", "load"], self.trace())

    def test_failed_migration_does_not_start_services(self) -> None:
        """失敗した段階から自動継続・rollback はしない。"""

        result = self.execute(SKM_FAKE_FAIL_EXACT="run --rm -T --no-deps --pull never migrate")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("up" in args for args in self.trace()))
        self.assertTrue(any("--migration-plan" in args for args in self.trace()))

    def test_worker_approval_starts_only_worker(self) -> None:
        """API/Web が確認済みなら、独立承認された Worker だけを起動する。"""

        for row in self.state["containers"][4:6]:
            row["state"] = "running"
        self.save_state()
        result = self.execute("worker", BACKGROUND_APPROVED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        starts = [args for args in self.trace() if "up" in args]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][-2:], ["120", "worker"])

    def test_web_only_starts_only_web_with_existing_worker(self) -> None:
        """既存 Backend と Worker は切替えず、Web のみ起動する。"""

        for row in self.state["containers"][4:]:
            row["state"] = "running"
        self.state["containers"][5]["image"] = "sha256:" + "f" * 64
        self.save_state()
        result = self.execute("web", WEB_ONLY_CONFIRMED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        starts = [args for args in self.trace() if "up" in args]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][-2:], ["120", "web"])
        self.assertFalse(any("migrate" in args and "run" in args for args in self.trace()))

    def test_web_only_rejects_backend_version_change(self) -> None:
        """Web flag を使って Backend 不一致を回避できない。"""

        for row in self.state["containers"][4:]:
            row["state"] = "running"
        self.state["containers"][4]["image"] = "sha256:" + "f" * 64
        self.save_state()
        self.assertNotEqual(self.execute("web", WEB_ONLY_CONFIRMED="1").returncode, 0)
        self.assertFalse(any("up" in args for args in self.trace()))

    def test_context_path_change_is_not_a_runtime_only_setting(self) -> None:
        """Web の build path と API 設定が不一致なら migrate 前に拒否する。"""

        self.state["config"]["services"]["api"]["environment"]["SKILLMIND_CONTEXT_PATH"] = "/other"
        self.save_state()
        self.assertNotEqual(self.execute("migrate").returncode, 0)
        self.assertFalse(any("run" in args and "migrate" in args for args in self.trace()))

    @unittest.skipUnless(
        os.environ.get("SKM_TEST_PWSH"), "Set SKM_TEST_PWSH for PowerShell runtime tests"
    )
    def test_powershell_exports_complete_package_without_host_python(self) -> None:
        """PowerShell 自身を起動するが、Docker と build は合成 process とする。"""

        result = subprocess.run(
            [
                os.environ["SKM_TEST_PWSH"],
                "-NoProfile",
                "-File",
                str(ROOT / "scripts/export-images.ps1"),
                "-Version",
                "fixture",
                "-EnvFile",
                str(self.root / "runtime.env"),
                "-OutputDirectory",
                str(self.root / "output"),
            ],
            env={
                **self.environment,
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_DATA_HOME": str(self.root / "data"),
            },
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        release = self.root / "output/skillmind-fixture"
        self.assertEqual(
            {path.name for path in release.iterdir()},
            {
                "compose.yaml",
                "Makefile",
                ".env.example",
                "scripts",
                "images.tar",
                "release.env",
                "SHA256SUMS",
            },
        )
        self.assertNotIn(b"\r", (release / "scripts/deploy.sh").read_bytes())
        self.assertFalse((release / "scripts/deploy.sh").read_bytes().startswith(b"\xef\xbb\xbf"))
        verified = subprocess.run(
            ["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=release, capture_output=True
        )
        self.assertEqual(verified.returncode, 0)
        self.assertIn("RELEASE_SHA256=", result.stdout)


if __name__ == "__main__":
    unittest.main()
