"""実 Makefile の image 清理順と container 参照保護を隔離 Docker で検証する。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = "skillmind/backend:0.1.0"
WEB = "skillmind/web:0.1.0"
OLD_BACKEND = "sha256:" + "a" * 64
OLD_WEB = "sha256:" + "b" * 64
NEW_BACKEND = "sha256:" + "c" * 64
NEW_WEB = "sha256:" + "d" * 64
HELD = "sha256:" + "e" * 64
OTHER = "sha256:" + "f" * 64
BACKEND_UNUSED = "sha256:" + "1" * 64
WEB_UNUSED = "sha256:" + "2" * 64

FAKE_DOCKER = r'''
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["SKM_FAKE_ROOT"])
state_path = root / "state.json"
state = json.loads(state_path.read_text())
args = sys.argv[1:]
BACKEND = "skillmind/backend:0.1.0"
WEB = "skillmind/web:0.1.0"
with (root / "trace.jsonl").open("a") as out:
    out.write(json.dumps(args) + "\n")

def finish(code=0):
    state_path.write_text(json.dumps(state))
    raise SystemExit(code)

def image_id(reference):
    if reference in state["tags"]:
        return state["tags"][reference]
    return reference if reference in state["images"] else None

if args[:2] == ["image", "ls"]:
    fmt = args[args.index("--format") + 1]
    repository = args[-1]
    for reference, identifier in state["tags"].items():
        if reference.startswith(repository + ":"):
            print(identifier if fmt == "{{.ID}}" else reference)
    finish()
if args[:2] == ["image", "inspect"]:
    fmt = args[args.index("--format") + 1]
    for reference in args[2:args.index("--format")]:
        identifier = image_id(reference)
        if identifier is None:
            finish(1)
        if fmt == "{{.Id}}":
            print(identifier)
        elif fmt == "{{.Os}}/{{.Architecture}}":
            print("linux/amd64")
        elif fmt == "{{json .RepoTags}}":
            print(json.dumps([ref for ref, value in state["tags"].items() if value == identifier]))
        else:
            finish(2)
    finish()
if args[:2] == ["image", "rm"]:
    reference = args[2]
    identifier = image_id(reference)
    if identifier is None or identifier in state["containers"].values():
        finish(3)
    if reference in state["tags"]:
        del state["tags"][reference]
    elif any(value == identifier for value in state["tags"].values()):
        finish(4)
    if identifier not in state["tags"].values():
        state["images"].remove(identifier)
    finish()
if args[:2] == ["image", "load"]:
    for reference, identifier in state["archive"].items():
        state["tags"][reference] = identifier
        if identifier not in state["images"]:
            state["images"].append(identifier)
    finish()
if args[:2] == ["ps", "-aq"]:
    print("\n".join(state["containers"]))
    finish()
if args[0] == "inspect":
    fmt = args[args.index("--format") + 1]
    for container in [part for part in args[1:] if part != "--format" and part != fmt]:
        if container not in state["containers"]:
            finish(5)
        print(state["containers"][container])
    finish()
if args[0] == "info":
    print("linux/x86_64")
    finish()
if args[0] == "exec":
    print("sha256:" + "9" * 64)
    finish()
if args[0] == "compose":
    command = args[args.index("--env-file") + 2:]
    if command == ["config", "--images"]:
        print("\n".join([*state["archive"], "postgres:17", "redis:8", "minio:latest"]))
    elif command[:1] == ["up"]:
        for service in ("api", "worker", "maintenance", "web"):
            if service in command:
                image = WEB if service == "web" else BACKEND
                state["containers"][service + "-1"] = state["tags"][image]
    elif command[:2] == ["ps", "-q"]:
        print(command[-1] + "-1")
    finish()
finish(6)
'''


class DeployImageCleanupTests(unittest.TestCase):
    """停止済み container と無関係 image を守って旧 image を片づける。"""

    @staticmethod
    def deploy_script() -> str:
        """GNU make の recipe 展開に合わせて、隔離 shell へ deploy 部分を渡す。"""

        source = (ROOT / "Makefile").read_text().splitlines()
        start = source.index("deploy:") + 1
        end = source.index("bootstrap-admin:")
        recipe = "\n".join(
            line[2:] if line.startswith("\t@") else line[1:]
            for line in source[start:end] if line.startswith("\t")
        )
        return (
            recipe.replace(
                "$(COMPOSE)",
                'docker compose --project-directory . --file compose.yml --env-file ".env"',
            )
            .replace("$(ENV_FILE)", ".env")
            .replace("$(MAKE)", "true")
            .replace("$$", "$")
        )

    def test_cleanup_runs_before_load_and_after_successful_recreation(self) -> None:
        """旧 tag は導入前、旧稼働 image は container 交換後に削除する。"""

        with tempfile.TemporaryDirectory(prefix="skm image cleanup ") as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "Makefile", root / "Makefile")
            (root / ".env").write_text("FIXTURE=1\n")
            (root / "images.tar").write_text("fixture archive")
            state = {
                "images": [
                    OLD_BACKEND, OLD_WEB, NEW_BACKEND, NEW_WEB, HELD, OTHER,
                    BACKEND_UNUSED, WEB_UNUSED, "sha256:" + "0" * 64,
                ],
                "tags": {
                    BACKEND: OLD_BACKEND,
                    "skillmind/backend:old-alias": OLD_BACKEND,
                    "skillmind/backend:unused": BACKEND_UNUSED,
                    "skillmind/backend:held": HELD,
                    WEB: OLD_WEB,
                    "skillmind/web:unused": WEB_UNUSED,
                    "unrelated/fixture:unused": OTHER,
                    "postgres:17": "sha256:" + "0" * 64,
                    "redis:8": "sha256:" + "0" * 64,
                    "minio:latest": "sha256:" + "0" * 64,
                },
                "containers": {
                    "api-1": OLD_BACKEND,
                    "worker-1": OLD_BACKEND,
                    "maintenance-1": OLD_BACKEND,
                    "web-1": OLD_WEB,
                    "stopped-job": HELD,
                },
                "archive": {BACKEND: NEW_BACKEND, WEB: NEW_WEB},
            }
            (root / "state.json").write_text(json.dumps(state))
            docker = root / "docker"
            docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
            docker.chmod(0o700)
            environment = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "SKM_FAKE_ROOT": str(root),
                "ENV_FILE": ".env",
            }
            for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
                environment.pop(name, None)
            result = subprocess.run(
                ["sh", "-euc", self.deploy_script()], cwd=root, env=environment,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            trace = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
            load_at = next(i for i, args in enumerate(trace) if args[:2] == ["image", "load"])
            removed = [(i, args[2]) for i, args in enumerate(trace) if args[:2] == ["image", "rm"]]
            self.assertEqual(
                {reference for i, reference in removed if i < load_at},
                {"skillmind/backend:unused", "skillmind/web:unused"},
            )
            self.assertEqual(
                {reference for i, reference in removed if i > load_at},
                {"skillmind/backend:old-alias", OLD_WEB},
                removed,
            )
            current = json.loads((root / "state.json").read_text())
            self.assertEqual(current["tags"][BACKEND], NEW_BACKEND)
            self.assertEqual(current["tags"][WEB], NEW_WEB)
            self.assertEqual(current["containers"]["stopped-job"], HELD)
            self.assertIn(HELD, current["images"])
            self.assertIn(OTHER, current["images"])
            self.assertNotIn(OLD_BACKEND, current["images"])
            self.assertNotIn(OLD_WEB, current["images"])
            self.assertFalse(
                any("--force" in args for args in trace if args[:2] == ["image", "rm"])
            )


if __name__ == "__main__":
    unittest.main()
