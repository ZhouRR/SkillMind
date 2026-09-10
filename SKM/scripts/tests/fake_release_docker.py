"""実 Docker の代わりに固定 fixture の image/container 状態と argv を記録する。"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

BASE = Path(os.environ["SKM_FAKE_ROOT"])


def main(args: list[str]) -> int:
    """外部接続せず、Makefile/PowerShell が要求した操作だけを再現する。"""

    state = json.loads((BASE / "state.json").read_text())
    with (BASE / "trace.jsonl").open("a") as stream:
        stream.write(json.dumps(args) + "\n")
    joined = " ".join(args)
    if os.environ.get("SKM_FAKE_TERMINATE_SUFFIX") and joined.endswith(
        os.environ["SKM_FAKE_TERMINATE_SUFFIX"]
    ):
        os.kill(os.getppid(), signal.SIGTERM)
        return 0
    suffix = os.environ.get("SKM_FAKE_FAIL_SUFFIX")
    if suffix and joined.endswith(suffix):
        print("fixture docker failure: requested operation failed", file=sys.stderr)
        return 17
    services = state["config"]["services"]
    if args[:2] == ["image", "save"]:
        output = Path(args[args.index("--output") + 1])
        references = args[args.index("--output") + 2 :]
        output.write_text(json.dumps({ref: state["images"][ref] for ref in references}))
    elif args[:2] == ["image", "load"]:
        state["images"].update(json.loads(Path(args[args.index("--input") + 1]).read_text()))
        print("Loaded fixture images")
    elif args[:2] == ["image", "inspect"]:
        position = args.index("--format")
        for reference in args[2:position]:
            item = state["images"].get(reference)
            if item is None:
                print(f"No such image: {reference}", file=sys.stderr)
                return 1
            template = args[position + 1]
            if template == "{{.Id}}":
                print(item["id"])
            elif template == "{{.Os}}/{{.Architecture}}":
                print(item["platform"])
            else:
                raise AssertionError(args)
    elif args[0] == "info":
        print(state.get("server_platform", "linux/x86_64"))
    elif args[0] == "compose":
        if "--no-interpolate" in args:
            assert "--no-env-resolution" in args
            assert os.environ["COMPOSE_DISABLE_ENV_FILE"] == "true"
            assert not os.environ.get("COMPOSE_ENV_FILES")
            assert os.environ["COMPOSE_PROFILES"] == "skillmind-export-no-profiles"
            print(json.dumps(state["config"]))
            return 0
        assert args[1:5] == ["--project-directory", ".", "--file", "compose.yml"], args
        assert args[5] == "--env-file", args
        if not Path(args[6]).is_file():
            print("Environment file is unavailable", file=sys.stderr)
            return 1
        command = args[7:]
        if command[0] == "run" and "--pull" in command:
            print("unknown flag: --pull", file=sys.stderr)
            return 16
        if command == ["config", "--quiet"]:
            pass
        elif command == ["config", "--images"]:
            for service in services.values():
                print(service["image"])
        elif command[:3] == ["pull", "--policy", "missing"]:
            for name in command[3:]:
                reference = services[name]["image"]
                if reference not in state["images"]:
                    if os.environ.get("SKM_FAKE_OFFLINE"):
                        print("Fixture registry unavailable", file=sys.stderr)
                        return 1
                    state["images"][reference] = {
                        "id": "sha256:" + "c" * 64,
                        "platform": "linux/amd64",
                    }
        elif command[0] == "stop":
            for name in command[3:]:
                if name in state["containers"]:
                    state["containers"][name] = "exited"
        elif command[0] == "up":
            names = [arg for arg in command[1:] if arg in services]
            for name in names:
                state["containers"][name] = (
                    "exited(0)" if name == "object-storage-init" else "running"
                )
        elif command[0] == "run":
            assert services["migrate"]["pull_policy"] == "never"
            if command[-1] == "migrate":
                state["revision"] = "head"
            else:
                print('{"status": "ready"}')
        elif command == ["ps", "--all"]:
            print(json.dumps(state["containers"]))
        else:
            raise AssertionError(command)
    else:
        raise AssertionError(args)
    (BASE / "state.json").write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
