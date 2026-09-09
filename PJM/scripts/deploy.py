"""明示承認された配備を、実状態を再検査する独立段階として進める。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from scripts.compose import DEFAULT_IMAGES, ComposeContext, ComposeError
elif __package__ in (None, ""):
    from compose import DEFAULT_IMAGES, ComposeContext, ComposeError
else:
    from .compose import DEFAULT_IMAGES, ComposeContext, ComposeError

APP_TAGS = DEFAULT_IMAGES
INFRASTRUCTURE = {"postgres", "redis", "object-storage", "object-storage-init"}
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
INSPECT_CONTAINER = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"environment":{{json .Config.Env}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
    '"state":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'
)


class DeploymentError(RuntimeError):
    """Secret や子 command の原文を含まない停止理由を表す。"""


def json_object(value: str, reason: str) -> dict[str, object]:
    """外部 command の必須 mapping を推測で補完せず検査する。"""

    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as error:
        raise DeploymentError(reason) from error
    if not isinstance(parsed, dict):
        raise DeploymentError(reason)
    return parsed


def image_id(value: str) -> str:
    """可変 tag や短い digest を配備承認の image identity に使わせない。"""

    if not IMAGE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "image ID must be sha256 followed by 64 lowercase hex digits"
        )
    return value


def environment_values(value: object) -> dict[str, str]:
    """Docker の環境 list を内部比較だけに使い、重複や不正値を拒否する。"""

    if value is None:
        return {}
    if not isinstance(value, list):
        raise DeploymentError("container_environment_invalid")
    result = {}
    for item in value:
        if not isinstance(item, str) or "=" not in item:
            raise DeploymentError("container_environment_invalid")
        key, content = item.split("=", 1)
        if not key or key in result:
            raise DeploymentError("container_environment_invalid")
        result[key] = content
    return result


def archive_member(archive: tarfile.TarFile, name: str, limit: int) -> bytes:
    """展開せず、小さな正規 file だけを一意に読み込む。"""

    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not name or "\\" in name:
        raise DeploymentError("archive_metadata_path_invalid")
    matches = [member for member in archive.getmembers() if member.name == name]
    if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size <= limit:
        raise DeploymentError("archive_metadata_invalid")
    extracted = archive.extractfile(matches[0])
    if extracted is None:
        raise DeploymentError("archive_metadata_missing")
    with extracted:
        return extracted.read(limit + 1)


def verify_archive(stream: BinaryIO, checksum: str, images: Mapping[str, str]) -> None:
    """同じ open file の checksum と Docker save の application config identity を検査する。"""

    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise DeploymentError("archive_checksum_required")
    stream.seek(0)
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    if digest.hexdigest() != checksum:
        raise DeploymentError("archive_checksum_mismatch")
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            manifest = json.loads(archive_member(archive, "manifest.json", 1024 * 1024))
            if not isinstance(manifest, list) or not manifest:
                raise DeploymentError("archive_manifest_invalid")
            for application, tag in APP_TAGS.items():
                entries = [
                    item
                    for item in manifest
                    if isinstance(item, dict)
                    and isinstance(item.get("RepoTags"), list)
                    and tag in item["RepoTags"]
                ]
                if len(entries) != 1 or not isinstance(entries[0].get("Config"), str):
                    raise DeploymentError("archive_application_missing_or_ambiguous")
                config = archive_member(archive, entries[0]["Config"], 4 * 1024 * 1024)
                if "sha256:" + hashlib.sha256(config).hexdigest() != images[application]:
                    raise DeploymentError("archive_application_identity_mismatch")
    except (tarfile.TarError, ValueError, OSError) as error:
        raise DeploymentError("archive_invalid") from error
    stream.seek(0)


class Deployment:
    """同一 daemon/project と immutable image を毎段階で照合する。"""

    def __init__(self, context: ComposeContext, daemon_id: str, images: Mapping[str, str]) -> None:
        """承認対象を固定し、前回の成功 marker は信用しない。"""

        self.context = context
        self.daemon_id = daemon_id
        self.images = dict(images)
        self.services: dict[str, object] = {}

    def command(
        self, arguments: Sequence[str], *, docker: bool = False, stdin: BinaryIO | None = None
    ) -> str:
        """失敗時の argv/stderr を表示せず、次段階へ進まない。"""

        try:
            if docker:
                result = self.context.docker_run(arguments, capture_output=True, stdin=stdin)
            else:
                result = self.context.run(arguments, capture_output=True)
        except (subprocess.SubprocessError, OSError) as error:
            raise DeploymentError("command_failed_result_may_be_unknown") from error
        return result.stdout

    def target(self) -> None:
        """同名 project が別 daemon に存在しても承認を流用しない。"""

        if (
            not self.daemon_id
            or self.command(["info", "--format", "{{.ID}}"], docker=True).strip() != self.daemon_id
        ):
            raise DeploymentError("docker_daemon_mismatch")
        config = json_object(self.command(["config", "--format", "json"]), "compose_config_invalid")
        if config.get("name") != self.context.project_name:
            raise DeploymentError("compose_project_mismatch")
        services = config.get("services")
        if not isinstance(services, dict):
            raise DeploymentError("compose_services_invalid")
        self.services = services
        for name, application in (
            ("api", "backend"),
            ("worker", "backend"),
            ("migrate", "backend"),
            ("web", "web"),
        ):
            service = services.get(name)
            if not isinstance(service, dict) or service.get("image") != self.images[application]:
                raise DeploymentError("compose_image_not_pinned")

    def installed_images(self) -> None:
        """archive 欠損時も既存の同名 tag を新 image と誤認しない。"""

        for expected in set(self.images.values()):
            actual = self.command(
                ["image", "inspect", "--format", "{{.Id}}", expected], docker=True
            )
            if actual.strip() != expected:
                raise DeploymentError("installed_image_mismatch")

    def container_configuration(self, row: dict[str, object]) -> None:
        """既存 container の古い接続先を、新しい設定の preflight と取り違えない。"""

        service = self.services.get(str(row.get("service")))
        if not isinstance(service, dict) or not isinstance(service.get("image"), str):
            raise DeploymentError("service_configuration_invalid")
        image = json_object(
            self.command(
                [
                    "image",
                    "inspect",
                    "--format",
                    '{"id":{{json .Id}},"environment":{{json .Config.Env}}}',
                    service["image"],
                ],
                docker=True,
            ),
            "image_configuration_invalid",
        )
        if image.get("id") != row.get("image"):
            raise DeploymentError("running_image_mismatch")
        expected = environment_values(image.get("environment"))
        configured = service.get("environment", {})
        if not isinstance(configured, dict):
            raise DeploymentError("service_environment_invalid")
        for key, value in configured.items():
            if not isinstance(key, str) or (value is not None and not isinstance(value, str)):
                raise DeploymentError("service_environment_invalid")
            if value is None:
                expected.pop(key, None)
            else:
                # Compose config は再読込用に全 $ を $$ として出力する。dotenv を再実装しない。
                expected[key] = value.replace("$$", "$")
        if expected != environment_values(row.get("environment")):
            raise DeploymentError("running_configuration_mismatch")

    def containers(self) -> list[dict[str, object]]:
        """Compose ps の service 列挙に依存せず、同 project の orphan/one-off も見る。"""

        ids = self.command(
            [
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={self.context.project_name}",
            ],
            docker=True,
        ).split()
        if len(ids) != len(set(ids)) or any(not CONTAINER_ID.fullmatch(item) for item in ids):
            raise DeploymentError("container_identity_invalid")
        result = []
        for identifier in ids:
            row = json_object(
                self.command(
                    [
                        "container",
                        "inspect",
                        "--format",
                        INSPECT_CONTAINER,
                        identifier,
                    ],
                    docker=True,
                ),
                "container_inspection_invalid",
            )
            if row.get("id") != identifier or row.get("project") != self.context.project_name:
                raise DeploymentError("container_target_mismatch")
            observed_image = row.get("image")
            if not isinstance(observed_image, str) or not IMAGE_ID.fullmatch(observed_image):
                raise DeploymentError("container_image_invalid")
            state = row.get("state")
            if not isinstance(state, str) or state not in {
                "created",
                "running",
                "paused",
                "restarting",
                "removing",
                "exited",
                "dead",
            }:
                raise DeploymentError("container_state_unknown")
            result.append(row)
        return result

    def gate(
        self, *, frontend: bool = False, infrastructure: bool = True, worker: bool = False
    ) -> None:
        """活動 writer の拒否と、必要な実 container の health/image を別々に検査する。"""

        rows = self.containers()
        active = [row for row in rows if row["state"] not in {"created", "exited", "dead"}]
        allowed = (
            INFRASTRUCTURE
            | ({"api", "web"} if frontend else set())
            | ({"worker"} if worker else set())
        )
        if any(row.get("service") not in allowed or row.get("oneoff") != "False" for row in active):
            raise DeploymentError("business_writers_not_stopped")
        required: dict[str, tuple[str | None, bool]] = {}
        if infrastructure:
            required.update(
                {"postgres": (None, True), "redis": (None, True), "object-storage": (None, False)}
            )
            initializers = [
                row
                for row in rows
                if row.get("service") == "object-storage-init" and row.get("oneoff") == "False"
            ]
            if (
                len(initializers) != 1
                or initializers[0]["state"] != "exited"
                or type(initializers[0].get("exit_code")) is not int
                or initializers[0].get("exit_code") != 0
            ):
                raise DeploymentError("storage_initialization_unconfirmed")
            self.container_configuration(initializers[0])
        if frontend:
            required.update(
                {"api": (self.images["backend"], True), "web": (self.images["web"], True)}
            )
        if worker:
            required["worker"] = (self.images["backend"], False)
        for service, (expected_image, healthy) in required.items():
            matches = [
                row
                for row in rows
                if row.get("service") == service and row.get("oneoff") == "False"
            ]
            if len(matches) != 1 or matches[0]["state"] != "running":
                raise DeploymentError("required_service_not_running")
            if healthy and matches[0].get("health") != "healthy":
                raise DeploymentError("required_service_not_healthy")
            if expected_image is not None and matches[0].get("image") != expected_image:
                raise DeploymentError("running_image_mismatch")
            self.container_configuration(matches[0])

    def preflight(self, *, running_api: bool = False, migration_plan: bool = False) -> None:
        """DB head と Redis の確認を実 image に実行させ、業務受入とは区別する。"""

        prefix = (
            ["exec", "-T", "api"]
            if running_api
            else [
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "--pull",
                "never",
                "migrate",
            ]
        )
        command = [*prefix, "python", "-m", "projectmind.ops.preflight"]
        if migration_plan:
            command.append("--migration-plan")
        report = json_object(self.command(command), "preflight_report_invalid")
        checks = report.get("checks")
        if report.get("status") != "ready" or not isinstance(checks, dict):
            raise DeploymentError("preflight_not_ready")
        for name in ("postgres",) if migration_plan else ("postgres", "redis"):
            if not isinstance(checks.get(name), dict) or checks[name].get("status") != "ok":
                raise DeploymentError("preflight_not_ready")
        postgres = checks["postgres"]
        head, current, pending = (
            postgres.get("migration_head"),
            postgres.get("current"),
            postgres.get("pending"),
        )
        if (
            not isinstance(head, str)
            or not head
            or not isinstance(current, list)
            or len(current) > 1
            or not isinstance(pending, list)
            or any(not isinstance(item, str) or not item for item in [*current, *pending])
            or (not migration_plan and (current != [head] or pending))
        ):
            raise DeploymentError("preflight_revision_report_invalid")

    def execute(
        self,
        phase: str,
        *,
        archive: Path | None = None,
        archive_sha256: str = "",
        background_approved: bool = False,
    ) -> None:
        """一段階だけ実行し、成功しても後続段階や一般入口を自動開放しない。"""

        if phase not in {"load", "migrate", "api", "worker"}:
            raise DeploymentError("unknown_phase")
        if phase == "worker" and not background_approved:
            raise DeploymentError("background_approval_required")
        if phase == "load" and (archive is None or not archive.is_file()):
            raise DeploymentError("archive_required")
        self.target()
        self.gate(frontend=phase == "worker", infrastructure=phase != "load")
        if phase == "load":
            assert archive is not None
            with archive.open("rb") as stream:
                verify_archive(stream, archive_sha256, self.images)
                # archive 検査中に別 writer が起動した場合も load 前に停止する。
                self.gate(infrastructure=False)
                self.command(["image", "load"], docker=True, stdin=stream)
            self.installed_images()
        else:
            self.installed_images()
            if phase == "migrate":
                self.preflight(migration_plan=True)
                self.gate()
                self.command(["run", "--rm", "-T", "--no-deps", "--pull", "never", "migrate"])
                self.preflight()
            elif phase == "api":
                self.preflight()
                self.gate()
                self.start(["api", "web"])
                self.gate(frontend=True)
                self.preflight(running_api=True)
            else:
                self.preflight(running_api=True)
                self.gate(frontend=True)
                self.start(["worker"])
                self.gate(frontend=True, worker=True)

    def start(self, services: Sequence[str]) -> None:
        """依存、build、pull を禁止し、期限内の起動状態を待つ。"""

        self.command(
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
                *services,
            ]
        )


def main(arguments: Sequence[str] | None = None) -> int:
    """明示された保守確認と対象 identity がなければ Docker 自体を呼ばない。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("load", "migrate", "api", "worker"))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--daemon-id", required=True)
    parser.add_argument("--backend-image-id", required=True, type=image_id)
    parser.add_argument("--web-image-id", required=True, type=image_id)
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help=(
            "confirm recovery point, all-instance stop/reconciliation, closed ingress "
            "and exclusive maintenance ownership"
        ),
    )
    parser.add_argument("--background-approved", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--archive-sha256", default="")
    options = parser.parse_args(arguments)
    try:
        if not options.maintenance_confirmed:
            raise DeploymentError("maintenance_confirmation_required")
        images = {"backend": options.backend_image_id, "web": options.web_image_id}
        context = ComposeContext.resolve(
            options.env_file, project_name=options.project_name, image_ids=images
        )
        Deployment(context, options.daemon_id, images).execute(
            options.phase,
            archive=options.archive,
            archive_sha256=options.archive_sha256,
            background_approved=options.background_approved,
        )
    except (DeploymentError, ComposeError, OSError) as error:
        reason = (
            str(error)
            if isinstance(error, DeploymentError)
            else "configuration_or_file_unavailable"
        )
        print(
            f"Deployment stopped: {reason}. Keep maintenance isolation; "
            "inspect original state before retry.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print(
            "Deployment interrupted; result may be unknown. Keep maintenance isolation.",
            file=sys.stderr,
        )
        return 130
    print(
        f"Deployment phase {options.phase} completed; "
        "later stages and ordinary ingress remain unapproved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
