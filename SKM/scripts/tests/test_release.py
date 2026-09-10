"""実 Make/PowerShell と fake Docker で四 file 配備と純 export を検査する。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP_IMAGES = ("skillmind/backend:0.1.0", "skillmind/web:0.1.0")


class ShellFlagsTests(unittest.TestCase):
    """旧 Make の単一 argv 渡しでも Shell 起動と失敗停止を維持する。"""

    def test_flags_work_as_one_argument_and_preserve_error_checks(self) -> None:
        """sh/bash へ実 flags を分割せず渡し、空白混入・e/u の欠落を検知する。"""

        source = (ROOT / "Makefile").read_text()
        flags = next(
            line.split(":=", 1)[1].strip()
            for line in source.splitlines()
            if line.startswith(".SHELLFLAGS :=")
        )
        for shell in dict.fromkeys(filter(None, (shutil.which("sh"), shutil.which("bash")))):
            for command, succeeds in (
                ("printf 'shell-ok'", True),
                ("false; printf 'must-not-run'", False),
                ('unset SKM_UNSET_FIXTURE; printf "%s" "$SKM_UNSET_FIXTURE"', False),
            ):
                with self.subTest(shell=shell, command=command):
                    result = subprocess.run(
                        [shell, flags, command], capture_output=True, text=True, timeout=5
                    )
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    self.assertEqual(result.stdout, "shell-ok" if succeeds else "")


class ReleaseTests(unittest.TestCase):
    """実 service/data を触らず、一時 directory の四 file だけで配備を再現する。"""

    def setUp(self) -> None:
        """空白を含む server path と合成 image/config を用意する。"""

        self.temporary = tempfile.TemporaryDirectory(prefix="skillmind deployment test ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.server = self.base / "server files"
        self.server.mkdir()
        self.windows = self.base / "windows"
        (self.windows / "scripts").mkdir(parents=True)
        for name in ("compose.yml", "Makefile"):
            shutil.copyfile(ROOT / name, self.server / name)
        shutil.copyfile(ROOT / "compose.yml", self.windows / "compose.yml")
        shutil.copyfile(
            ROOT / "scripts/export-images.ps1", self.windows / "scripts/export-images.ps1"
        )
        (self.windows / ".env").mkdir()
        (self.server / ".env").write_text("FIXTURE=keep-existing-password\n")
        config = yaml.safe_load((ROOT / "compose.yml").read_text())
        self.images = {
            name: {"id": "sha256:" + str(index + 1) * 64, "platform": "linux/amd64"}
            for index, name in enumerate(
                dict.fromkeys(service["image"] for service in config["services"].values())
            )
        }
        self.state = {
            "config": config,
            "images": self.images.copy(),
            "containers": {},
            "revision": "base",
        }
        self.save_state()
        (self.server / "images.tar").write_text(
            json.dumps({name: self.images[name] for name in APP_IMAGES})
        )
        fake = ROOT / "scripts/tests/fake_release_docker.py"
        docker = self.base / "docker"
        docker.write_text(
            f"#!{sys.executable}\nimport runpy\n"
            f"runpy.run_path({str(fake)!r}, run_name='__main__')\n"
        )
        docker.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": f"{self.base}:{os.environ['PATH']}",
            "SKM_FAKE_ROOT": str(self.base),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENV_FILE": ".env",
        }
        for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "COMPOSE_PROJECT_NAME"):
            self.environment.pop(key, None)

    def save_state(self) -> None:
        """fake executable に公開 fixture だけを渡す。"""

        (self.base / "state.json").write_text(json.dumps(self.state))

    def trace(self) -> list[list[str]]:
        """実行済み Docker argv を返す。"""

        path = self.base / "trace.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def deploy(self, **environment: str) -> subprocess.CompletedProcess[str]:
        """実 GNU make の shell/停止動作を確認し、Docker は fixture に限定する。"""

        return subprocess.run(
            ["make", "--no-print-directory", "deploy"],
            cwd=self.server,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=90,
        )

    def export(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
        """実 PowerShell を dotenv が読めない fixture root で実行する。"""

        if not os.environ.get("SKM_TEST_PWSH"):
            self.skipTest("Set SKM_TEST_PWSH for PowerShell runtime tests")
        return subprocess.run(
            [
                os.environ["SKM_TEST_PWSH"],
                "-NoProfile",
                "-File",
                str(self.windows / "scripts/export-images.ps1"),
                *arguments,
            ],
            env={
                **self.environment,
                "XDG_CACHE_HOME": str(self.base / "cache"),
                "XDG_CONFIG_HOME": str(self.base / "config"),
                "XDG_DATA_HOME": str(self.base / "data"),
                **environment,
            },
            capture_output=True,
            text=True,
            timeout=90,
        )

    def test_first_deploy_needs_only_four_files_and_starts_worker_last(self) -> None:
        """空 server へ load・基盤・migration・API/Web・Worker の順に配備する。"""

        self.state["images"] = {}
        self.save_state()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            {p.name for p in self.server.iterdir()},
            {"compose.yml", "Makefile", ".env", "images.tar"},
        )
        self.assertEqual((self.server / ".env").read_text(), "FIXTURE=keep-existing-password\n")
        current = json.loads((self.base / "state.json").read_text())
        self.assertEqual(current["revision"], "head")
        self.assertEqual(current["containers"]["worker"], "running")
        self.assertEqual(current["containers"]["object-storage-init"], "exited(0)")
        stages = (
            "configuration",
            "load-images",
            "prepare-images",
            "stop-application",
            "infrastructure",
            "initialize-storage",
            "migration-check",
            "migrate",
            "readiness",
            "api-web",
            "worker",
            "complete",
        )
        positions = [result.stdout.index(f"[{stage}]") for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(
            any(args[0] == "run" or "build" in args or "down" in args for args in self.trace())
        )
        up = [args for args in self.trace() if "up" in args]
        self.assertTrue(
            all("--no-build" in args and "--no-deps" in args and "never" in args for args in up)
        )
        self.assertEqual(up[-1][-1], "worker")
        pull = next(args for args in self.trace() if "pull" in args)
        self.assertEqual(pull[-4:], ["postgres", "redis", "object-storage", "object-storage-init"])

    def test_update_stops_existing_application_before_migration(self) -> None:
        """既存設定を保ち、停止できた後だけ DB を変更する。"""

        self.state["containers"] = {
            name: "running"
            for name in ("api", "web", "worker", "postgres", "redis", "object-storage")
        }
        self.save_state()
        result = self.deploy(SKM_FAKE_OFFLINE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace()
        stop = next(index for index, args in enumerate(trace) if "stop" in args)
        migrate = next(
            index for index, args in enumerate(trace) if "run" in args and args[-1] == "migrate"
        )
        self.assertLess(stop, migrate)
        self.assertIn("worker", trace[stop])
        self.assertNotIn("postgres", trace[stop])

    def test_failures_report_stage_and_never_continue(self) -> None:
        """失敗 stage 以降を実行せず、native Docker error を利用者へ残す。"""

        for suffix, stage, next_stage in (
            ("image load --input images.tar", "load-images", "prepare-images"),
            (
                "stop --timeout 60 api web worker migrate object-storage-init",
                "stop-application",
                "infrastructure",
            ),
            (
                "up -d --no-build --no-deps --pull never --wait "
                "--wait-timeout 120 postgres redis object-storage",
                "infrastructure",
                "initialize-storage",
            ),
            (
                "--exit-code-from object-storage-init object-storage-init",
                "initialize-storage",
                "migration-check",
            ),
            (
                "migrate python -m skillmind.ops.preflight --migration-plan",
                "migration-check",
                "migrate",
            ),
            ("run --rm -T --no-deps migrate", "migrate", "readiness"),
            ("migrate python -m skillmind.ops.preflight", "readiness", "api-web"),
            ("--wait-timeout 120 api web", "api-web", "worker"),
            ("--wait-timeout 120 worker", "worker", "complete"),
        ):
            with self.subTest(stage=stage):
                result = self.deploy(SKM_FAKE_FAIL_SUFFIX=suffix)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Deployment failed at [{stage}]", result.stderr)
                self.assertIn("fixture docker failure", result.stderr)
                self.assertNotIn(f"[{next_stage}]", result.stdout)

    def test_missing_input_stops_before_docker(self) -> None:
        """不完全な転送や空設定では service に触れない。"""

        for name in ("images.tar", ".env"):
            with self.subTest(name=name):
                original = (self.server / name).read_bytes()
                (self.server / name).write_bytes(b"")
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("[configuration]", result.stderr)
                self.assertEqual(self.trace(), [])
                (self.server / name).write_bytes(original)

    def test_interruption_stops_before_migration(self) -> None:
        """子 command が終了 code 0 でも TERM を受けた配備は続行しない。"""

        result = self.deploy(
            SKM_FAKE_TERMINATE_SUFFIX="migrate python -m skillmind.ops.preflight --migration-plan"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[migration-check]", result.stderr)
        self.assertNotIn("[migrate]", result.stdout)

    def test_offline_first_deploy_uses_bundled_infrastructure(self) -> None:
        """全 image を含む一つの tar なら初回も registry 接続を必要としない。"""

        (self.server / "images.tar").write_text(json.dumps(self.images))
        self.state["images"] = {}
        self.save_state()
        result = self.deploy(SKM_FAKE_OFFLINE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unavailable_infrastructure_stops_before_existing_services(self) -> None:
        """offline で基盤 image が不足しても既存 application を停止しない。"""

        self.state["images"] = {}
        self.save_state()
        result = self.deploy(SKM_FAKE_OFFLINE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[prepare-images]", result.stderr)
        self.assertFalse(any("stop" in args for args in self.trace()))

    def test_platform_mismatch_stops_before_service_changes(self) -> None:
        """archive の architecture と server が違えば停止・migration 前に拒否する。"""

        self.state["server_platform"] = "linux/aarch64"
        self.save_state()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Image platform mismatch", result.stderr)
        self.assertFalse(any("stop" in args for args in self.trace()))

    def test_custom_environment_file_is_shared_by_all_compose_calls(self) -> None:
        """特殊 path でも補間/注入を同じ file にし、source/eval は使わない。"""

        selected = self.base / "custom configuration.env"
        selected.write_text("FIXTURE=custom\n")
        result = self.deploy(ENV_FILE=str(selected))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for args in self.trace():
            if args[0] == "compose":
                self.assertEqual(args[args.index("--env-file") + 1], str(selected))

    def test_storage_initialization_retries_are_bounded_and_private(self) -> None:
        """Compose 内の実 shell command を fake mc/sleep で実行し、無限待機を防ぐ。"""

        command = self.state["config"]["services"]["object-storage-init"]["command"][0]
        harness = """
