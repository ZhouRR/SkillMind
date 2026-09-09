"""実 Docker を使わず、配備の対象照合と失敗後の非継続を検証する。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import BinaryIO, cast
from unittest.mock import patch

from scripts import deploy

CONFIGS = {name: json.dumps({"fixture": name}).encode() for name in ("backend", "web")}
IMAGES = {name: "sha256:" + hashlib.sha256(body).hexdigest() for name, body in CONFIGS.items()}
OLD_IMAGES = {"sha256:" + "a" * 64, "sha256:" + "b" * 64}
INFRA_IMAGES = {
    name: "sha256:" + hashlib.sha256(name.encode()).hexdigest()
    for name in ("postgres", "redis", "object-storage", "object-storage-init")
}
SERVICE_IMAGES = {
    **INFRA_IMAGES,
    "api": IMAGES["backend"],
    "worker": IMAGES["backend"],
    "migrate": IMAGES["backend"],
    "web": IMAGES["web"],
}
IMAGE_ENV = ["PATH=/fixture/bin", "FIXTURE_DEFAULT=image"]
INSPECT_IMAGE = '{"id":{{json .Id}},"environment":{{json .Config.Env}}}'
DAEMON = "fixture-daemon"
PROJECT = "fixture-project"
MIGRATE = ("run", "--rm", "-T", "--no-deps", "--pull", "never", "migrate")
PROBE = ("python", "-m", "projectmind.ops.preflight")
START = (
    "up",
    "-d",
    "--no-build",
    "--no-deps",
    "--pull",
    "never",
    "--wait",
    "--wait-timeout",
    "120",
)
READY = {
    "status": "ready",
    "checks": {
        "postgres": {
            "status": "ok",
            "migration_head": "fixture_head",
            "current": ["fixture_head"],
            "pending": [],
        },
        "redis": {"status": "ok"},
    },
}
Trace = tuple[str, tuple[str, ...]]


def container(service: str, number: int, **overrides: object) -> dict[str, object]:
    """外部 process と無関係な container の観測値を作る。"""

    row: dict[str, object] = {
        "id": f"{number:064x}",
        "project": PROJECT,
        "service": service,
        "image": SERVICE_IMAGES.get(service, IMAGES["backend"]),
        "environment": [
            "PATH=/fixture/bin",
            "FIXTURE_DEFAULT=configured",
            f"FIXTURE_SERVICE={service}",
        ],
        "oneoff": "False",
        "state": "running",
        "exit_code": 0,
        "health": "healthy",
    }
    row.update(overrides)
    return row


class FakeContext:
    """未知 command は拒否し、試験で許可した観測と副作用だけを記録する。"""

    def __init__(self, *, frontend: bool = False) -> None:
        """各段階を独立した実状態から始め、前段階 marker を模倣しない。"""

        self.project_name = PROJECT
        self.daemon = DAEMON
        self.config: dict[str, object] = {
            "name": PROJECT,
            "services": {
                name: {
                    "image": identifier,
                    "environment": {
                        "FIXTURE_SERVICE": name,
                        "FIXTURE_DEFAULT": "configured",
                    },
                }
                for name, identifier in SERVICE_IMAGES.items()
            },
        }
        self.rows = [
            container(name, number)
            for number, name in enumerate(("postgres", "redis", "object-storage"), start=1)
        ]
        self.rows.append(container("object-storage-init", 4, state="exited"))
        if frontend:
            self.rows.extend((container("api", 5), container("web", 6)))
        self.trace: list[Trace] = []
        self.installed = OLD_IMAGES | set(SERVICE_IMAGES.values())
        self.responses: dict[Trace, str] = {}
        self.before: Callable[[FakeContext, Trace], None] | None = None
        self.fail_at: int | None = None
        self.failure: BaseException = subprocess.CalledProcessError(
            17, ["fixture-command"], output="fixture-private-output", stderr="fixture-private-error"
        )
        self.loaded_bytes: bytes | None = None
        self.loaded_identity: tuple[int, int] | None = None

    def run(
        self, arguments: Sequence[str], *, capture_output: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Compose の実 subprocess へ到達しない入口を提供する。"""

        if not capture_output:
            raise AssertionError("Deployment must capture command output")
        return self._command("compose", arguments)

    def docker_run(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        stdin: BinaryIO | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Docker の副作用は記憶上だけで実行し、load の open file を検査する。"""

        if not capture_output:
            raise AssertionError("Deployment must capture command output")
        return self._command("docker", arguments, stdin=stdin)

    def _command(
        self, kind: str, arguments: Sequence[str], *, stdin: BinaryIO | None = None
    ) -> subprocess.CompletedProcess[str]:
        """失敗した command 自体までを trace に残し、それ以降は自動応答しない。"""

        args = tuple(arguments)
        entry = (kind, args)
        self.trace.append(entry)
        if self.before is not None:
            self.before(self, entry)
        if self.fail_at == len(self.trace):
            raise self.failure
        if entry in self.responses:
            output = self.responses[entry]
        elif entry == ("docker", ("info", "--format", "{{.ID}}")):
            output = self.daemon
        elif entry == ("compose", ("config", "--format", "json")):
            output = json.dumps(self.config)
        elif kind == "docker" and args[:1] == ("ps",):
            expected = (
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={PROJECT}",
            )
            if args != expected:
                raise AssertionError(args)
            output = "\n".join(str(row["id"]) for row in self.rows)
        elif kind == "docker" and args[:2] == ("container", "inspect"):
            if args[2:4] != ("--format", deploy.INSPECT_CONTAINER):
                raise AssertionError(args)
            output = json.dumps(next(row for row in self.rows if row["id"] == args[-1]))
        elif kind == "docker" and args[:2] == ("image", "inspect"):
            if args[-1] not in self.installed:
                raise subprocess.CalledProcessError(1, args)
            if args[2:4] == ("--format", "{{.Id}}"):
                output = args[-1]
            elif args[2:4] == ("--format", INSPECT_IMAGE):
                output = json.dumps({"id": args[-1], "environment": IMAGE_ENV})
            else:
                raise AssertionError(args)
        elif entry == ("docker", ("image", "load")):
            if stdin is None or stdin.tell() != 0:
                raise AssertionError("load must receive the verified, rewound file")
            identity = os.fstat(stdin.fileno())
            self.loaded_identity = (identity.st_dev, identity.st_ino)
            self.loaded_bytes = stdin.read()
            self.installed.update(IMAGES.values())
            output = "Loaded fixture images"
        elif kind == "compose" and args in (
            MIGRATE + PROBE,
            MIGRATE + PROBE + ("--migration-plan",),
            ("exec", "-T", "api", *PROBE),
        ):
            output = json.dumps(READY)
        elif entry == ("compose", MIGRATE):
            output = ""
        elif kind == "compose" and args[: len(START)] == START:
            if args[len(START) :] not in (("api", "web"), ("worker",)):
                raise AssertionError(args)
            for service in args[len(START) :]:
                self.rows.append(container(service, len(self.rows) + 1))
            output = ""
        else:
            raise AssertionError(f"Unmocked command: {entry!r}")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")


class DeploymentTestCase(unittest.TestCase):
    """生成する file を専用一時 directory に限定する共通 fixture。"""

    def setUp(self) -> None:
        """実環境の設定や archive を一切利用しない。"""

        directory = tempfile.TemporaryDirectory(prefix="projectmind-deploy-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.archive = self.root / "fixture images.tar"
        self.checksum = self.write_archive()

    def write_archive(
        self,
        *,
        manifest: object | None = None,
        extra: Sequence[tuple[str, bytes]] = (),
        configs: dict[str, bytes] | None = None,
    ) -> str:
        """可信 fixture 自作 tar の metadata を試験ごとに変更する。"""

        entries = (
            [
                {"Config": f"{name}.json", "RepoTags": [deploy.APP_TAGS[name]], "Layers": []}
                for name in CONFIGS
            ]
            if manifest is None
            else manifest
        )
        members = [("manifest.json", json.dumps(entries).encode())]
        members.extend((f"{name}.json", data) for name, data in (configs or CONFIGS).items())
        members.extend(extra)
        with tarfile.open(self.archive, "w") as archive:
            for name, body in members:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def execute(self, context: FakeContext, phase: str) -> None:
        """承認対象の ID と当該 file だけを実装へ渡す。"""

        deployment = deploy.Deployment(cast(deploy.ComposeContext, context), DAEMON, IMAGES)
        deployment.execute(
            phase,
            archive=self.archive,
            archive_sha256=self.checksum,
            background_approved=phase == "worker",
        )

    def writes(self, context: FakeContext) -> list[Trace]:
        """観測 command と潜在的な配備変更を分けて oracle にする。"""

        return [
            entry
            for entry in context.trace
            if entry == ("docker", ("image", "load"))
            or entry == ("compose", MIGRATE)
            or (entry[0] == "compose" and entry[1][:1] == ("up",))
        ]

    def rejects(self, context: FakeContext, reason: str, phase: str = "api") -> None:
        """拒否理由だけでなく、まだ副作用が無いことも確認する。"""

        with self.assertRaisesRegex(deploy.DeploymentError, reason):
            self.execute(context, phase)
        self.assertEqual(self.writes(context), [])


class ArchiveTests(DeploymentTestCase):
    """旧 tag が存在しても archive 自体の承認 identity を省略させない。"""

    def test_verified_archive_rewinds_the_same_stream(self) -> None:
        """検査成功後の stream をそのまま load へ渡せる。"""

        with self.archive.open("rb") as stream:
            deploy.verify_archive(stream, self.checksum, IMAGES)
            self.assertEqual(stream.tell(), 0)

    def test_checksum_is_required_and_exact(self) -> None:
        """省略、短縮、大文字、内容違いを拒否する。"""

        for checksum in ("", "a" * 63, "A" * 64, "0" * 64):
            with (
                self.subTest(checksum=checksum),
                self.archive.open("rb") as stream,
                self.assertRaisesRegex(deploy.DeploymentError, "archive_checksum"),
            ):
                deploy.verify_archive(stream, checksum, IMAGES)

    def test_non_archive_with_correct_checksum_is_rejected(self) -> None:
        """checksum 一致を file 形式の検証の代わりにしない。"""

        self.archive.write_bytes(b"fixture-not-a-tar")
        self.checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.rejects(FakeContext(), "archive_invalid", "load")

    def test_manifest_must_be_a_nonempty_list(self) -> None:
        """合法 JSON でも Docker save の一覧形式でなければ停止する。"""

        for manifest in ({}, [], "fixture", True):
            with self.subTest(manifest=manifest):
                self.checksum = self.write_archive(manifest=manifest)
                self.rejects(FakeContext(), "archive_manifest_invalid", "load")

    def test_missing_application_cannot_borrow_an_installed_old_tag(self) -> None:
        """両アプリの旧 image がある場合でも片方だけの archive を load しない。"""

        for missing in CONFIGS:
            with self.subTest(missing=missing):
                manifest = [
                    {"Config": f"{name}.json", "RepoTags": [deploy.APP_TAGS[name]]}
                    for name in CONFIGS
                    if name != missing
                ]
                self.checksum = self.write_archive(manifest=manifest)
                context = FakeContext()
                self.rejects(context, "archive_application_missing", "load")
                self.assertTrue(context.installed >= OLD_IMAGES)

    def test_ambiguous_application_tag_is_rejected(self) -> None:
        """同じ application tag を持つ複数 entry を推測で選択しない。"""

        manifest = [{"Config": "backend.json", "RepoTags": [deploy.APP_TAGS["backend"]]}] * 2
        self.checksum = self.write_archive(manifest=manifest)
        self.rejects(FakeContext(), "archive_application_missing_or_ambiguous", "load")

    def test_config_digest_must_equal_the_approved_image(self) -> None:
        """承認 ID と異なる config bytes は同じ tag でも拒否する。"""

        self.checksum = self.write_archive(configs={**CONFIGS, "backend": b"different-fixture"})
        self.rejects(FakeContext(), "archive_application_identity_mismatch", "load")

    def test_unsafe_config_paths_are_rejected_without_extraction(self) -> None:
        """metadata 名から作業 directory 外へ展開しない。"""

        for path in ("../backend.json", "/backend.json", "dir\\backend.json", ""):
            with self.subTest(path=path):
                manifest = [{"Config": path, "RepoTags": [deploy.APP_TAGS["backend"]]}]
                self.checksum = self.write_archive(manifest=manifest)
                self.rejects(FakeContext(), "archive_metadata_path_invalid", "load")

    def test_duplicate_missing_or_empty_metadata_is_rejected(self) -> None:
        """重複 member と空 config を先勝ち・後勝ちで解釈しない。"""

        for extra, configs in ((("manifest.json", b"[]"),), None), ((), {"backend": b""}):
            with self.subTest(extra=extra):
                self.checksum = self.write_archive(extra=extra, configs=configs)
                self.rejects(FakeContext(), "archive_metadata_invalid", "load")
        self.checksum = self.write_archive(configs={"backend": CONFIGS["backend"]})
        self.rejects(FakeContext(), "archive_metadata_invalid", "load")

    def test_metadata_symlink_is_not_followed(self) -> None:
        """正規 file 以外を config の代替にしない。"""

        with tarfile.open(self.archive, "w") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.type = tarfile.SYMTYPE
            info.linkname = "fixture-target"
            archive.addfile(info)
        self.checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.rejects(FakeContext(), "archive_metadata_invalid", "load")

    def test_replacing_archive_path_does_not_replace_verified_stdin(self) -> None:
        """検査後の path 差替えでは既に開いた file identity を変更できない。"""

        original = self.archive.read_bytes()
        status = self.archive.stat()
        context = FakeContext()
        observations = 0

        def replace_after_verification(fake: FakeContext, entry: Trace) -> None:
            """二度目の writer 検査時に path だけを別 file へ置き換える。"""

            nonlocal observations
            if entry[0] == "docker" and entry[1][:1] == ("ps",):
                observations += 1
                if observations == 2:
                    replacement = self.root / "replacement.tar"
                    replacement.write_bytes(b"unverified-fixture")
                    replacement.replace(self.archive)

        context.before = replace_after_verification
        self.execute(context, "load")
        self.assertEqual(context.loaded_bytes, original)
        self.assertEqual(context.loaded_identity, (status.st_dev, status.st_ino))
        self.assertNotEqual(self.archive.stat().st_ino, status.st_ino)


class PhaseTests(DeploymentTestCase):
    """各 phase の正確な command 境界と、全 command 位置の失敗停止を検証する。"""

    def test_load_only_loads_and_preserves_previous_images(self) -> None:
        """load 成功は migrate/start/旧 image 削除を意味しない。"""

        context = FakeContext()
        context.installed = OLD_IMAGES | set(INFRA_IMAGES.values())
        self.execute(context, "load")
        self.assertEqual(self.writes(context), [("docker", ("image", "load"))])
        self.assertTrue(context.installed >= OLD_IMAGES)
        self.assertFalse(any("rm" in args or "prune" in args for _, args in context.trace))

    def test_migrate_checks_plan_before_upgrade_and_readiness_after(self) -> None:
        """migration は単独 run だけで、成功後も API/Worker を起動しない。"""

        context = FakeContext()
        self.execute(context, "migrate")
        commands = [
            args
            for kind, args in context.trace
            if kind == "compose" and args != ("config", "--format", "json")
        ]
        self.assertEqual(
            commands, [MIGRATE + PROBE + ("--migration-plan",), MIGRATE, MIGRATE + PROBE]
        )
        self.assertEqual(self.writes(context), [("compose", MIGRATE)])

    def test_api_starts_only_frontend_with_both_preflights(self) -> None:
        """API/Web の前後に確認し、Worker はこの phase に含まれない。"""

        context = FakeContext()
        self.execute(context, "api")
        self.assertEqual(self.writes(context), [("compose", (*START, "api", "web"))])
        self.assertLess(
            context.trace.index(("compose", MIGRATE + PROBE)),
            context.trace.index(("compose", (*START, "api", "web"))),
        )
        self.assertEqual(context.trace[-1], ("compose", ("exec", "-T", "api", *PROBE)))

    def test_worker_requires_frontend_and_only_starts_worker(self) -> None:
        """背景許可がある場合のみ既存 API の確認後に Worker を起動する。"""

        context = FakeContext(frontend=True)
        self.execute(context, "worker")
        self.assertEqual(self.writes(context), [("compose", (*START, "worker"))])
        self.assertLess(
            context.trace.index(("compose", ("exec", "-T", "api", *PROBE))),
            context.trace.index(("compose", (*START, "worker"))),
        )

    def test_every_command_failure_stops_at_its_exact_prefix(self) -> None:
        """観測と副作用のどの command が失敗しても、後続 command を一つも呼ばない。"""

        for phase in ("load", "migrate", "api", "worker"):
            baseline = FakeContext(frontend=phase == "worker")
            self.execute(baseline, phase)
            for position in range(1, len(baseline.trace) + 1):
                with self.subTest(phase=phase, position=position):
                    context = FakeContext(frontend=phase == "worker")
                    context.fail_at = position
                    with self.assertRaisesRegex(deploy.DeploymentError, "command_failed"):
                        self.execute(context, phase)
                    self.assertEqual(context.trace, baseline.trace[:position])

    def test_timeout_and_disconnection_are_unknown_without_replay(self) -> None:
        """副作用直後の応答不明を retry や自動 rollback へ変換しない。"""

        for phase in ("load", "migrate", "api", "worker"):
            baseline = FakeContext(frontend=phase == "worker")
            self.execute(baseline, phase)
            position = baseline.trace.index(self.writes(baseline)[0]) + 1
            for error in (
                OSError("fixture-private-error"),
                subprocess.TimeoutExpired("fixture-private-command", 1),
            ):
                with self.subTest(phase=phase, error=type(error).__name__):
                    context = FakeContext(frontend=phase == "worker")
                    context.fail_at, context.failure = position, error
                    with self.assertRaisesRegex(deploy.DeploymentError, "result_may_be_unknown"):
                        self.execute(context, phase)
                    self.assertEqual(context.trace, baseline.trace[:position])

    def test_missing_background_approval_makes_no_command(self) -> None:
        """保守確認や image 所在だけを背景実行の許可にしない。"""

        context = FakeContext(frontend=True)
        instance = deploy.Deployment(cast(deploy.ComposeContext, context), DAEMON, IMAGES)
        with self.assertRaisesRegex(deploy.DeploymentError, "background_approval_required"):
            instance.execute("worker")
        self.assertEqual(context.trace, [])

    def test_unknown_phase_or_missing_archive_makes_no_command(self) -> None:
        """実行不能な要求は対象への問い合わせ前に拒否する。"""

        for phase in ("all", "load"):
            with self.subTest(phase=phase):
                context = FakeContext()
                instance = deploy.Deployment(cast(deploy.ComposeContext, context), DAEMON, IMAGES)
                with self.assertRaises(deploy.DeploymentError):
                    instance.execute(phase)
                self.assertEqual(context.trace, [])

    def test_new_writer_between_archive_check_and_load_blocks_load(self) -> None:
        """初回停止観測を後続の writer 不在証明として再利用しない。"""

        context = FakeContext()
        count = 0

        def start_writer(fake: FakeContext, entry: Trace) -> None:
            """二度目の container 一覧で新 writer を観測する。"""

            nonlocal count
            if entry[0] == "docker" and entry[1][:1] == ("ps",):
                count += 1
                if count == 2:
                    fake.rows.append(container("worker", 9))

        context.before = start_writer
        self.rejects(context, "business_writers_not_stopped", "load")

    def test_new_writer_after_preflight_prevents_migration_or_start(self) -> None:
        """read-only 確認中に復活した writer を次の実操作前に再検出する。"""

        for phase in ("migrate", "api", "worker"):
            with self.subTest(phase=phase):
                context = FakeContext(frontend=phase == "worker")

                def start_writer(fake: FakeContext, entry: Trace) -> None:
                    """preflight 応答が返る時点で別 Worker が既に動いている。"""

                    if entry[0] == "compose" and "projectmind.ops.preflight" in entry[1]:
                        fake.rows.append(container("worker", 9))

                context.before = start_writer
                self.rejects(context, "business_writers_not_stopped", phase)

    def test_partial_start_failure_preserves_observed_state_without_automatic_rollback(
        self,
    ) -> None:
        """up 失敗を全 container の未起動とは扱わず、その後の操作を禁止する。"""

        baseline = FakeContext()
        self.execute(baseline, "api")
        context = FakeContext()
        context.fail_at = baseline.trace.index(("compose", (*START, "api", "web"))) + 1

        def partially_start(fake: FakeContext, entry: Trace) -> None:
            """応答失敗の直前に一つだけ実起動した状況を模倣する。"""

            if entry == ("compose", (*START, "api", "web")):
                fake.rows.append(container("api", 5))

        context.before = partially_start
        with self.assertRaisesRegex(deploy.DeploymentError, "result_may_be_unknown"):
            self.execute(context, "api")
        self.assertEqual(context.trace, baseline.trace[: context.fail_at])
        self.assertEqual(
            [row["service"] for row in context.rows if row["service"] == "api"], ["api"]
        )
        self.assertEqual(self.writes(context), [("compose", (*START, "api", "web"))])


class TargetAndGateTests(DeploymentTestCase):
    """daemon/project/container/image の各観測境界を独立して崩す。"""

    def test_daemon_mismatch_stops_before_compose(self) -> None:
        """同名 Project の別 daemon に承認を流用しない。"""

        context = FakeContext()
        context.daemon = "different-fixture-daemon"
        self.rejects(context, "docker_daemon_mismatch")
        self.assertEqual(len(context.trace), 1)

    def test_compose_project_and_all_application_pins_are_checked(self) -> None:
        """api だけでなく worker/migrate/web も承認 digest に固定する。"""

        context = FakeContext()
        context.config["name"] = "other-project"
        self.rejects(context, "compose_project_mismatch")
        for service in ("api", "worker", "migrate", "web"):
            with self.subTest(service=service):
                context = FakeContext()
                services = cast(dict[str, dict[str, str]], context.config["services"])
                services[service]["image"] = "fixture/mutable:old"
                self.rejects(context, "compose_image_not_pinned")

    def test_invalid_compose_json_and_services_are_rejected(self) -> None:
        """JSON parse 成功だけを Compose 構成の受理条件にしない。"""

        for output in ("invalid", "[]", json.dumps({"name": PROJECT, "services": []})):
            with self.subTest(output=output):
                context = FakeContext()
                context.responses[("compose", ("config", "--format", "json"))] = output
                self.rejects(context, "compose_(config|services)_invalid")

    def test_missing_installed_id_and_wrong_inspection_block_start(self) -> None:
        """既存 tag の所在を immutable image の所在と取り違えない。"""

        context = FakeContext()
        context.installed = OLD_IMAGES | set(INFRA_IMAGES.values())
        self.rejects(context, "command_failed")
        context = FakeContext()
        for expected in IMAGES.values():
            context.responses[("docker", ("image", "inspect", "--format", "{{.Id}}", expected))] = (
                next(iter(OLD_IMAGES))
            )
        self.rejects(context, "installed_image_mismatch")

    def test_post_load_image_mismatch_never_continues_to_another_phase(self) -> None:
        """load 成功後の identity 不一致を成功扱いしない。"""

        context = FakeContext()
        for expected in IMAGES.values():
            context.responses[("docker", ("image", "inspect", "--format", "{{.Id}}", expected))] = (
                "sha256:" + "c" * 64
            )
        with self.assertRaisesRegex(deploy.DeploymentError, "installed_image_mismatch"):
            self.execute(context, "load")
        self.assertEqual(self.writes(context), [("docker", ("image", "load"))])
        self.assertTrue(context.installed >= OLD_IMAGES)

    def test_duplicate_or_short_container_ids_are_rejected(self) -> None:
        """一覧の不正 identity を無視して残りの container だけで判断しない。"""

        for output in ("short", f"{'1' * 64}\n{'1' * 64}"):
            with self.subTest(output=output):
                context = FakeContext()
                context.responses[
                    (
                        "docker",
                        (
                            "ps",
                            "--all",
                            "--quiet",
                            "--no-trunc",
                            "--filter",
                            f"label=com.docker.compose.project={PROJECT}",
                        ),
                    )
                ] = output
                self.rejects(context, "container_identity_invalid")

    def test_container_inspection_must_match_requested_id_and_project(self) -> None:
        """tag や一覧 label だけでなく実 inspect の対象も照合する。"""

        for field, value in (("id", "f" * 64), ("project", "other-project")):
            with self.subTest(field=field):
                context = FakeContext()
                row = {**context.rows[0], field: value}
                context.responses[
                    (
                        "docker",
                        (
                            "container",
                            "inspect",
                            "--format",
                            deploy.INSPECT_CONTAINER,
                            str(context.rows[0]["id"]),
                        ),
                    )
                ] = json.dumps(row)
                self.rejects(context, "container_target_mismatch")

    def test_unknown_or_malformed_container_state_is_rejected(self) -> None:
        """daemon の未知 state を停止済みと推測しない。"""

        for state in ("fixture-unknown", None, False, [], {}):
            with self.subTest(state=state):
                context = FakeContext()
                context.rows[0]["state"] = state
                self.rejects(context, "container_state_unknown")
        context = FakeContext()
        context.responses[
            (
                "docker",
                (
                    "container",
                    "inspect",
                    "--format",
                    deploy.INSPECT_CONTAINER,
                    str(context.rows[0]["id"]),
                ),
            )
        ] = "[]"
        self.rejects(context, "container_inspection_invalid")

    def test_container_image_identity_cannot_be_omitted_or_shortened(self) -> None:
        """基盤側でも未知の image を同じ設定の既存 container と認定しない。"""

        for image in (None, False, {}, [], "", "short", "sha256:" + "A" * 64):
            with self.subTest(image=image):
                context = FakeContext()
                context.rows[0]["image"] = image
                self.rejects(context, "container_image_invalid")

    def test_running_or_paused_writers_or_orphans_block_load(self) -> None:
        """dispatch flag を見ず、実 writer/orphan/one-off の活動を拒否する。"""

        for service in ("api", "worker", "migrate", "old-service"):
            for state in ("running", "paused", "restarting", "removing"):
                with self.subTest(service=service, state=state):
                    context = FakeContext()
                    context.rows.append(container(service, 9, state=state))
                    self.rejects(context, "business_writers_not_stopped", "load")
        context = FakeContext()
        context.rows.append(container("postgres", 9, oneoff="True"))
        self.rejects(context, "business_writers_not_stopped", "load")

    def test_stopped_old_writers_do_not_require_deleting_containers(self) -> None:
        """旧 image/container は停止したまま保全できる。"""

        context = FakeContext()
        context.rows.append(container("worker", 9, state="exited", image=next(iter(OLD_IMAGES))))
        self.execute(context, "load")
        self.assertEqual(len(context.rows), 5)

    def test_infrastructure_must_be_unique_running_and_healthy(self) -> None:
        """依存の暗黙起動や不健全な DB/Redis による継続を禁止する。"""

        for service in ("postgres", "redis", "object-storage"):
            for change in ("missing", "duplicate", "exited"):
                with self.subTest(service=service, change=change):
                    context = FakeContext()
                    row = next(row for row in context.rows if row["service"] == service)
                    if change == "missing":
                        context.rows.remove(row)
                    elif change == "duplicate":
                        context.rows.append(container(service, 9))
                    else:
                        row["state"] = change
                    self.rejects(context, "required_service_not_running")
        for service in ("postgres", "redis"):
            with self.subTest(service=service):
                context = FakeContext()
                next(row for row in context.rows if row["service"] == service)["health"] = (
                    "starting"
                )
                self.rejects(context, "required_service_not_healthy")

    def test_bucket_initializer_requires_one_successfully_exited_container(self) -> None:
        """bucket 準備失敗・実行中・重複・one-off を正常完了とみなさない。"""

        for values in (
            {"exit_code": 1},
            {"exit_code": False},
            {"exit_code": "0"},
            {"state": "running"},
            {"oneoff": "True"},
        ):
            with self.subTest(values=values):
                context = FakeContext()
                context.rows[3].update(values)
                self.rejects(context, "storage_initialization_unconfirmed")
        context = FakeContext()
        context.rows.append(container("object-storage-init", 9, state="exited"))
        self.rejects(context, "storage_initialization_unconfirmed")

    def test_worker_gate_checks_actual_frontend_image_and_health(self) -> None:
        """起動予定の設定ではなく稼働 container の .Image を照合する。"""

        for service in ("api", "web"):
            for field, value, reason in (
                ("image", next(iter(OLD_IMAGES)), "running_image_mismatch"),
                ("health", "unhealthy", "required_service_not_healthy"),
            ):
                with self.subTest(service=service, field=field):
                    context = FakeContext(frontend=True)
                    next(row for row in context.rows if row["service"] == service)[field] = value
                    self.rejects(context, reason, "worker")

    def test_migration_plan_unknown_or_multiple_heads_prevents_upgrade(self) -> None:
        """read-only migration plan の拒否後は upgrade を呼び出さない。"""

        for reason in (
            "unknown_database_revision",
            "multiple_database_revisions",
            "migration_head_missing",
            "MultipleHeads",
        ):
            with self.subTest(reason=reason):
                context = FakeContext()
                context.responses[("compose", MIGRATE + PROBE + ("--migration-plan",))] = (
                    json.dumps(
                        {
                            "status": "not_ready",
                            "checks": {"postgres": {"status": "error", "reason": reason}},
                        }
                    )
                )
                self.rejects(context, "preflight_not_ready", "migrate")

    def test_migration_plan_does_not_require_redis_report(self) -> None:
        """migration 前の DB graph 検証は通常の全基盤 readiness と区別する。"""

        context = FakeContext()
        context.responses[("compose", MIGRATE + PROBE + ("--migration-plan",))] = json.dumps(
            {
                "status": "ready",
                "checks": {
                    "postgres": {
                        "status": "ok",
                        "migration_head": "fixture_head",
                        "current": [],
                        "pending": ["fixture_head"],
                    }
                },
            }
        )
        self.execute(context, "migrate")

    def test_preflight_requires_complete_revision_facts(self) -> None:
        """古い省略 report や矛盾した ready で次段階へ進まない。"""

        for invalid in (
            {},
            {"migration_head": None},
            {"current": None},
            {"pending": None},
            {"current": ["a", "b"]},
            {"pending": [True]},
            {"current": []},
            {"pending": ["fixture_head"]},
        ):
            with self.subTest(invalid=invalid):
                context = FakeContext()
                postgres = (
                    {"status": "ok"}
                    if not invalid
                    else {
                        **READY["checks"]["postgres"],
                        **invalid,
                    }
                )
                context.responses[("compose", MIGRATE + PROBE)] = json.dumps(
                    {
                        "status": "ready",
                        "checks": {"postgres": postgres, "redis": {"status": "ok"}},
                    }
                )
                with self.assertRaisesRegex(deploy.DeploymentError, "revision_report_invalid"):
                    self.execute(context, "api")
                self.assertEqual(self.writes(context), [])

    def test_preflight_requires_valid_report_and_both_checks(self) -> None:
        """exit 0 や一つの healthy だけで API を起動しない。"""

        for report in (
            "invalid",
            "[]",
            "{}",
            json.dumps({"status": "ready", "checks": {}}),
            json.dumps({"status": "ready", "checks": {"postgres": {"status": "ok"}}}),
            json.dumps(
                {
                    "status": "ready",
                    "checks": {"postgres": {"status": "ok"}, "redis": {"status": "error"}},
                }
            ),
        ):
            with self.subTest(report=report):
                context = FakeContext()
                context.responses[("compose", MIGRATE + PROBE)] = report
                self.rejects(context, "preflight_(report_invalid|not_ready)")


class ConfigurationGateTests(DeploymentTestCase):
    """古い設定で healthy な container と現在承認する設定を混同させない。"""

    def environment(self, context: FakeContext, service: str) -> dict[str, object]:
        """Compose が描画した当該 service の環境 mapping を取り出す。"""

        services = cast(dict[str, dict[str, object]], context.config["services"])
        return cast(dict[str, object], services[service]["environment"])

    def test_changed_api_or_infrastructure_configuration_prevents_worker_start(self) -> None:
        """旧 API の preflight が成功しても、新設定との差分で背景実行を停止する。"""

        for service in ("api", "web", *INFRA_IMAGES):
            with self.subTest(service=service):
                context = FakeContext(frontend=True)
                self.environment(context, service)["FIXTURE_CONNECTION"] = "new-fixture-target"
                self.rejects(context, "running_configuration_mismatch", "worker")
                self.assertNotIn(("compose", ("exec", "-T", "api", *PROBE)), context.trace)

    def test_container_extra_or_missing_environment_is_not_ignored(self) -> None:
        """承認外接続先の追加と image default の欠落を双方検出する。"""

        for value in (["PATH=/fixture/bin"], [*IMAGE_ENV, "FIXTURE_EXTRA=unexpected"], []):
            with self.subTest(value=value):
                context = FakeContext(frontend=True)
                next(row for row in context.rows if row["service"] == "api")["environment"] = value
                self.rejects(context, "running_configuration_mismatch", "worker")

    def test_compose_null_removes_image_default_instead_of_stringifying_it(self) -> None:
        """未設定指定は文字列 None でなく default の除去として比較する。"""

        context = FakeContext(frontend=True)
        self.environment(context, "api")["FIXTURE_DEFAULT"] = None
        next(row for row in context.rows if row["service"] == "api")["environment"] = [
            "PATH=/fixture/bin",
            "FIXTURE_SERVICE=api",
        ]
        self.execute(context, "worker")

    def test_compose_dollar_escaping_is_decoded_exactly_once(self) -> None:
        """Compose の再読込用 escape と実 container の値を同じ表現で照合する。"""

        context = FakeContext(frontend=True)
        self.environment(context, "api")["FIXTURE_LITERAL"] = "$$one $$$$two $${three}"
        row = next(row for row in context.rows if row["service"] == "api")
        cast(list[str], row["environment"]).append("FIXTURE_LITERAL=$one $$two ${three}")
        self.execute(context, "worker")
        context = FakeContext(frontend=True)
        self.environment(context, "api")["FIXTURE_LITERAL"] = "$$one $$$$two $${three}"
        row = next(row for row in context.rows if row["service"] == "api")
        cast(list[str], row["environment"]).append("FIXTURE_LITERAL=$one $two ${three}")
        self.rejects(context, "running_configuration_mismatch", "worker")

    def test_environment_values_reject_duplicate_bare_and_nontext_entries(self) -> None:
        """環境 list の破損を勝手な上書きや空値への変換で隠さない。"""

        for value in ({}, ["A=one", "A=two"], ["A"], ["=value"], [1]):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(deploy.DeploymentError, "container_environment_invalid"),
            ):
                deploy.environment_values(value)
        self.assertEqual(deploy.environment_values(["A=a=b", "EMPTY="]), {"A": "a=b", "EMPTY": ""})

    def test_malformed_image_environment_prevents_start(self) -> None:
        """image inspect 成功でも metadata list が壊れていたら開始しない。"""

        context = FakeContext()
        context.responses[
            (
                "docker",
                (
                    "image",
                    "inspect",
                    "--format",
                    INSPECT_IMAGE,
                    INFRA_IMAGES["object-storage-init"],
                ),
            )
        ] = json.dumps(
            {
                "id": INFRA_IMAGES["object-storage-init"],
                "environment": {"A": "value"},
            }
        )
        self.rejects(context, "container_environment_invalid")

    def test_nontext_compose_environment_does_not_get_coerced(self) -> None:
        """JSON の数値や bool を安全な文字列とみなさない。"""

        for value in (1, False, [], {}):
            with self.subTest(value=value):
                context = FakeContext()
                self.environment(context, "object-storage-init")["FIXTURE_VALUE"] = value
                self.rejects(context, "service_environment_invalid")

    def test_configuration_mismatch_diagnostic_does_not_disclose_values(self) -> None:
        """差分値やその digest を CLI に表示せず停止理由だけを返す。"""

        context = FakeContext(frontend=True)
        self.environment(context, "api")["FIXTURE_CONNECTION"] = "fixture-private-config"
        error = io.StringIO()
        arguments = [
            "worker",
            "--project-name",
            PROJECT,
            "--daemon-id",
            DAEMON,
            "--backend-image-id",
            IMAGES["backend"],
            "--web-image-id",
            IMAGES["web"],
            "--maintenance-confirmed",
            "--background-approved",
        ]
        with (
            patch.object(deploy.ComposeContext, "resolve", return_value=context),
            redirect_stderr(error),
        ):
            self.assertEqual(deploy.main(arguments), 1)
        self.assertIn("running_configuration_mismatch", error.getvalue())
        self.assertNotIn("fixture-private-config", error.getvalue())
        self.assertNotIn(hashlib.sha256(b"fixture-private-config").hexdigest(), error.getvalue())
        self.assertEqual(self.writes(context), [])


class MainTests(DeploymentTestCase):
    """CLI の確認・機密非表示・中断結果を fake context だけで検証する。"""

    def arguments(self, phase: str = "api") -> list[str]:
        """実 deployment に使用できない fixture identity を明示する。"""

        return [
            phase,
            "--env-file",
            str(self.root / "fixture config.txt"),
            "--project-name",
            PROJECT,
            "--daemon-id",
            DAEMON,
            "--backend-image-id",
            IMAGES["backend"],
            "--web-image-id",
            IMAGES["web"],
        ]

    def test_maintenance_confirmation_is_required_before_resolving_environment(self) -> None:
        """未確認の実行では設定読取や Docker 問い合わせへ到達しない。"""

        with (
            patch.object(deploy.ComposeContext, "resolve") as resolve,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(deploy.main(self.arguments()), 1)
            resolve.assert_not_called()

    def test_mutable_or_short_image_ids_are_rejected_before_environment(self) -> None:
        """通常 tag や省略 digest を CLI 承認 identity に使わせない。"""

        for value in ("fixture/backend:old", "sha256:abc", "sha256:" + "A" * 64):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                deploy.image_id(value)
        arguments = [*self.arguments(), "--maintenance-confirmed"]
        arguments[arguments.index("--backend-image-id") + 1] = "fixture/backend:old"
        with (
            patch.object(deploy.ComposeContext, "resolve") as resolve,
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as raised:
                deploy.main(arguments)
            self.assertEqual(raised.exception.code, 2)
            resolve.assert_not_called()

    def test_success_keeps_later_stages_and_ingress_unapproved(self) -> None:
        """単独段階の成功を一般公開済みと報告しない。"""

        context = FakeContext()
        output = io.StringIO()
        with patch.object(deploy.ComposeContext, "resolve", return_value=context) as resolve:
            with redirect_stdout(output):
                self.assertEqual(deploy.main([*self.arguments(), "--maintenance-confirmed"]), 0)
            resolve.assert_called_once_with(
                str(self.root / "fixture config.txt"), project_name=PROJECT, image_ids=IMAGES
            )
        self.assertIn("later stages and ordinary ingress remain unapproved", output.getvalue())
        self.assertEqual(self.writes(context), [("compose", (*START, "api", "web"))])

    def test_command_failure_does_not_print_captured_private_output(self) -> None:
        """診断用 stdout/stderr/argv を共有 report に流さない。"""

        context = FakeContext()
        context.fail_at = 1
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(deploy.ComposeContext, "resolve", return_value=context),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            self.assertEqual(deploy.main([*self.arguments(), "--maintenance-confirmed"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("result_may_be_unknown", errors.getvalue())
        self.assertNotIn("fixture-private", errors.getvalue())
        self.assertNotIn("fixture-command", errors.getvalue())

    def test_configuration_and_file_failures_are_redacted(self) -> None:
        """設定や file 例外に含まれる私有情報を表示しない。"""

        for error in (
            deploy.ComposeError("fixture-private-source"),
            OSError("fixture-private-file"),
        ):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with (
                    patch.object(deploy.ComposeContext, "resolve", side_effect=error),
                    redirect_stderr(output),
                ):
                    self.assertEqual(deploy.main([*self.arguments(), "--maintenance-confirmed"]), 1)
                self.assertIn("configuration_or_file_unavailable", output.getvalue())
                self.assertNotIn("fixture-private", output.getvalue())

    def test_keyboard_interrupt_reports_unknown_without_continuing(self) -> None:
        """中断を成功または安全な未実行と断言しない。"""

        context = FakeContext()
        context.fail_at, context.failure = 1, KeyboardInterrupt()
        output = io.StringIO()
        with (
            patch.object(deploy.ComposeContext, "resolve", return_value=context),
            redirect_stderr(output),
        ):
            self.assertEqual(deploy.main([*self.arguments(), "--maintenance-confirmed"]), 130)
        self.assertIn("result may be unknown", output.getvalue())
        self.assertEqual(len(context.trace), 1)


if __name__ == "__main__":
    unittest.main()
