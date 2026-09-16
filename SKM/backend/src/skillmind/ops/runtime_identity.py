"""モデル・業務 DB を呼ばずに、配備済み Backend の設定 identity を比較する。"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.release import configured_execution_features
from skillmind.skills.wiring import build_skill_interpreter

if TYPE_CHECKING:
    from collections.abc import Sequence

    from skillmind.core.settings import Settings


def runtime_report(settings: Settings) -> dict[str, Any]:
    """実組立関数の公開 identity と配送設定だけを返し、資格情報は出力しない。

    同一 fingerprint は設定の一致だけを示す。ログイン済み、外部接続可能、既存 job が
    完了したことは証明しない。保守 Worker でも検査用に宣言だけを組み立てる。
    """

    interpreter, catalog, identity, model = build_skill_interpreter(settings)
    features = configured_execution_features(settings)
    return {
        "report_version": "skillmind.backend-runtime-identity/v1",
        "engine": settings.agent_sdk,
        "interpreter_available": all(
            item is not None for item in (interpreter, catalog, identity, model)
        ),
        "interpreter": identity.to_dict() if identity is not None else None,
        "catalog_checksum": catalog.checksum if catalog is not None else None,
        "runtime_profile": getattr(interpreter, "runtime_profile", None),
        "model": model,
        "queue_name": settings.queue_name,
        "worker_dispatch_enabled": settings.worker_dispatch_enabled,
        "features": {
            "deferred": features.deferred,
            "database_writes": features.database_writes,
            "document_writes": features.document_writes,
            "git_writes": features.git_writes,
            "scheduling": settings.scheduling_enabled,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """診断だけを実行する。未設定 Interpreter を導入/既存 task の停止条件にしない。"""

    parser = argparse.ArgumentParser(description="Inspect Backend configuration without model calls")
    parser.add_argument("--fingerprint", action="store_true", help="Print only the stable checksum")
    args = parser.parse_args(argv)
    try:
        from skillmind.core.settings import get_settings

        report = runtime_report(get_settings())
        body = canonical_json(report)
    except (OSError, ValueError):
        # 構成例外に含まれ得る接続情報や入力値を表示しない。
        print("Backend runtime identity could not be inspected.", file=sys.stderr)
        return 2
    if not report["interpreter_available"]:
        print(
            "Interpreter is unavailable; this check does not block import or published tasks.",
            file=sys.stderr,
        )
    print("sha256:" + sha256_hex(body) if args.fingerprint else body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
