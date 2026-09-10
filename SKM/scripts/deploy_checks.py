"""Docker 権限なしの一時 container で既存の配備検査を再利用する。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

if __package__ in (None, ""):
    from compose import ComposeContext
    from deploy import Deployment, DeploymentError, image_id, verify_archive
else:
    from .compose import ComposeContext
    from .deploy import Deployment, DeploymentError, image_id, verify_archive


class SnapshotContext:
    """host の観測値だけを読み、外部 command や Docker socket に接続しない。"""

    def __init__(self, directory: Path, project: str) -> None:
        """秘密を含む一時観測 file を memory 上だけで解析する。"""

        self.directory = directory
        self.project_name = project
        self.config = json.loads((directory / "config.json").read_text())
        self.rows = {}
        for item in json.loads((directory / "containers.json").read_text()):
            labels = item["Config"]["Labels"] or {}
            state = item["State"]
            identifier = item["Id"]
            if identifier in self.rows:
                raise DeploymentError("container_identity_invalid")
            self.rows[identifier] = {
                "id": identifier,
                "image": item["Image"],
                "environment": item["Config"].get("Env"),
                "project": labels.get("com.docker.compose.project"),
                "service": labels.get("com.docker.compose.service"),
                "oneoff": labels.get("com.docker.compose.oneoff"),
                "state": state["Status"],
                "exit_code": state["ExitCode"],
                "health": state.get("Health", {}).get("Status"),
            }
        self.images = {}
        lines = (directory / "images.txt").read_text().splitlines()
        if len(lines) % 2:
            raise DeploymentError("image_snapshot_invalid")
        for reference, raw in zip(lines[::2], lines[1::2], strict=True):
            item = json.loads(raw)
            self.images[reference] = item

    def run(self, arguments: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Compose の設定と preflight 応答のみを提供する。"""

        if list(arguments) == ["config", "--format", "json"]:
            output = json.dumps(self.config)
        elif "skillmind.ops.preflight" in arguments:
            output = (self.directory / "preflight.json").read_text()
        else:
            raise DeploymentError("snapshot_command_not_allowed")
        return subprocess.CompletedProcess(arguments, 0, stdout=output)

    def docker_run(self, arguments: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        """固定された read-only query だけを既存 validator に供給する。"""

        args = list(arguments)
        if args == ["info", "--format", "{{.ID}}"]:
            output = (self.directory / "daemon.txt").read_text()
        elif args[:1] == ["ps"]:
            output = (self.directory / "ids.txt").read_text()
        elif args[:2] == ["container", "inspect"]:
            output = json.dumps(self.rows[args[-1]])
        elif args[:2] == ["image", "inspect"]:
            item = self.images[args[-1]]
            output = item["id"] if args[3] == "{{.Id}}" else json.dumps(item)
        else:
            raise DeploymentError("snapshot_command_not_allowed")
        return subprocess.CompletedProcess(arguments, 0, stdout=output)


def validate_state(
    directory: Path,
    project: str,
    daemon: str,
    images: dict[str, str],
    mode: str,
    context_path: str = "/skillmind",
) -> None:
    """各操作の直前・直後に同じ identity/config/health 門禁を適用する。"""

    context = SnapshotContext(directory, project)
    deployment = Deployment(cast(ComposeContext, context), daemon, images)
    deployment.target()
    deployment.installed_images()
    api = deployment.services["api"]
    web = deployment.services["web"]
    if (
        api.get("environment", {}).get("SKILLMIND_CONTEXT_PATH") != context_path
        or web.get("build", {}).get("args", {}).get("SKILLMIND_CONTEXT_PATH") != context_path
    ):
        raise DeploymentError("release_context_path_mismatch")
    worker = mode == "worker"
    frontend = mode in {"frontend", "worker", "web-before", "web-after"}
    if mode.startswith("web-"):
        worker = any(
            row["service"] == "worker" and row["state"] not in {"created", "exited", "dead"}
            for row in context.rows.values()
        )
        if mode == "web-before":
            web = [row for row in context.rows.values() if row["service"] == "web"]
            if len(web) != 1:
                raise DeploymentError("web_update_requires_existing_frontend")
            # Web だけは切替前 image を許す。Backend/設定変更は純 Web 更新では拒否する。
            deployment.images["web"] = web[0]["image"]
            deployment.services["web"]["image"] = web[0]["image"]
    deployment.gate(frontend=frontend, infrastructure=mode != "loaded", worker=worker)


def main(argv: Sequence[str] | None = None) -> int:
    """JSON の内容・環境値・traceback を公開しない一時検査入口。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("archive", "state", "preflight"))
    parser.add_argument("--backend", required=True, type=image_id)
    parser.add_argument("--web", required=True, type=image_id)
    parser.add_argument("--project", default="skillmind")
    parser.add_argument("--daemon", default="")
    parser.add_argument("--directory", type=Path, default=Path("/snapshot"))
    parser.add_argument("--archive", type=Path, default=Path("/release/images.tar"))
    parser.add_argument("--checksum", default="")
    parser.add_argument("--context-path", default="/skillmind")
    parser.add_argument(
        "--mode",
        choices=("loaded", "stopped", "frontend", "worker", "web-before", "web-after"),
        default="stopped",
    )
    parser.add_argument("--migration-plan", action="store_true")
    options = parser.parse_args(argv)
    try:
        images = {"backend": options.backend, "web": options.web}
        if options.kind == "archive":
            with options.archive.open("rb") as stream:
                verify_archive(stream, options.checksum, images)
        elif options.kind == "state":
            validate_state(
                options.directory,
                options.project,
                options.daemon,
                images,
                options.mode,
                options.context_path,
            )
        else:
            context = SnapshotContext(options.directory, options.project)
            Deployment(cast(ComposeContext, context), options.daemon, images).preflight(
                migration_plan=options.migration_plan
            )
    except (DeploymentError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            "Deployment validation failed; keep isolation and inspect state privately.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