calls=0
mc() {
    if [ "$1" = alias ]; then
        calls=$((calls + 1))
        [ "$calls" -ge "$READY_AT" ]
    else
        printf '%s\n' "$*"
    fi
}
sleep() { :; }
trap 'printf "attempts=%s\\n" "$calls"' 0
"""
        for ready_at, code, attempts in ((2, 0, 2), (999, 1, 60)):
            with self.subTest(ready_at=ready_at):
                result = subprocess.run(
                    ["sh", "-ec", harness + command.replace("$$", "$")],
                    env={
                        **self.environment,
                        "READY_AT": str(ready_at),
                        "MINIO_ROOT_USER": "fixture-user",
                        "MINIO_ROOT_PASSWORD": "fixture-password",
                        "SKILLMIND_OBJECT_STORAGE_BUCKET": "fixture-bucket",
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(f"attempts={attempts}", result.stdout)
                self.assertNotIn("fixture-password", result.stdout + result.stderr)
                if code == 0:
                    self.assertIn("mb --ignore-existing skillmind/fixture-bucket", result.stdout)
                else:
                    self.assertIn("MinIO initialization failed", result.stderr)

    def test_export_produces_only_tar_without_build_pull_or_container(self) -> None:
        """既定 tag を保存するだけで、追加 file や公開環境値を生成しない。"""

        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.windows / "images"
        self.assertEqual({p.name for p in output.iterdir()}, {"images.tar"})
        self.assertEqual(set(json.loads((output / "images.tar").read_text())), set(APP_IMAGES))
        self.assertTrue(
            all(args[:2] in (["image", "inspect"], ["image", "save"]) for args in self.trace())
        )
        self.assertTrue(
            (ROOT / "scripts/export-images.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
        )

    def test_export_force_replaces_only_archive_and_preserves_old_on_failure(self) -> None:
        """明示 Force のみ置換し、save 失敗時は旧 archive の byte を保全する。"""

        output = self.windows / "images"
        output.mkdir()
        archive = output / "images.tar"
        archive.write_text("previous archive")
        sentinel = output / "keep.txt"
        sentinel.write_text("keep")
        self.assertNotEqual(self.export().returncode, 0)
        self.assertEqual(self.trace(), [])
        # save の固定 argv は動的 partial path を含むため image inspect で失敗を注入する。
        failed = self.export("-Force", SKM_FAKE_FAIL_SUFFIX="skillmind/web:0.1.0 --format {{.Id}}")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(archive.read_text(), "previous archive")
        result = self.export("-Force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(json.loads(archive.read_text())), set(APP_IMAGES))
        self.assertEqual(sentinel.read_text(), "keep")

    def test_export_save_failure_keeps_existing_archive(self) -> None:
        """途中保存の失敗でも既存 archive を消去・上書きしない。"""

        output = self.windows / "images"
        output.mkdir()
        archive = output / "images.tar"
        archive.write_text("previous archive")
        result = self.export(
            "-Force", SKM_FAKE_FAIL_SUFFIX="skillmind/backend:0.1.0 skillmind/web:0.1.0"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker 'image save' failed (exit code 17)", result.stderr)
        self.assertIn("fixture docker failure", result.stderr)
        self.assertEqual(archive.read_text(), "previous archive")

    def test_export_infrastructure_uses_local_images_without_dotenv(self) -> None:
        """任意の完全 offline export でも dotenv 解析や pull を行わない。"""

        result = self.export("-IncludeInfrastructure", COMPOSE_ENV_FILES="missing.env")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        archive = self.windows / "images/images.tar"
        self.assertEqual(set(json.loads(archive.read_text())), set(self.images))
        self.assertFalse(
            any(args[0] in {"pull", "run"} or "build" in args for args in self.trace())
        )

    def test_export_force_rejects_directory_or_link(self) -> None:
        """Force でも別 file への link や directory は破壊しない。"""

        output = self.windows / "images"
        output.mkdir()
        (output / "images.tar").mkdir()
        self.assertNotEqual(self.export("-Force").returncode, 0)
        (output / "images.tar").rmdir()
        target = output / "keep.txt"
        target.write_text("keep")
        (output / "images.tar").symlink_to(target)
        self.assertNotEqual(self.export("-Force").returncode, 0)
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(self.trace(), [])


if __name__ == "__main__":
    unittest.main()
