"""Shell/PowerShell の release 回帰専用で、実 Docker には接続しない。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from test_deploy import DAEMON, IMAGE_ENV, IMAGES, READY, SERVICE_IMAGES


def main() -> int:
    """一時 fixture に限定して process protocol と失敗を模倣する。"""

    root = Path(os.environ["SKM_FAKE_ROOT"])
    state = json.loads((root / "state.json").read_text())
    args = sys.argv[1:]
    with (root / "trace.jsonl").open("a") as stream:
        stream.write(json.dumps(args) + "\n")
    if os.environ.get("SKM_FAKE_FAIL") and os.environ["SKM_FAKE_FAIL"] in " ".join(args):
        print("fixture-private-error", file=sys.stderr)
        return 17
    if os.environ.get("SKM_FAKE_FAIL_EXACT") == " ".join(args[9:]):
        print("fixture-private-error", file=sys.stderr)
        return 17
    if args[0] == "compose":
        args = args[9:]
        if args == ["config", "--quiet"]:
            return 0
        if args == ["config", "--format", "json"]:
            print(json.dumps(state["config"]))
        elif args == ["config", "--images"]:
            print("\n".join(service["image"] for service in state["config"]["services"].values()))
        elif "skillmind.ops.preflight" in args:
            print(json.dumps(READY))
        elif args[:1] == ["run"]:
            if args[-1] != "migrate":
                raise ValueError("unexpected command")
        elif args[:1] == ["up"]:
            services = args[9:]
            for service in services:
                row = next(row for row in state["containers"] if row["service"] == service)
                row.update(state="running", image=SERVICE_IMAGES[service])
            (root / "state.json").write_text(json.dumps(state))
        elif args[:1] == ["build"]:
            pass
        else:
            raise ValueError(f"unexpected compose arguments {args}")
    elif args[:1] == ["info"]:
        print(DAEMON if args[-1] == "{{.ID}}" else state.get("platform", "linux/x86_64"))
    elif args[:1] == ["ps"]:
        print("\n".join(row["id"] for row in state["containers"]))
    elif args[:2] == ["container", "inspect"]:
        rows = [row for row in state["containers"] if row["id"] in args]
        if "--format" in args:
            template = args[args.index("--format") + 1]
            for row in rows:
                print(
                    row["image"]
                    if template == "{{.Image}}"
                    else f"{row['service']}|{row['oneoff']}|{row['state']}"
                )
        else:
            print(
                json.dumps(
                    [
                        {
                            "Id": row["id"],
                            "Image": row["image"],
                            "Config": {
                                "Env": row["environment"],
                                "Labels": {
                                    "com.docker.compose.project": row["project"],
                                    "com.docker.compose.service": row["service"],
                                    "com.docker.compose.oneoff": row["oneoff"],
                                },
                            },
                            "State": {
                                "Status": row["state"],
                                "ExitCode": row["exit_code"],
                                "Health": {"Status": row["health"]},
                            },
                        }
                        for row in rows
                    ]
                )
            )
    elif args[:2] == ["image", "inspect"]:
        reference = args[-1]
        reference = {
            "skillmind/backend:0.1.0": IMAGES["backend"],
            "skillmind/web:0.1.0": IMAGES["web"],
        }.get(reference, reference)
        if "--format" not in args:
            print(
                json.dumps(
                    [
                        {
                            "Id": reference,
                            "Os": "linux",
                            "Architecture": "amd64",
                            "Config": {"Labels": {"org.skillmind.context-path": "/skillmind"}},
                        }
                    ]
                )
            )
            return 0
        template = args[args.index("--format") + 1]
        if template == "{{.Id}}|{{.Os}}/{{.Architecture}}":
            print(f"{reference}|linux/amd64")
        elif template == "{{.Id}}":
            print(reference)
        elif "org.skillmind.context-path" in template:
            print("/skillmind")
        else:
            print(json.dumps({"id": reference, "environment": IMAGE_ENV}))
    elif args[:2] == ["image", "load"]:
        if not sys.stdin.buffer.read():
            raise ValueError("archive not supplied on stdin")
    elif args[:2] == ["image", "save"]:
        shutil.copyfile(root / "images.tar", args[args.index("--output") + 1])
    elif args[:1] == ["version"]:
        print("linux")
    elif args[:1] == ["pull"]:
        pass
    elif args[:1] == ["run"]:
        offset = args.index("/opt/skillmind-deploy/deploy_checks.py")
        command = [
            sys.executable,
            str(Path(__file__).parents[1] / "deploy_checks.py"),
            *args[offset + 1 :],
        ]
        for token in args:
            if token.startswith("type=bind,"):
                fields = dict(field.split("=", 1) for field in token.split(",") if "=" in field)
                if fields["dst"] == "/snapshot":
                    command.extend(("--directory", fields["src"]))
                elif fields["dst"] == "/release/images.tar":
                    command.extend(("--archive", fields["src"]))
        return subprocess.run(command, check=False).returncode
    else:
        raise ValueError(f"unexpected docker arguments {args}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
