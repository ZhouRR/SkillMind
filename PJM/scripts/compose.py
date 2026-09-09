"""Compose の補間と Backend env_file を同じ明示的な対象へ固定する。"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import IO, Never

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_VARIABLE = "PJM_COMPOSE_ENV_FILE"
DEFAULT_IMAGES = {"backend": "projectmind/backend:0.1.0", "web": "projectmind/web:0.1.0"}
IMAGE_VARIABLES = {"backend": "PJM_BACKEND_IMAGE", "web": "PJM_WEB_IMAGE"}
Runner = Callable[..., subprocess.CompletedProcess[str]]


class ComposeError(ValueError):
    """環境値や元の argv を含めず呼出元へ伝えられる安全な設定拒否。"""


def _regular_file(path: Path, label: str) -> Path:
    """内容を読まず、存在する通常 file の絶対 path に限定する。"""

    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ComposeError(f"Selected {label} is not a regular file.")
    except (OSError, RuntimeError, ValueError) as error:
        raise ComposeError(f"Selected {label} does not exist or cannot be resolved.") from error
    return resolved


def _blocked_option(token: str, command: str) -> bool:
    """固定した設定源、Project、Docker daemon の上書き option を拒否する。"""

    name = token.split("=", 1)[0]
    return (
        name
        in {
            "--file",
            "--env-file",
            "--project-name",
            "--project-directory",
            "--env-from-file",
            "--context",
            "--host",
            "--config",
            "--profile",
            "--compatibility",
        }
        or any(token.startswith(prefix) for prefix in ("-p", "-H", "-c"))
        or (token.startswith("-f") and command != "logs")
    )


def _validate_compose_args(args: Sequence[str]) -> tuple[str, ...]:
    """exec/run の service 後だけを容器 command とし、そこでの同名 option は保持する。"""

    if any(not isinstance(value, str) or "\0" in value for value in args):
        raise ComposeError("Compose arguments must be valid text.")
    if not args or not re.fullmatch(r"[a-z][a-z0-9-]*", args[0]):
        raise ComposeError("A Compose subcommand must precede its arguments.")
    command = args[0]
    value_options = {
        "--env",
        "-e",
        "--index",
        "--user",
        "-u",
        "--workdir",
        "-w",
        "--entrypoint",
        "--name",
        "--label",
        "-l",
        "--volume",
        "-v",
        "--cap-add",
        "--cap-drop",
        "--pull",
    }
    flag_options = {
        "--detach",
        "-d",
        "--no-TTY",
        "--no-tty",
        "-T",
        "--interactive",
        "-i",
        "--privileged",
        "--rm",
        "--no-deps",
        "--build",
        "--quiet",
        "-q",
        "--quiet-pull",
        "--quiet-build",
        "--remove-orphans",
        "--service-ports",
        "--use-aliases",
    }
    index = 1
    while index < len(args):
        token = args[index]
        if _blocked_option(token, command):
            raise ComposeError("Compose target overrides are not allowed in command arguments.")
        if command not in {"exec", "run"}:
            index += 1
            continue
        if token == "--":
            if index + 1 >= len(args):
                raise ComposeError("The container service is missing.")
            return tuple(args)
        if not token.startswith("-"):
            return tuple(args)
        option = token.split("=", 1)[0]
        if option in value_options:
            if "=" not in token:
                index += 1
                if index >= len(args):
                    raise ComposeError("A container option value is missing.")
        elif option not in flag_options:
            raise ComposeError("Unsupported option before the container service.")
        index += 1
    if command in {"exec", "run"}:
        raise ComposeError("The container service is missing.")
    return tuple(args)


@dataclass(frozen=True)
class ComposeContext:
    """選択した設定源・Project・daemon 環境を一連の操作で共有する。"""

    root: Path
    env_file: Path
    compose_file: Path
    project_name: str
    child_env: Mapping[str, str]

    @classmethod
    def resolve(
        cls,
        env_file: str | Path | None = None,
        *,
        project_name: str | None = None,
        root: Path = PROJECT_ROOT,
        environ: Mapping[str, str] | None = None,
        image_ids: Mapping[str, str] | None = None,
    ) -> ComposeContext:
        """明示値 > shell > 既定値で対象を確定し、dotenv の解釈自体は Compose に任せる。"""

        environment = dict(os.environ if environ is None else environ)
        try:
            project_root = root.resolve(strict=True)
            if not project_root.is_dir():
                raise ComposeError("The project directory is invalid.")
        except (OSError, RuntimeError) as error:
            raise ComposeError("The project directory cannot be resolved.") from error
        selected = env_file if env_file is not None else environment.get("ENV_FILE", ".env")
        if not str(selected):
            raise ComposeError("The selected environment file is empty.")
        selected_path = Path(selected)
        if not selected_path.is_absolute():
            selected_path = project_root / selected_path
        resolved_env = _regular_file(selected_path, "environment file")
        compose_file = _regular_file(project_root / "compose.yaml", "Compose file")
        name = (
            project_name
            if project_name is not None
            else environment.get("COMPOSE_PROJECT_NAME", "projectmind")
        )
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ComposeError("The Compose project name is invalid.")
        pins = dict(image_ids or {})
        if set(pins) - set(DEFAULT_IMAGES) or any(
            not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for value in pins.values()
        ):
            raise ComposeError("Image pins must be backend/web immutable sha256 identities.")

        # shell/.env の暗黙的な file・profile 選択を止め、相対 cwd にも依存させない。
        for key in (
            "COMPOSE_FILE",
            "COMPOSE_ENV_FILES",
            "COMPOSE_PROFILES",
            "COMPOSE_PATH_SEPARATOR",
        ):
            environment.pop(key, None)
        environment[SOURCE_VARIABLE] = str(resolved_env)
        environment["ENV_FILE"] = str(resolved_env)
        environment["COMPOSE_PROJECT_NAME"] = name
        environment["COMPOSE_DISABLE_ENV_FILE"] = "true"
        environment["COMPOSE_PROFILES"] = ""
        environment["COMPOSE_COMPATIBILITY"] = "false"
        for service, variable in IMAGE_VARIABLES.items():
            environment[variable] = pins.get(service, DEFAULT_IMAGES[service])
        return cls(project_root, resolved_env, compose_file, name, MappingProxyType(environment))

    def command(self, args: Sequence[str]) -> list[str]:
        """固定した絶対 path と明示 Project を argv として渡し、shell を経由しない。"""

        return [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "--file",
            str(self.compose_file),
            "--env-file",
            str(self.env_file),
            "--project-name",
            self.project_name,
            *_validate_compose_args(args),
        ]

    def run(
        self,
        args: Sequence[str],
        *,
        runner: Runner = subprocess.run,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        stdin: IO[str] | IO[bytes] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """公開/移行側にも同じ Compose context を渡し、出力の公開判断は呼出元へ残す。"""

        return runner(
            self.command(args),
            cwd=self.root,
            env=dict(self.child_env),
            text=True,
            capture_output=capture_output,
            check=check,
            timeout=timeout,
            stdin=stdin,
        )

    def docker_run(
        self,
        args: Sequence[str],
        *,
        runner: Runner = subprocess.run,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        stdin: IO[str] | IO[bytes] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """image inspect/load も Compose と同じ daemon/cwd を使い、後付け daemon 切替を拒否する。"""

        if any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise ComposeError("Docker arguments must be valid text.")
        if not args or not re.fullmatch(r"[a-z][a-z0-9-]*", args[0]) or args[0] == "compose":
            raise ComposeError("A Docker subcommand other than Compose is required.")
        if any(
            arg.split("=", 1)[0] in {"--context", "--host", "--config"}
            or arg.startswith(("-H", "-c"))
            for arg in args
        ):
            raise ComposeError("Docker target overrides are not allowed in command arguments.")
        return runner(
            ["docker", *args],
            cwd=self.root,
            env=dict(self.child_env),
            text=True,
            capture_output=capture_output,
            check=check,
            timeout=timeout,
            stdin=stdin,
        )


class _SafeArgumentParser(argparse.ArgumentParser):
    """未知 option に付随する値を argparse の標準 error 表示へ流さない。"""

    def error(self, message: str) -> Never:
        """parser の詳細には元引数が含まれるため、公開可能な固定文だけを返す。"""

        raise ComposeError("Invalid Compose wrapper arguments; use --help for usage.")


def main(argv: Sequence[str] | None = None) -> int:
    """通常 stdout/TTY は保持し、失敗時も元 argv/env/stderr を例外表示しない。"""

    # Docker-only host では Backend の package 制約が適用されないため CLI 自身で拒否する。
    if sys.version_info < (3, 12):  # noqa: UP036
        print("Python 3.12 or newer is required.", file=sys.stderr)
        return 2
    parser = _SafeArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--env-file")
    parser.add_argument("--project-name", "-p")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    try:
        options = parser.parse_args(argv)
        context = ComposeContext.resolve(options.env_file, project_name=options.project_name)
        command = options.command[1:] if options.command[:1] == ["--"] else options.command
        # logs --follow を全量 memory に溜めず、失敗理由に混ざる設定値だけ非公開にする。
        result = context.run(
            command,
            runner=partial(subprocess.run, stderr=subprocess.PIPE),
            check=False,
        )
    except ComposeError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print("Docker Compose could not be executed.", file=sys.stderr)
        return 1
    if result.returncode:
        print("Docker Compose failed; review the selected environment privately.", file=sys.stderr)
        return result.returncode if result.returncode > 0 else 1
    # 成功時に要求された通常出力だけを返す。config 全文の公開は CLI の明示操作に限る。
    if result.stdout:
        print(result.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
