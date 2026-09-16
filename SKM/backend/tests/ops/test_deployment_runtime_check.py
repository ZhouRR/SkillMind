"""実 Makefile を隔離 Docker fake で実行し、同期更新と非破壊の診断を検証する。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
_FAKE_DOCKER = r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["SKM_TEST_LOG"]).open("a") as out:
    out.write(json.dumps(args) + "\n")
scenario = os.environ.get("SKM_TEST_SCENARIO", "ok")
if args[:2] == ["image", "inspect"]:
    print("linux/amd64" if "{{.Os}}/{{.Architecture}}" in args else "sha256:loaded-backend")
elif args[:1] == ["info"]:
    print("linux/amd64")
elif args[:1] == ["inspect"]:
    print("sha256:stale" if scenario == "image" and args[1] == "worker-1" else "sha256:loaded-backend")
elif args[:1] == ["exec"]:
    if scenario == "exec":
        sys.exit(2)
    if scenario == "invalid":
        print("sha256:invalid")
    else:
        changed = (scenario == "identity" and args[1] == "worker-1") or (scenario == "replica" and args[1] == "worker-2")
        print("sha256:" + ("b" if changed else "a") * 64)
        if scenario == "unavailable":
            print("Interpreter is unavailable; no model was started.", file=sys.stderr)
elif args[:1] == ["compose"]:
    if "--images" in args:
        print("skillmind/backend:0.1.0\nskillmind/web:0.1.0")
    elif "ps" in args and "-q" in args:
        service = args[-1]
        if not (scenario == "missing" and service == "worker"):
            print(service + "-1")
            if scenario == "replica" and service == "worker":
                print("worker-2")
'''


@pytest.fixture
def deployment(tmp_path: Path):
    """本番 Docker や配備 directory を触らず、所有する一時 fixture だけを起動する。"""

    if shutil.which("make") is None:
        pytest.skip("GNU make is required for the isolated deployment test")
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copyfile(ROOT / "compose.yml", tmp_path / "compose.yml")
    (tmp_path / ".env").write_text("FIXTURE_ONLY=1\n")
    (tmp_path / "images.tar").write_bytes(b"not-a-real-archive")
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(f"#!{sys.executable}\n" + _FAKE_DOCKER)
    docker.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
        "SKM_TEST_LOG": str(tmp_path / "calls.jsonl"),
        "ENV_FILE": ".env",
    }
    # 外側の make jobserver や developer の override を一時配備に持ち込まない。
    for key in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
        environment.pop(key, None)

    def run(target: str, scenario: str = "ok"):
        """一回の同期手順と Docker 引数を、有限 timeout のもとで取得する。"""

        result = subprocess.run(
            ["make", "--no-print-directory", target], cwd=tmp_path,
            env={**environment, "SKM_TEST_SCENARIO": scenario},
            text=True, capture_output=True, timeout=20, check=False,
        )
        calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        return result, calls

    return run


def test_runtime_check_only_reads_images_and_configuration(deployment) -> None:
    """全 Backend を検査するが、model job・migration・restart を診断から呼ばない。"""

    result, calls = deployment("runtime-check")
    assert result.returncode == 0, result.stderr
    assert {args[1] for args in calls if args[0] == "exec"} == {
        "api-1", "worker-1", "maintenance-1",
    }
    assert all(args[0] in {"image", "inspect", "compose", "exec"} for args in calls)
    assert all("ps" in args for args in calls if args[0] == "compose")
    assert all("skillmind.ops.runtime_identity" in args for args in calls if args[0] == "exec")


@pytest.mark.parametrize("scenario", ["image", "identity", "missing", "invalid", "exec", "replica"])
def test_mismatched_or_unreadable_backend_does_not_claim_synchronized(deployment, scenario) -> None:
    """一台目だけでなく全 replica を検査し、不一致を修復や再実行で隠さない。"""

    result, calls = deployment("runtime-check", scenario)
    assert result.returncode != 0
    assert not any("restart" in args or "run" in args or "stop" in args for args in calls)


def test_disabled_interpreter_does_not_turn_diagnostic_into_import_gate(deployment) -> None:
    """両端が同じ未設定状態なら、設定一致と機能の可用性を混同しない。"""

    result, _ = deployment("runtime-check", "unavailable")
    assert result.returncode == 0
    assert "unavailable" in result.stderr


def test_deploy_recreates_all_backends_before_opening_web(deployment) -> None:
    """元の migration 順を保ち、同期再作成・比較後に Web を戻す。"""

    result, calls = deployment("deploy")
    assert result.returncode == 0, result.stderr
    backend = next(i for i, args in enumerate(calls) if "up" in args and args[-3:] == ["api", "worker", "maintenance"])
    assert "--force-recreate" in calls[backend] and "--wait" in calls[backend]
    probes = [i for i, args in enumerate(calls) if args[0] == "exec"]
    web = next(i for i, args in enumerate(calls) if "up" in args and args[-1] == "web")
    assert backend < min(probes) <= max(probes) < web
    assert any("skillmind.ops.preflight" in args for args in calls[:backend])
    assert not any("--volumes" in args or "--force" in args or "prune" in args for args in calls)


def test_compose_shares_backend_image_env_and_checks_worker_heartbeat() -> None:
    """実行 Worker の running を、起動完了の証明として扱わない。"""

    services = yaml.safe_load((ROOT / "compose.yml").read_text())["services"]
    api = services["api"]
    for role in ("worker", "maintenance"):
        assert services[role]["image"] == api["image"]
        assert services[role]["env_file"] == api["env_file"]
        assert services[role]["environment"] == api["environment"]
        assert "--check" in services[role]["healthcheck"]["test"]
    assert "codex-data:/var/lib/skillmind/codex" in services["worker"]["volumes"]
    assert "volumes" not in api
