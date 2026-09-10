"""環境変数から読み込む application 設定を定義する。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """環境変数を正本とする application 設定。

    Production Secret は制御された API/Worker 境界で注入し、Run workspace や Agent
    subprocess の環境へ複製してはならない。
    """

    model_config = SettingsConfigDict(
        env_prefix="PROJECTMIND_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_port: int = 8000
    context_path: str = Field(
        default="/projectmind",
        pattern=r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$",
    )
    contracts_dir: Path = Path("contracts")
    trusted_proxy_cidrs: str = "127.0.0.1"
    sse_heartbeat_seconds: int = Field(default=15, ge=5, le=60)
    auth_login_csrf_ttl_seconds: int = Field(default=300, ge=60, le=900)
    auth_session_idle_minutes: int = Field(default=30, ge=5, le=240)
    auth_session_absolute_hours: int = Field(default=12, ge=1, le=168)
    auth_admin_session_absolute_hours: int = Field(default=8, ge=1, le=24)
    auth_login_attempts_per_minute: int = Field(default=5, ge=1, le=30)
    # 来源は challenge 取得も含む HTTP request 数、account/組合は login 試行数である。
    auth_login_account_attempts_per_minute: int = Field(default=15, ge=1, le=100)
    auth_login_source_requests_per_minute: int = Field(default=100, ge=2, le=10000)
    auth_login_protection_timeout_seconds: float = Field(default=2, gt=0, le=10)

    database_url: str = "postgresql+asyncpg://projectmind:projectmind@localhost:5432/projectmind"
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "projectmind:runs"
    worker_dispatch_enabled: bool = False
    outbox_batch_size: int = Field(default=20, ge=1, le=100)
    run_lease_seconds: int = Field(default=60, ge=30, le=300)
    run_max_attempts: int = Field(default=3, ge=1, le=10)
    # モデル待機の wall timeout と分け、heartbeat があっても資源準備を無期限にしない。
    run_preparation_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    run_workspace_root: Path = Path("/var/lib/projectmind/runs")

    # 凍結入力の存量と単一 search の走査量は別の制限。各 root と全 root の双方を守り、
    # manifest 等を含む最終量の超過は、部分入力を渡さず fail closed とする。
    workspace_materialize_max_bytes: int = Field(default=10_485_760, ge=1, le=104_857_600)
    workspace_materialize_max_files: int = Field(default=500, ge=1, le=5_000)
    workspace_materialize_total_max_bytes: int = Field(default=104_857_600, ge=1, le=536_870_912)
    workspace_materialize_total_max_files: int = Field(default=5_000, ge=1, le=50_000)

    # git/svn command 1 回あたりの上限。準備全体・モデル実行の期限とは別に remote を打ち切る。
    repository_command_timeout_seconds: int = Field(default=120, ge=5, le=600)
    # 扇出子 Agent 一 branch あたりの打ち切り時間。Run の wall timeout(900 秒)より必ず短くし、
    # 一路の停滞が Run 全体の期限を食い潰さないようにする (計画 §23 D6)。
    subagent_branch_timeout_seconds: int = Field(default=300, ge=30, le=600)

    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_bucket: str = "projectmind"
    object_storage_access_key: str = "projectmind"
    object_storage_secret_key: str = "projectmind"
    # 保存先を再作成したら新 UUID にする。未指定では文書の blob 操作を放行しない。
    object_storage_namespace_id: UUID | None = None

    # MANAGED SecretReference の envelope 暗号鍵(KEK)。"version:base64key" を "," で連ねた
    # keyring で、先頭が新規封入用の active 鍵、残りは rotation 中の復号専用の旧鍵。未設定なら
    # MANAGED resolver は無効(fail closed)。KEK は DB/backup/log と分離して保管する。
    managed_secret_kek: str | None = None

    # アップロード上限と Project 単位配額、許可 content-type: fail-closed の allowlist。
    document_max_bytes: int = Field(default=26_214_400, ge=1, le=104_857_600)
    project_document_quota_bytes: int = Field(default=524_288_000, ge=1)
    document_allowed_content_types: tuple[str, ...] = (
        "text/plain",
        "text/markdown",
        "text/csv",
        # HTML は保存だけ許可する。配信は content endpoint が常に attachment を強制し、
        # 画面 preview は script 無効の空 sandbox iframe に限るため、同源で描画される経路はない。
        "text/html",
        "application/json",
        "application/yaml",
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "application/zip",
    )

    # endpoint が Anthropic structured outputs 非対応 (例 Kimi 互換 API は json_object のみで
    # JSON を本文 text に返す) の時 true。prompt 誘導の text JSON を候補として受理し、下流の
    # schema 検証 + retry で契約を担保する。既定 false は structured outputs を必須とする。
    skill_interpreter_accept_prompt_json: bool = False

    @property
    def auth_session_cookie_name(self) -> str:
        """Production だけ __Host- prefix の強制条件を満たす cookie 名を返す。"""

        if self.environment == "production":
            return "__Host-projectmind_session"
        return "projectmind_session"

    @property
    def auth_cookie_secure(self) -> bool:
        """HTTPS が契約上必須の production でだけ Secure cookie を強制する。"""

        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process 内で共有する不変な Settings instance を返す。"""

    return Settings()
