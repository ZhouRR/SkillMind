"""Redmine issue.update/v1 の optimistic concurrency と read-back Provider を実装する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.issue_update import (
    ISSUE_UPDATE_CAPABILITY,
    ISSUE_UPDATE_PROVIDER_VERSION,
)

_MAX_RESPONSE_BYTES = 1_048_576
_DEFAULT_TIMEOUT_SECONDS = 15.0
_EFFECT_PROTOCOL = "skillmind.redmine-effect/v1"
_EFFECT_CAPABILITIES_PATH = "/.well-known/skillmind-effect-provider.json"
_ISSUE_UPDATE_PATH = "/skillmind/effects/issue.update/v1/issues"


class EffectProviderStaleError(RuntimeError):
    """Target revision が変化し、desired state とも一致しないことを表す。"""


class EffectProviderVerificationError(RuntimeError):
    """Write 成功後の read-back が期待した state と一致しないことを表す。"""


class EffectProviderTransportError(RuntimeError):
    """外部 Provider failure の安全な分類と retryability を保持する。"""

    def __init__(self, code: str, *, retryable: bool) -> None:
        """Response body や credential を含めず安定した error code を保持する。"""

        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RedmineIssueSnapshot:
    """Read-back に必要な issue revision と scoped field values。"""

    issue_id: str
    revision: str
    fields: dict[str, Any]


class RedmineTransport(Protocol):
    """Redmine HTTP access を testable boundary に閉じる port。"""

    async def read_issue(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        field_keys: tuple[str, ...],
    ) -> RedmineIssueSnapshot:
        """指定 field だけを正規化して current revision と共に返す。"""

        ...

    async def ensure_issue_update_protocol(
        self,
        *,
        base_url: str,
        api_key: str,
    ) -> None:
        """Atomic CAS と idempotency を宣言する adapter capability を確認する。"""

        ...

    async def update_issue(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        expected_revision: str,
        fields: Mapping[str, Any],
        idempotency_key: str,
    ) -> bool:
        """Versioned adapter で issue field を原子的に更新し、再実行かを真偽値で返す。"""

        ...


class RedmineIssueUpdateProvider:
    """Approved field SET を pre-read → conditional update → read-back で閉じる。"""

    def __init__(self, transport: RedmineTransport) -> None:
        """Secret を保持しない Redmine transport を注入する。"""

        self._transport = transport

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """Revision mismatch/replay/verification を区別して issue update を実行する。"""

        if (
            execution.capability_version != ISSUE_UPDATE_CAPABILITY
            or execution.provider != "redmine"
        ):
            raise ValueError("EffectExecution does not target the Redmine issue Provider")
        if credential is None:
            raise EffectProviderTransportError("credential_unavailable", retryable=False)
        base_url = execution.integration_config.get("base_url")
        issue_id = execution.target.get("locator")
        expected_revision = execution.precondition.get("revision")
        if not isinstance(base_url, str) or not isinstance(issue_id, str):
            raise ValueError("EffectExecution Integration/target snapshot is invalid")
        if not isinstance(expected_revision, str):
            raise ValueError("EffectExecution precondition snapshot is invalid")
        # Stock Redmine の Issue REST update は原子的な revision 条件と idempotency を
        # 契約化していない。安全な GET discovery を先に行い、対応 adapter が無ければ
        # write request を一切送らず fail closed にする。
        await self._transport.ensure_issue_update_protocol(
            base_url=base_url,
            api_key=credential,
        )
        desired = _desired_fields(execution.changes)
        field_keys = tuple(sorted(desired))
        before = await self._transport.read_issue(
            base_url=base_url,
            api_key=credential,
            issue_id=issue_id,
            field_keys=field_keys,
        )
        if before.revision != expected_revision:
            if _matches(before.fields, desired):
                evidence = _evidence(execution, before, phase="before")
                return EffectProviderResult(
                    before=evidence,
                    after=_evidence(execution, before, phase="after"),
                    verification={
                        "method": "READ_BACK",
                        "matched_paths": [f"/fields/{key}" for key in field_keys],
                        "replayed": True,
                    },
                    replayed=True,
                )
            raise EffectProviderStaleError("Redmine issue revision has changed")
        replayed = await self._transport.update_issue(
            base_url=base_url,
            api_key=credential,
            issue_id=issue_id,
            expected_revision=expected_revision,
            fields=desired,
            idempotency_key=execution.idempotency_key,
        )
        after = await self._transport.read_issue(
            base_url=base_url,
            api_key=credential,
            issue_id=issue_id,
            field_keys=field_keys,
        )
        if not _matches(after.fields, desired):
            raise EffectProviderVerificationError("Redmine read-back did not match desired fields")
        return EffectProviderResult(
            before=_evidence(execution, before, phase="before"),
            after=_evidence(execution, after, phase="after"),
            verification={
                "method": "READ_BACK",
                "matched_paths": [f"/fields/{key}" for key in field_keys],
                "replayed": replayed,
                "before_revision": before.revision,
                "after_revision": after.revision,
            },
            replayed=replayed,
        )


class UrllibRedmineTransport:
    """Redirect を禁止した bounded HTTP transport で Redmine REST API を呼び出す。"""

    def __init__(self, *, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        """一回の HTTP request timeout を固定する。"""

        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("Redmine HTTP timeout is invalid")
        self._timeout_seconds = timeout_seconds

    async def read_issue(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        field_keys: tuple[str, ...],
    ) -> RedmineIssueSnapshot:
        """Redmine issue JSON を scoped field snapshot へ正規化する。"""

        payload = await asyncio.to_thread(
            self._request_json,
            method="GET",
            url=f"{base_url}/issues/{quote(issue_id, safe='')}.json",
            api_key=api_key,
            body=None,
            idempotency_key=None,
        )
        issue = payload.get("issue")
        if not isinstance(issue, Mapping):
            raise EffectProviderTransportError("provider_response_invalid", retryable=False)
        revision = issue.get("updated_on")
        if not isinstance(revision, str) or not revision:
            raise EffectProviderTransportError("provider_response_invalid", retryable=False)
        return RedmineIssueSnapshot(
            issue_id=issue_id,
            revision=revision,
            fields={key: _read_field(issue, key) for key in field_keys},
        )

    async def ensure_issue_update_protocol(
        self,
        *,
        base_url: str,
        api_key: str,
    ) -> None:
        """Well-known document が atomic CAS/idempotency contract を宣言するか確認する。"""

        payload = await asyncio.to_thread(
            self._request_json,
            method="GET",
            url=f"{base_url}{_EFFECT_CAPABILITIES_PATH}",
            api_key=api_key,
            body=None,
            idempotency_key=None,
        )
        if (
            payload.get("protocol") != _EFFECT_PROTOCOL
            or payload.get("provider") != "redmine"
            or payload.get("capabilities") != [ISSUE_UPDATE_CAPABILITY]
            or payload.get("atomic_precondition") != "revision"
            or payload.get("idempotency") != "key"
        ):
            raise EffectProviderTransportError(
                "provider_effect_protocol_unavailable",
                retryable=False,
            )

    async def update_issue(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        expected_revision: str,
        fields: Mapping[str, Any],
        idempotency_key: str,
    ) -> bool:
        """Adapter の versioned CAS endpoint へ expected revision と idempotency を渡す。"""

        body = {
            "capability_version": ISSUE_UPDATE_CAPABILITY,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "issue": _write_fields(fields),
        }
        payload = await asyncio.to_thread(
            self._request_json,
            method="PUT",
            url=f"{base_url}{_ISSUE_UPDATE_PATH}/{quote(issue_id, safe='')}.json",
            api_key=api_key,
            body=body,
            idempotency_key=idempotency_key,
        )
        outcome = payload.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"APPLIED", "REPLAYED"}:
            raise EffectProviderTransportError("provider_response_invalid", retryable=False)
        return outcome == "REPLAYED"

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        api_key: str,
        body: dict[str, Any] | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Secret/response body を error へ反射せず bounded JSON request を実行する。"""

        headers = {
            "Accept": "application/json",
            "X-Redmine-API-Key": api_key,
        }
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["X-Skillmind-Idempotency-Key"] = idempotency_key
        request = Request(url=url, data=encoded, headers=headers, method=method)
        opener = build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if error.code in {409, 412}:
                raise EffectProviderStaleError("Redmine conditional update was rejected") from error
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
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EffectProviderTransportError(
                "provider_response_invalid", retryable=False
            ) from error
        if not isinstance(parsed, dict):
            raise EffectProviderTransportError("provider_response_invalid", retryable=False)
        return parsed


