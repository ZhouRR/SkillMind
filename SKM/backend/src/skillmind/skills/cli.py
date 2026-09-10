"""Deterministic Skill parser を local ADMIN が確認する CLI を提供する。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from skillmind.skills.importer import (
    DeterministicManifestDraftBuilder,
    SkillImportError,
    SkillPackageParser,
)


def parse_skill(source: Path, contracts_dir: Path) -> dict[str, Any]:
    """Directory Skill を normalized package と Assisted manifest draft へ変換する。"""

    package = SkillPackageParser().parse_directory(source.resolve())
    schema_path = contracts_dir.resolve() / "runtime-manifest" / "v1alpha1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("RuntimeManifest Schema must be a JSON object")
    manifest = DeterministicManifestDraftBuilder(schema).build(package)
    return {"normalized_package": package.to_dict(), "runtime_manifest_draft": manifest}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI argument を解析し、機密を含まない JSON result/error を出力する。"""

    parser = argparse.ArgumentParser(
        description="Parse a directory Skill without invoking a model or executing scripts."
    )
    parser.add_argument("source", type=Path, help="Skill source directory")
    parser.add_argument(
        "--contracts-dir",
        type=Path,
        default=Path("contracts"),
        help="Skillmind contracts directory",
    )
    arguments = parser.parse_args(argv)
    try:
        result = parse_skill(arguments.source, arguments.contracts_dir)
    except SkillImportError as error:
        print(
            json.dumps(
                {"status": "error", "code": error.code, "path": error.path},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "parser_configuration_error",
                    "type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
