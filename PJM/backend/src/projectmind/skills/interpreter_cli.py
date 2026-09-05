"""Model を呼ばない Skill Interpreter fixture runner CLI を提供する。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from projectmind.skills.importer import SkillImportError, SkillPackageParser
from projectmind.skills.interpreter import (
    InterpreterFixtureRunner,
    SkillStaticAnalyzer,
    UnsafeSkillSourceError,
    build_interpreter_request,
    load_capability_catalog,
    load_inline_text_files,
    load_interpreter_system_skill,
)


def run_fixture(
    *,
    source: Path,
    fixture_response: Path,
    contracts_dir: Path,
    system_skill: Path,
    capability_catalog: Path,
) -> dict[str, Any]:
    """Directory source と固定 response を S1 contract で解析・検証する。"""

    package = SkillPackageParser().parse_directory(source.resolve())
    analysis = SkillStaticAnalyzer().analyze(
        package,
        load_inline_text_files(source, package),
    )
    request = build_interpreter_request(
        package=package,
        analysis=analysis,
        catalog=load_capability_catalog(capability_catalog),
        system_skill=load_interpreter_system_skill(system_skill),
    )
    response = _load_json(fixture_response)
    validated = InterpreterFixtureRunner(contracts_dir).run(request, response)
    return {"request": request, "response": validated}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI argument を解析し、source 値を含まない安定 error を返す。"""

    parser = argparse.ArgumentParser(
        description="Validate a Skill interpreter fixture without invoking a model."
    )
    parser.add_argument("source", type=Path, help="Skill source directory")
    parser.add_argument("fixture_response", type=Path, help="Interpreter response fixture")
    parser.add_argument("--contracts-dir", type=Path, default=Path("contracts"))
    parser.add_argument(
        "--system-skill",
        type=Path,
        default=Path("skills/projectmind-skill-interpreter"),
    )
    parser.add_argument(
        "--capability-catalog",
        type=Path,
        default=Path("contracts/examples/skill-capability-catalog.v1.json"),
    )
    arguments = parser.parse_args(argv)
    try:
        result = run_fixture(
            source=arguments.source,
            fixture_response=arguments.fixture_response,
            contracts_dir=arguments.contracts_dir,
            system_skill=arguments.system_skill,
            capability_catalog=arguments.capability_catalog,
        )
    except SkillImportError as error:
        payload = {"status": "error", "code": error.code, "path": error.path}
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2
    except UnsafeSkillSourceError:
        print(
            json.dumps({"status": "error", "code": "unsafe_skill_source"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        payload = {
            "status": "error",
            "code": "interpreter_fixture_invalid",
            "type": type(error).__name__,
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _load_json(path: Path) -> dict[str, Any]:
    """Fixture path から JSON object だけを読み込む。"""

    value = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Interpreter fixture response must be a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
