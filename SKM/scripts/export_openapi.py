"""FastAPI から正規化した OpenAPI contract を書き出す。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from skillmind.api.main import app  # noqa: E402

OUTPUT = ROOT / "contracts" / "openapi" / "skillmind-api.v1.json"


def main() -> None:
    """現在の application から OpenAPI JSON を再生成する。"""

    # 引数なし専用 CLI。--help などの未知引数で契約 file を誤って上書きしないよう、
    # 書き出す前に argparse で引数を検証する。
    argparse.ArgumentParser(description=__doc__).parse_args()
    # Sort と末尾改行を固定し、意味のない contract diff を発生させない。
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
