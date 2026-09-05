"""KEK rotation: 全 MANAGED SecretReference の密文を active KEK へ再封入する運用コマンド。

`PROJECTMIND_MANAGED_SECRET_KEK` を「新 active 鍵 + 旧鍵」の keyring にした状態で実行する。
各密文はその封入時 KEK version で復号し、active 鍵で再封入する。完了後に旧鍵を keyring から
外せる。credential や KEK は一切出力しない。
"""

from __future__ import annotations

import asyncio

from projectmind.core.secret_crypto import SecretCryptoError, load_secret_cipher
from projectmind.core.settings import Settings
from projectmind.db.resources import create_database_engine, create_session_factory
from projectmind.integrations import IntegrationService


async def rotate_managed_secrets() -> tuple[int, int]:
    """Application 設定の KEK keyring で全 MANAGED 密文を active KEK へ再封入する。"""

    settings = Settings()
    cipher = load_secret_cipher(settings.managed_secret_kek)
    if cipher is None:
        raise SecretCryptoError("PROJECTMIND_MANAGED_SECRET_KEK is not configured")
    engine = create_database_engine(settings)
    try:
        service = IntegrationService(create_session_factory(engine), secret_cipher=cipher)
        return await service.rotate_managed_secrets()
    finally:
        await engine.dispose()


def main() -> int:
    """再封入した件数だけを出力し、credential・KEK・database URL は表示しない。"""

    try:
        rotated, skipped = asyncio.run(rotate_managed_secrets())
    except SecretCryptoError as error:
        print(f"Rotation refused: {error}")
        return 1
    print(f"Managed secrets rotated: {rotated}, already current: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
