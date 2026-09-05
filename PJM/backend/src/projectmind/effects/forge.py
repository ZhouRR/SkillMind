"""落地した branch から Pull Request / Merge Request を開く transport (計画 §20 R4)。

PR は「承認の代わり」ではない。`repository.write/v1` は事前許可の対象外であり、apply の前に
必ず人手承認を経ている。PR を開くのは**評審と統合の入口を人へ渡す**ためで、ProjectMind 側は
自動 merge を一切行わない。

forge の種別・API endpoint・project 識別子は Integration config が明示する。host 名から推測
しないのは、自建 instance で外れたときに「別の場所へ書こうとする」状態になるため——推測より
fail closed を選ぶ。設定が無い Integration では PR を開かず、branch と commit だけを残す。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from projectmind.effects.redmine import (
    EffectProviderTransportError,
    _NoRedirectHandler,
)

FORGE_KINDS = frozenset({"github", "gitlab"})

_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    """開かれた (または既に在った) PR の公開参照。"""

    url: str
    identifier: str
    created: bool


class ForgeTransport(Protocol):
    """Forge の PR API を呼ぶ port。凭据は Provider 境界内でだけ解決される。"""

    async def ensure_pull_request(
        self,
        *,
        kind: str,
        api_base_url: str,
        project: str,
        token: str,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        """同じ source branch の PR が在れば再利用し、無ければ開く。"""

        ...


class UrllibForgeTransport:
    """Redirect を禁止した bounded HTTP transport で GitHub/GitLab の PR API を呼ぶ。"""

    def __init__(self, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        """一回の HTTP request timeout を固定する。"""

        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("Forge HTTP timeout is invalid")
        self._timeout_seconds = timeout_seconds

    async def ensure_pull_request(
        self,
        *,
        kind: str,
        api_base_url: str,
        project: str,
        token: str,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        """既存 PR を先に探し、無いときだけ作成する (再実行で重複を作らない)。"""

        if kind not in FORGE_KINDS:
            raise EffectProviderTransportError("effect_provider_failed", retryable=False)
        existing = await asyncio.to_thread(
            self._find_existing,
            kind=kind,
            api_base_url=api_base_url,
            project=project,
            token=token,
            source_branch=source_branch,
        )
        if existing is not None:
            return existing
        return await asyncio.to_thread(
            self._create,
            kind=kind,
            api_base_url=api_base_url,
            project=project,
            token=token,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            body=body,
        )

    def _find_existing(
        self,
        *,
        kind: str,
        api_base_url: str,
        project: str,
        token: str,
        source_branch: str,
    ) -> PullRequestRef | None:
        """同一 source branch の open な PR を探す。"""

        encoded_project = quote(project, safe="" if kind == "gitlab" else "/")
        branch = quote(source_branch, safe="")
        if kind == "github":
            url = f"{api_base_url}/repos/{encoded_project}/pulls?state=open&head={branch}"
        else:
            url = (
                f"{api_base_url}/projects/{encoded_project}/merge_requests"
                f"?state=opened&source_branch={branch}"
            )
        payload = self._request(method="GET", url=url, kind=kind, token=token, body=None)
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        if not isinstance(first, dict):
            return None
        return _reference(kind, first, created=False)

    def _create(
        self,
        *,
        kind: str,
        api_base_url: str,
        project: str,
        token: str,
        source_branch: str,
        target_branch: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        """PR/MR を作成する。"""

        encoded_project = quote(project, safe="" if kind == "gitlab" else "/")
        if kind == "github":
            url = f"{api_base_url}/repos/{encoded_project}/pulls"
            payload: dict[str, Any] = {
                "title": title,
                "head": source_branch,
                "base": target_branch,
                "body": body,
            }
        else:
            url = f"{api_base_url}/projects/{encoded_project}/merge_requests"
            payload = {
                "title": title,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "description": body,
            }
        created = self._request(method="POST", url=url, kind=kind, token=token, body=payload)
        if not isinstance(created, dict):
            raise EffectProviderTransportError("provider_response_invalid", retryable=False)
        return _reference(kind, created, created=True)

    def _request(
        self,
        *,
        method: str,
        url: str,
        kind: str,
        token: str,
        body: dict[str, Any] | None,
    ) -> Any:
        """Secret と response body を error へ反射せず bounded JSON request を実行する。"""

        headers = {"Accept": "application/json"}
        if kind == "github":
            headers["Authorization"] = f"Bearer {token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        else:
            headers["PRIVATE-TOKEN"] = token
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url=url, data=encoded, headers=headers, method=method)
        opener = build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise EffectProviderTransportError(
                "provider_http_error",
                retryable=error.code == 429 or 500 <= error.code <= 599,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise EffectProviderTransportError("provider_unavailable", retryable=True) from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise EffectProviderTransportError("provider_response_too_large", retryable=False)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EffectProviderTransportError(
                "provider_response_invalid", retryable=False
            ) from error


def _reference(kind: str, payload: dict[str, Any], *, created: bool) -> PullRequestRef:
    """Forge 応答から公開参照 (URL と番号) だけを取り出す。"""

    url = payload.get("html_url") if kind == "github" else payload.get("web_url")
    number = payload.get("number") if kind == "github" else payload.get("iid")
    if not isinstance(url, str) or not url or number is None:
        raise EffectProviderTransportError("provider_response_invalid", retryable=False)
    return PullRequestRef(url=url, identifier=str(number), created=created)
