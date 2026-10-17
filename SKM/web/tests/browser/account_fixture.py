"""各ブラウザ runner が共有する架空アカウントと遮断済み HTTP API を提供する。"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error, Route

ACTOR = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000003"
TARGET = "00000000-0000-4000-8000-000000000100"
CSRF = "c" * 32
PASSWORD = "browser-only new passphrase"
CURRENT_PASSWORD = "browser-only current passphrase"
NOW = "2026-09-09T00:00:00Z"
PROJECT = "00000000-0000-4000-8000-000000000020"
NEXT_PROJECT = "00000000-0000-4000-8000-000000000021"


class ResponseGate:
    """HTTP 応答を既知の handle で止め、次の会話が成立してから解放する。"""

    def __init__(self) -> None:
        """case 内だけで共有する到達/解放/完了と拒否を保持する。"""
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()
        self.failure: tuple[int, str] | None = None


def project(project_id: str) -> dict:
    """実 Project を操作せず sidebar の選択と module lifecycle を再現する。"""
    return {
        "project_id": project_id,
        "key": f"fixture-{project_id[-12:]}",
        "name": f"Browser project {project_id[-2:]}",
        "description": "Browser fixture",
        "status": "ACTIVE",
        "settings": {},
        "retention_days": 30,
        "row_version": 7,
        "created_at": NOW,
        "updated_at": NOW,
    }


def account(user_id: str, index: int = 0, role: str = "USER") -> dict:
    """許可済み公開 field だけを持つ架空アカウントを返す。"""

    return {
        "user_id": user_id,
        "email": "reader@example.com" if user_id == ACTOR else f"person{index:02}@example.com",
        "display_name": "Browser reader" if user_id == ACTOR else f"Browser person {index:02}",
        "system_role": role,
        "status": "ACTIVE",
        "row_version": 7,
        "created_at": NOW,
        "updated_at": NOW,
    }


def session(user_id: str = ACTOR, role: str = "USER") -> dict:
    """実 token を使わず、App と component に同じ会話 identity を渡す。"""

    user = account(user_id, index=99 if user_id == OTHER else 0, role=role)
    return {
        "user": {
            **{key: user[key] for key in ("user_id", "email", "display_name", "system_role")},
            "organization_id": "00000000-0000-4000-8000-000000000002",
        },
        "csrf_token": CSRF,
        "deferred_features_enabled": True,
        "absolute_expires_at": "2099-01-01T00:00:00Z",
    }


class AccountsApi:
    """UI の観測可能な応答だけを模擬し、実 transaction の証明には用いない。"""

    def __init__(self, url: str, role: str = "USER", language: str = "en") -> None:
        """case ごとに account、応答喪失、待機 gate を独立させる。"""

        address = urlsplit(url)
        self.origin = f"{address.scheme}://{address.netloc}"
        self.prefix = address.path.removesuffix("tests/browser/accounts.html") + "api/v1/"
        self.role = role
        self.language = language
        self.actor = ACTOR
        self.users = {ACTOR: account(ACTOR, role=role), OTHER: account(OTHER, 99)}
        self.directory = [f"00000000-0000-4000-8000-{100 + index:012}" for index in range(30)]
        self.users.update(
            {user_id: account(user_id, index) for index, user_id in enumerate(self.directory)}
        )
        self.calls: list[tuple[str, str, dict, object]] = []
        self.unexpected: list[str] = []
        self.failures: list[str] = []
        self.failure: tuple[int, str] | None = None
        self.hold = False
        self.retry_seconds = 1
        self.drop = False
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()
        self.projects: list[dict] = []
        self.preference: str | None = None
        self.gates: dict[tuple[str, str], ResponseGate] = {}

    def mutations(self, suffix: str | None = None) -> list[tuple[str, str, dict, object]]:
        """preference 保存と区別し、対象 account mutation の回数を数える。"""

        return [
            call
            for call in self.calls
            if call[0] != "GET"
            and call[1].startswith("users")
            and not call[1].endswith(("preference", "ui-language"))
            and (suffix is None or call[1] == suffix)
        ]

    async def route(self, route: Route) -> None:
        """不明な API/外部 HTTP を遮断し、fixture handler の失敗も最後に報告する。"""

        try:
            await self.respond(route)
        except Exception as error:
            # Route task の例外を握り潰して UI の unknown 成功扱いにしない。
            self.failures.append(f"{type(error).__name__}: {error}")
            await route.abort()

    async def respond(self, route: Route) -> None:
        """local source 資産だけ通過させ、公開 API の形と CSRF を照合する。"""

        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            self.unexpected.append(request.url)
            await route.abort()
            return
        if not url.path.startswith((self.prefix, "/api/", self.prefix.removesuffix("v1/"))):
            await route.continue_()
            return
        suffix = url.path.removeprefix(self.prefix)
        query = parse_qs(url.query)
        body = request.post_data_json if request.post_data else None
        method = request.method
        self.calls.append((method, suffix, query, body))
        if method != "GET":
            assert request.headers.get("x-csrf-token") == CSRF, "Missing current CSRF"
            assert request.headers.get("origin") == self.origin, "Wrong Origin"
        result: object
        if suffix == "api-keys" and method == "GET":
            result = {"items": []}
        elif suffix == "auth/session" and method == "GET":
            result = session(self.actor, self.role)
        elif suffix == "auth/login-context" and method == "GET":
            result = {"csrf_token": CSRF, "expires_in_seconds": 300}
        elif suffix == "auth/login" and method == "POST":
            assert body == {"email": self.users[self.actor]["email"], "password": PASSWORD}
            result = session(self.actor, self.role)
        elif suffix == "auth/logout" and method == "POST":
            result = {}
        elif suffix == "meta" and method == "GET":
            result = {
                key: "browser fixture" for key in ("name", "version", "phase", "task", "ingress")
            }
        elif suffix == "projects" and method == "GET":
            result = {"items": self.projects.copy()}
        elif suffix.startswith("projects/") and suffix.count("/") == 1 and method == "GET":
            target = suffix.split("/")[1]
            matches = [item for item in self.projects if item["project_id"] == target]
            assert len(matches) == 1, "Requested project is not an authorized fixture"
            result = matches[0].copy()
        elif suffix.endswith("/modules") and method == "GET":
            assert suffix in {f"projects/{PROJECT}/modules", f"projects/{NEXT_PROJECT}/modules"}
            result = {"modules": []}
        elif (
            suffix in {f"projects/{PROJECT}/runs", f"projects/{NEXT_PROJECT}/runs"}
            and method == "GET"
        ):
            result = {
                "items": [],
                "total": 0,
                "limit": int(query["limit"][0]),
                "offset": int(query["offset"][0]),
            }
        elif suffix == "users/me/project-preference" and method in {"GET", "PUT"}:
            result = {"project_id": self.preference if method == "GET" else body["project_id"]}
        elif suffix == "users/me/ui-language" and method in {"GET", "PUT"}:
            result = {"ui_language": self.language if method == "GET" else body["ui_language"]}
        elif suffix == "users/me/account" and method == "GET":
            result = self.users[self.actor].copy()
        elif suffix == "users" and method == "GET":
            assert self.role == "ADMIN", "USER requested an ADMIN list"
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            q = query.get("q", [""])[0].lower()
            items = [self.users[user_id].copy() for user_id in self.directory]
            items = [
                item for item in items if q in f"{item['email']} {item['display_name']}".lower()
            ]
            result = {
                "items": items[offset : offset + limit],
                "total": len(items),
                "limit": limit,
                "offset": offset,
            }
        elif suffix.endswith("/security-events") and method == "GET":
            target = self.actor if suffix == "users/me/security-events" else suffix.split("/")[1]
            assert target == self.actor or self.role == "ADMIN"
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            items = [
                {
                    "event_id": f"00000000-0000-4000-8000-{index + 1000:012}",
                    "user_id": target,
                    "actor_id": ACTOR,
                    "action": "UPDATED",
                    "row_version": index + 1,
                    "previous_role": "USER",
                    "previous_status": "ACTIVE",
                    "system_role": "USER",
                    "status": "ACTIVE",
                    "revoked_sessions": 0,
                    "request_id": f"00000000-0000-4000-8000-{index + 2000:012}",
                    "created_at": NOW,
                }
                for index in range(30)
            ]
            result = {
                "items": items[offset : offset + limit],
                "total": 30,
                "limit": limit,
                "offset": offset,
            }
        elif suffix.startswith("users/") and method == "GET" and suffix.count("/") == 1:
            assert self.role == "ADMIN"
            result = self.users[suffix.split("/")[1]].copy()
        elif (suffix == "users" and method == "POST") or (
            suffix.startswith("users/") and method in {"POST", "PUT"}
        ):
            await self.mutate(route, suffix, body)
            return
        else:
            self.unexpected.append(f"{method} {request.url}")
            await route.abort()
            return
        gate = self.gates.get((method, suffix))
        if gate:
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 30)
        try:
            if gate and gate.failure:
                status, code = gate.failure
                await route.fulfill(
                    status=status,
                    json={
                        "type": f"https://skillmind.local/problems/{code}",
                        "title": "Late fixture rejection",
                        "status": status,
                        "code": code,
                        "detail": PASSWORD,
                    },
                )
            else:
                await route.fulfill(status=200, headers={"Cache-Control": "no-store"}, json=result)
        finally:
            if gate:
                gate.returned.set()

    async def mutate(self, route: Route, suffix: str, body: dict) -> None:
        """原版を記録し、既知拒否・応答喪失・遅延を case が明示的に選ぶ。"""

        original_actor = self.actor
        hold, drop, failure = self.hold, self.drop, self.failure
        self.received.set()
        if hold:
            await asyncio.wait_for(self.release.wait(), 20)
        try:
            if drop:
                await route.abort("failed")
                return
            if failure:
                status, code = failure
                await route.fulfill(
                    status=status,
                    headers={
                        "Content-Type": "application/problem+json",
                        "Retry-After": str(self.retry_seconds),
                        "Cache-Control": "no-store",
                    },
                    json={
                        "type": f"https://skillmind.local/problems/{code}",
                        "title": "Fixture rejection",
                        "status": status,
                        "code": code,
                        "detail": PASSWORD,
                        "request_id": "00000000-0000-4000-8000-000000009999",
                    },
                )
                return
            if suffix == "users":
                assert self.role == "ADMIN"
                assert set(body) == {"email", "display_name", "system_role", "password"}
                assert body["password"] == PASSWORD
                target = "00000000-0000-4000-8000-000000000900"
                user = account(target)
                user.update({key: body[key] for key in ("email", "display_name", "system_role")})
                user["row_version"] = 1
                self.directory.append(target)
                security_change = False
            else:
                target = original_actor if suffix.startswith("users/me/") else suffix.split("/")[1]
                assert target == original_actor or self.role == "ADMIN"
                user = self.users[target].copy()
                security_change = route.request.method != "PUT" or (
                    body.get("system_role") != user["system_role"]
                    or body.get("status") != user["status"]
                )
                assert body["expected_row_version"] == user["row_version"], "Wrong original version"
                if suffix.endswith("/password"):
                    assert set(body) == {"current_password", "new_password", "expected_row_version"}
                    assert (
                        body["current_password"] == CURRENT_PASSWORD
                        and body["new_password"] == PASSWORD
                    )
                elif route.request.method == "PUT":
                    assert set(body) == {
                        "display_name",
                        "system_role",
                        "status",
                        "expected_row_version",
                    }
                    user.update(
                        {key: body[key] for key in ("display_name", "system_role", "status")}
                    )
                else:
                    assert set(body) == {"expected_row_version"}
                user["row_version"] += 1
            self.users[target] = user
            await route.fulfill(
                status=201 if suffix == "users" else 200,
                headers={"Cache-Control": "no-store"},
                json={
                    "user": user,
                    "revoked_sessions": 2 if security_change else 0,
                    "session_revoked": target == original_actor and security_change,
                },
            )
        except Error:
            # Native abort 済みの場合も遅い callback が発生しないことを別途観測する。
            if not hold:
                raise
        finally:
            self.returned.set()
