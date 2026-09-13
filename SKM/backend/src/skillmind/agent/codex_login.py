"""Worker 専用領域へ ChatGPT device-code login を保存する運用入口。"""

from __future__ import annotations

import argparse
import asyncio
import json

from skillmind.agent.codex_runtime import (
    CodexRuntimeConfiguration,
    create_codex_client,
    start_codex,
)
from skillmind.core.settings import Settings


async def run_login(*, status_only: bool) -> None:
    """秘密 token を表示せず、device verification URL/code と完了状態だけを返す。"""

    settings = Settings()
    configuration = CodexRuntimeConfiguration(
        settings.codex_model,
        settings.codex_reasoning_effort,
        settings.codex_home,
    )
    client = create_codex_client(configuration.client_config())
    try:
        await start_codex(client)
        if status_only:
            account = await asyncio.to_thread(client.account_read)
            print(json.dumps({"authenticated": account.account is not None}))
            return
        started = await asyncio.to_thread(client.account_login_start, {"type": "chatgptDeviceCode"})
        handle = started.root
        if handle.type != "chatgptDeviceCode":
            raise RuntimeError("Device login was not started")
        print(
            json.dumps(
                {"verification_url": handle.verification_url, "user_code": handle.user_code}
            ),
            flush=True,
        )
        async with asyncio.timeout(900):
            completed = await asyncio.to_thread(client.wait_for_login_completed, handle.login_id)
        print(json.dumps({"authenticated": completed.success}), flush=True)
        if not completed.success:
            raise RuntimeError("Device login did not complete")
    finally:
        await asyncio.to_thread(client.close)


def main() -> None:
    """対話 password/API key を要求しない device login または只読 status を実行する。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(run_login(status_only=arguments.status))


if __name__ == "__main__":
    main()
