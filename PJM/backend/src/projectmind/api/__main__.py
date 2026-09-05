"""ProjectMind API を console script から起動する entry point。"""

from __future__ import annotations

import uvicorn

from projectmind.core.settings import get_settings


def main() -> None:
    """環境設定を読み込み、proxy header 制約付きで Uvicorn を起動する。"""

    settings = get_settings()
    uvicorn.run(
        "projectmind.api.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_cidrs,
    )


if __name__ == "__main__":
    main()