class _NoRedirectHandler(HTTPRedirectHandler):
    """Credential header が別 origin へ転送されないよう redirect を拒否する。"""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """urllib の redirect follow を無効化する。"""

        del req, fp, code, msg, headers, newurl
        return None


def create_redmine_effect_provider() -> RedmineIssueUpdateProvider:
    """Production HTTP transport を持つ Redmine effect Provider を作成する。"""

    return RedmineIssueUpdateProvider(UrllibRedmineTransport())


def _desired_fields(changes: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Validated Proposal changes を Redmine field map へ変換する。"""

    desired: dict[str, Any] = {}
    for change in changes:
        path = change.get("path")
        if (
            not isinstance(path, str)
            or not path.startswith("/fields/")
            or change.get("action") != "SET"
        ):
            raise ValueError("EffectExecution changes are not valid issue field SET operations")
        desired[path.removeprefix("/fields/")] = change.get("value")
    if not desired:
        raise ValueError("EffectExecution contains no issue field changes")
    return desired


def _matches(actual: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Scoped field values が JSON semantics で desired state と一致するかを返す。"""

    return all(actual.get(key) == value for key, value in desired.items())


def _evidence(
    execution: ClaimedEffectExecution,
    snapshot: RedmineIssueSnapshot,
    *,
    phase: str,
) -> EffectEvidenceDraft:
    """Internal URL/credential を含めず re-locatable Evidence draft を作成する。"""

    logical_uri = (
        f"redmine://integration/{execution.integration_id}/issues/{snapshot.issue_id}"
    )
    content = {
        "issue_id": snapshot.issue_id,
        "revision": snapshot.revision,
        "fields": snapshot.fields,
    }
    return EffectEvidenceDraft(
        evidence_type=f"issue.update.{phase}",
        source_uri=logical_uri,
        source_locator={
            "integration_id": str(execution.integration_id),
            "issue_id": snapshot.issue_id,
            "revision": snapshot.revision,
        },
        content=content,
        excerpt=None,
        metadata={
            "phase": phase,
            "capability": ISSUE_UPDATE_CAPABILITY,
            "provider_version": ISSUE_UPDATE_PROVIDER_VERSION,
        },
    )


def _read_field(issue: Mapping[str, Any], field_key: str) -> Any:
    """Redmine response の nested relation/custom field を write field key へ正規化する。"""

    relation_names = {
        "status_id": "status",
        "priority_id": "priority",
        "assigned_to_id": "assigned_to",
        "category_id": "category",
        "fixed_version_id": "fixed_version",
    }
    relation = relation_names.get(field_key)
    if relation is not None:
        value = issue.get(relation)
        return value.get("id") if isinstance(value, Mapping) else None
    if field_key.startswith("custom_field."):
        custom_id = int(field_key.removeprefix("custom_field."))
        custom_fields = issue.get("custom_fields")
        if isinstance(custom_fields, list):
            for item in custom_fields:
                if isinstance(item, Mapping) and item.get("id") == custom_id:
                    return item.get("value")
        return None
    return issue.get(field_key)


def _write_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """custom field path を Redmine API の custom_fields array へ変換する。"""

    payload: dict[str, Any] = {}
    custom_fields: list[dict[str, Any]] = []
    for key, value in fields.items():
        if key.startswith("custom_field."):
            custom_fields.append(
                {"id": int(key.removeprefix("custom_field.")), "value": value}
            )
        else:
            payload[key] = value
    if custom_fields:
        payload["custom_fields"] = custom_fields
    return payload
