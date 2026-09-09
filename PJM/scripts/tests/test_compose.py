"""合成 file と偽子 process だけで Compose の設定源・対象固定を検証する。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import compose


class ComposeContextTests(unittest.TestCase):
    """本物の .env や Docker daemon に触らない共用 context の回帰。"""

    def setUp(self) -> None:
        """空白を含む合成 root を用意し、設定値は公開可能な固定文字列だけにする。"""

        directory = tempfile.TemporaryDirectory(prefix="projectmind compose test ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text("PUBLIC_SAMPLE=file\nCOMPOSE_PROJECT_NAME=file-ignored\n")
        (self.root / "compose.yaml").write_text("services: {}\n")
        self.custom = self.root / "config directory" / "selected config.env"
        self.custom.parent.mkdir()
        self.custom.write_text("PUBLIC_SAMPLE=custom\n")

    def context(self, **kwargs: object) -> compose.ComposeContext:
        """実 shell 環境を引き継がず、試験で明示した値だけを解決する。"""

        return compose.ComposeContext.resolve(root=self.root, environ={}, **kwargs)

    def test_default_uses_root_without_reading_dotenv(self) -> None:
        """対象は file 内容を parser へ重複実装せず、存在と絶対 path で確定する。"""

        with patch.object(Path, "read_text", side_effect=AssertionError("no dotenv parser")):
            context = self.context()
        self.assertEqual(context.env_file, self.env_file)
        self.assertEqual(context.project_name, "projectmind")
        self.assertEqual(context.child_env[compose.SOURCE_VARIABLE], str(self.env_file))

    def test_explicit_and_shell_paths_use_project_root_not_caller_cwd(self) -> None:
        """空白 path を一引数で保ち、明示値が shell の選択を上書きする。"""

        relative = self.custom.relative_to(self.root)
        context = compose.ComposeContext.resolve(
            relative, root=self.root, environ={"ENV_FILE": "missing.env"}
        )
        self.assertEqual(context.env_file, self.custom)
        shell_context = compose.ComposeContext.resolve(
            root=self.root, environ={"ENV_FILE": str(relative)}
        )
        self.assertEqual(shell_context.env_file, self.custom)
        self.assertEqual(self.context(env_file=self.custom).env_file, self.custom)

    def test_missing_empty_or_directory_source_is_rejected(self) -> None:
        """実行前に欠損や directory を拒否し、path の元文字列を例外へ載せない。"""

        for selected in ("", "private-marker-missing.env", self.custom.parent):
            with self.subTest(selected=selected), self.assertRaises(compose.ComposeError) as caught:
                self.context(env_file=selected)
            self.assertNotIn("private-marker", str(caught.exception))

    def test_empty_shell_source_is_not_silently_defaulted(self) -> None:
        """空値を既定 file へ切り替えず、意図を人間へ確認できる失敗にする。"""

        with self.assertRaises(compose.ComposeError):
            compose.ComposeContext.resolve(root=self.root, environ={"ENV_FILE": ""})

    def test_missing_root_or_compose_is_rejected(self) -> None:
        """存在する別 cwd の Compose file に fallback しない。"""

        with self.assertRaises(compose.ComposeError):
            compose.ComposeContext.resolve(root=self.root / "absent", environ={})
        (self.root / "compose.yaml").unlink()
        with self.assertRaises(compose.ComposeError):
            self.context()

    @unittest.skipIf(os.name == "nt", "Windows の symlink 権限に依存しない")
    def test_symlink_source_has_one_canonical_absolute_path(self) -> None:
        """補間と container 注入が別の symlink 表記に分かれない。"""

        alias = self.root / "alias.env"
        alias.symlink_to(self.custom)
        context = self.context(env_file=alias)
        self.assertEqual(context.env_file, self.custom)
        self.assertEqual(context.child_env[compose.SOURCE_VARIABLE], str(self.custom))

    def test_project_name_precedence_and_validation(self) -> None:
        """.env の project 名は使わず、明示値、shell、既定値の順を保つ。"""

        for explicit, shell, expected in (
            (None, {}, "projectmind"),
            (None, {"COMPOSE_PROJECT_NAME": "shell-project"}, "shell-project"),
            ("explicit_2", {"COMPOSE_PROJECT_NAME": "shell-project"}, "explicit_2"),
        ):
            with self.subTest(explicit=explicit, shell=shell):
                context = compose.ComposeContext.resolve(
                    root=self.root, environ=shell, project_name=explicit
                )
                self.assertEqual(context.project_name, expected)
                self.assertEqual(context.child_env["COMPOSE_PROJECT_NAME"], expected)
        for invalid in ("", "UPPER", "../private-marker", "space name", "-leading"):
            with self.subTest(invalid=invalid), self.assertRaises(compose.ComposeError):
                self.context(project_name=invalid)

    def test_child_environment_fixes_selectors_but_preserves_ordinary_shell_values(self) -> None:
        """通常補間の shell 優先と Docker daemon は保持し、source 選択だけ固定する。"""

        environment = {
            "COMPOSE_FILE": "other.yaml",
            "COMPOSE_ENV_FILES": "other.env",
            "COMPOSE_PATH_SEPARATOR": "!",
            "COMPOSE_PROFILES": "unexpected",
            "COMPOSE_COMPATIBILITY": "true",
            "COMPOSE_DISABLE_ENV_FILE": "false",
            "PJM_COMPOSE_ENV_FILE": "other.env",
            "PJM_BACKEND_IMAGE": "untrusted/backend:latest",
            "PJM_WEB_IMAGE": "untrusted/web:latest",
            "PUBLIC_SAMPLE": "shell-wins",
            "DOCKER_HOST": "unix:///synthetic-daemon.sock",
            "DOCKER_CONTEXT": "synthetic-context",
        }
        context = compose.ComposeContext.resolve(root=self.root, environ=environment)
        self.assertEqual(context.child_env["PUBLIC_SAMPLE"], "shell-wins")
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT"):
            self.assertEqual(context.child_env[key], environment[key])
        for key in ("COMPOSE_FILE", "COMPOSE_ENV_FILES", "COMPOSE_PATH_SEPARATOR"):
            self.assertNotIn(key, context.child_env)
        self.assertEqual(context.child_env["COMPOSE_PROFILES"], "")
        self.assertEqual(context.child_env["COMPOSE_COMPATIBILITY"], "false")
        self.assertEqual(context.child_env["COMPOSE_DISABLE_ENV_FILE"], "true")
        self.assertEqual(context.child_env["ENV_FILE"], str(self.env_file))
        for role, variable in compose.IMAGE_VARIABLES.items():
            self.assertEqual(context.child_env[variable], compose.DEFAULT_IMAGES[role])
        self.assertEqual(environment["COMPOSE_PROFILES"], "unexpected")
        with self.assertRaises(TypeError):
            context.child_env["ENV_FILE"] = "changed"  # type: ignore[index]

    def test_image_pins_are_strict_role_specific_and_override_shell(self) -> None:
        """承認した immutable identity だけを選び、省略した役割は既定 tag へ固定する。"""

        pin = "sha256:" + "1a" * 32
        context = self.context(image_ids={"backend": pin})
        self.assertEqual(context.child_env["PJM_BACKEND_IMAGE"], pin)
        self.assertEqual(context.child_env["PJM_WEB_IMAGE"], compose.DEFAULT_IMAGES["web"])
        for invalid in (
            {"worker": pin},
            {"backend": "latest"},
            {"web": "sha256:" + "A" * 64},
            {"web": "sha256:" + "a" * 63},
            {"backend": None},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(compose.ComposeError):
                self.context(image_ids=invalid)

    def test_fixed_command_preserves_spaces_and_one_source(self) -> None:
        """shell quoting ではなく argv 境界で空白を保護する。"""

        context = self.context(env_file=self.custom, project_name="explicit")
        self.assertEqual(
            context.command(["config", "--images"]),
            [
                "docker",
                "compose",
                "--project-directory",
                str(self.root),
                "--file",
                str(self.root / "compose.yaml"),
                "--env-file",
                str(self.custom),
                "--project-name",
                "explicit",
                "config",
                "--images",
            ],
        )

    def test_global_target_overrides_are_rejected(self) -> None:
        """後置 selector を global option として Docker へ再解釈させない。"""

        for option in (
            "--file=other.yaml",
            "-fother.yaml",
            "--env-file",
            "--project-name=other",
            "-pother",
            "--project-directory",
            "--context=other",
            "--host=other",
            "-Hother",
            "-cother",
            "--config=other",
            "--profile=other",
            "--compatibility",
        ):
            for prefix in (["config"], ["run", "--rm"]):
                with (
                    self.subTest(option=option, prefix=prefix),
                    self.assertRaises(compose.ComposeError),
                ):
                    self.context().command([*prefix, option])

    def test_container_command_body_is_not_reinterpreted(self) -> None:
        """exec/run の本来の command 引数にある同名 option を妨げない。"""

        for prefix in (
            ["exec", "-T", "--index", "1", "api"],
            ["run", "--rm", "-T", "--no-deps", "--pull", "never", "migrate"],
            ["run", "--env", "PUBLIC_SAMPLE=x y", "--", "migrate"],
        ):
            args = [*prefix, "python", "--env-file", "command.env", "-p", "command-only"]
            with self.subTest(prefix=prefix):
                self.assertEqual(self.context().command(args)[-len(args) :], args)

    def test_missing_container_service_or_option_value_is_rejected(self) -> None:
        """未完 option を service と誤認せず、未対応 option は閉じた失敗にする。"""

        for args in (
            [],
            ["--file", "other"],
            ["run"],
            ["exec", "--index"],
            ["run", "--"],
            ["run", "--unsupported", "migrate"],
            [3],
            ["ps", "x\0y"],
        ):
            with self.subTest(args=args), self.assertRaises(compose.ComposeError):
                self.context().command(args)

    def test_normal_logs_config_and_deployment_options_remain_available(self) -> None:
        """logs の短い follow option と配備で使う長い option を退行させない。"""

        for args in (
            ["logs", "-f", "--tail", "100", "api"],
            ["config", "--format", "json"],
            [
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "120",
                "api",
                "web",
            ],
        ):
            with self.subTest(args=args):
                self.assertEqual(self.context().command(args)[-len(args) :], args)

    def test_both_runners_share_fixed_cwd_environment_and_control_options(self) -> None:
        """inspect/load も Compose と同じ daemon context と明示期限を受け取る。"""

        context = self.context()
        input_stream = io.BytesIO(b"synthetic archive")
        for method, args in (
            (context.run, ["config", "--images"]),
            (context.docker_run, ["image", "load"]),
        ):
            runner = Mock(return_value=subprocess.CompletedProcess(args, 0, "ok", ""))
            method(args, runner=runner, capture_output=True, timeout=5, stdin=input_stream)
            self.assertEqual(
                runner.call_args.kwargs,
                {
                    "cwd": self.root,
                    "env": dict(context.child_env),
                    "text": True,
                    "capture_output": True,
                    "check": True,
                    "timeout": 5,
                    "stdin": input_stream,
                },
            )

    def test_docker_runner_rejects_daemon_overrides_but_accepts_inspection_templates(self) -> None:
        """共用 runner 経由の Compose 迂回を止め、通常 format/filter を壊さない。"""

        context = self.context()
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        for args in (
            ["compose", "ps"],
            ["info", "--context=x"],
            ["info", "-cx"],
            ["info", "-H", "x"],
            ["info", "--config=x"],
            [1],
            ["info", "x\0y"],
        ):
            with self.subTest(args=args), self.assertRaises(compose.ComposeError):
                context.docker_run(args, runner=runner)
        runner.assert_not_called()
        for args in (
            ["info", "--format", "{{.ID}}"],
            ["image", "inspect", "-f", "{{.Id}}", "synthetic-image"],
            ["ps", "--all", "--quiet", "--no-trunc", "--filter", "label=synthetic"],
            ["container", "inspect", "--format", '{"environment":{{json .Config.Env}}}', "id"],
        ):
            context.docker_run(args, runner=runner)
            self.assertEqual(runner.call_args.args[0], ["docker", *args])

    def test_real_fake_child_sees_one_source_and_default_check_stops_failure(self) -> None:
        """Docker の代わりに Python 子 process を起動し、実 argv/env/check を検証する。"""

        context = self.context(env_file=self.custom)
        program = (
            "import json, os, sys; "
            "print(json.dumps({'argv':sys.argv[1:], 'cwd':os.getcwd(), "
            "'source':os.environ['PJM_COMPOSE_ENV_FILE']}))"
        )

        def fake_child(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            """渡された実 argv を Python の検査 process へ転送する。"""

            return subprocess.run([sys.executable, "-B", "-c", program, *args], **kwargs)

        result = context.run(["config", "--images"], runner=fake_child, capture_output=True)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["source"], str(self.custom))
        self.assertEqual(observed["cwd"], str(self.root))
        self.assertEqual(observed["argv"], context.command(["config", "--images"]))

        def fail_child(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            """外部副作用なしで check 引数の本来の例外動作を確かめる。"""

            return subprocess.run([sys.executable, "-B", "-c", "raise SystemExit(7)"], **kwargs)

        for method in (context.run, context.docker_run):
            with self.assertRaises(subprocess.CalledProcessError):
                method(["version"], runner=fail_child, capture_output=True)
            self.assertEqual(method(["version"], runner=fail_child, check=False).returncode, 7)

    def test_cli_accepts_double_dash_and_keeps_private_stderr(self) -> None:
        """Make/docs の -- 境界を real main で通し、成功 stdout 以外は公開しない。"""

        context = self.context(env_file=self.custom)
        output, errors = io.StringIO(), io.StringIO()
        result = subprocess.CompletedProcess([], 0, "public-image\n", "private-marker")
        with (
            patch.object(compose.ComposeContext, "resolve", return_value=context),
            patch.object(compose.subprocess, "run", return_value=result) as runner,
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = compose.main(["--env-file", str(self.custom), "--", "config", "--images"])
        self.assertEqual(status, 0)
        self.assertEqual(runner.call_args.args[0], context.command(["config", "--images"]))
        self.assertEqual(runner.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertFalse(runner.call_args.kwargs["capture_output"])
        self.assertEqual(output.getvalue(), "public-image\n")
        self.assertEqual(errors.getvalue(), "")

    def test_cli_errors_never_print_raw_arguments_or_captured_failure(self) -> None:
        """parser/subprocess の private marker を例外表示へ漏らさない。"""

        for args, failure in (
            (["--unrecognized=private-marker"], None),
            (["--", "ps"], subprocess.CompletedProcess([], 8, "", "private-marker")),
            (["--", "ps"], OSError("private-marker")),
            (["--", "ps"], subprocess.TimeoutExpired(["private-marker"], 1)),
        ):
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(compose.ComposeContext, "resolve", return_value=self.context()),
                patch.object(compose.subprocess, "run") as runner,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                if isinstance(failure, Exception):
                    runner.side_effect = failure
                else:
                    runner.return_value = failure
                status = compose.main(args)
            self.assertNotEqual(status, 0)
            self.assertNotIn("private-marker", output.getvalue() + errors.getvalue())
            if failure is None:
                runner.assert_not_called()

    def test_cli_rejects_old_python_before_any_context_or_subprocess(self) -> None:
        """Docker-only host の新前提条件を曖昧な実行時例外にしない。"""

        with (
            patch.object(compose.sys, "version_info", (3, 11)),
            patch.object(compose.ComposeContext, "resolve") as resolver,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(compose.main(["--", "ps"]), 2)
        resolver.assert_not_called()


class ExportSourceContractTests(unittest.TestCase):
    """PowerShell 不在でも守れる構造検査。native runtime の実測とは区別する。"""

    def test_export_uses_shared_runner_and_explicit_application_roles(self) -> None:
        """dotenv の独自解決や prefix 判定の再導入を検知する。"""

        source = (compose.PROJECT_ROOT / "scripts/export-images.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn('[string]$PythonCommand = "python"', source)
        self.assertIn('$composeArguments += @("--", "config", "--images")', source)
        self.assertIn("& $pythonExecutable.Source @composeArguments", source)
        self.assertNotIn("docker compose --env-file", source)
        self.assertNotIn("$resolvedEnvFile", source)
        self.assertNotIn('.StartsWith("projectmind/"', source)
        for role, tag in compose.DEFAULT_IMAGES.items():
            self.assertIn(f'{role} = "{tag}"', source)

    def test_export_replaces_only_after_success_without_deleting_old_archive(self) -> None:
        """失敗時の削除対象はこの実行で予約した temp 一つに限定する。"""

        source = (compose.PROJECT_ROOT / "scripts/export-images.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("[System.IO.FileMode]::CreateNew", source)
        self.assertIn("& docker image save --output $temporaryPath @images", source)
        self.assertIn("(Get-Item -LiteralPath $temporaryPath).Length -le 0", source)
        self.assertIn("[System.IO.File]::Replace($temporaryPath, $archivePath, $null)", source)
        self.assertIn("[System.IO.File]::Move($temporaryPath, $archivePath)", source)
        self.assertNotIn("Remove-Item -LiteralPath $archivePath", source)
        self.assertIn("Remove-Item -LiteralPath $temporaryPath -Force", source)
        self.assertLess(
            source.index("image save --output"), source.index("[System.IO.File]::Replace")
        )


if __name__ == "__main__":
    unittest.main()
