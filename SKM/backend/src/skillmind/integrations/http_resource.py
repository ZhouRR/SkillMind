"""汎用 HTTP 接続の宛先・認証・操作範囲を一箇所で検証する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

READ_METHODS = frozenset({"GET", "HEAD"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HEADERS = frozenset({"accept", "content-type", "if-match", "if-none-match"})


def validate_path(value: Any) -> str:
    """二重 decode、authority 切替、dot segment を許さない API 相対 path。"""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 2048
        or "//" in value
        or any(c in value for c in "%\\?#")
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("HTTP path must be an unencoded absolute API path")
    return value


def normalize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """認証値は SecretReference に置き、接続設定には方式と header 名だけを保存する。"""
    if set(config) - {"base_url", "auth_mode", "credential_header"}:
        raise ValueError("HTTP config contains unknown fields")
    base = config.get("base_url")
    if not isinstance(base, str) or len(base) > 2048 or any(ord(c) < 33 for c in base):
        raise ValueError("HTTP base URL is invalid")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any((parsed.username, parsed.password, parsed.query, parsed.fragment))
    ):
        raise ValueError("HTTP base URL is invalid")
    _ = parsed.port
    validate_path(parsed.path or "/")
    mode = config.get("auth_mode", "bearer")
    if mode not in {"none", "bearer", "header"}:
        raise ValueError("HTTP authentication mode is invalid")
    result = {"base_url": base.rstrip("/"), "auth_mode": mode}
    if mode == "header":
        header = config.get("credential_header")
        if (
            not isinstance(header, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header) is None
            or header.lower()
            in _HEADERS
            | {
                "host",
                "cookie",
                "content-length",
                "transfer-encoding",
                "connection",
                "proxy-authorization",
            }
        ):
            raise ValueError("HTTP credential header is invalid")
        result["credential_header"] = header
    elif "credential_header" in config:
        raise ValueError("Credential header requires header authentication")
    return result


def normalize_scope(scope: Mapping[str, Any], *, write_enabled: bool) -> dict[str, Any]:
    """管理者が明示した path prefix と HTTP method のみを許可する。"""
    if set(scope) != {"paths", "methods"}:
        raise ValueError("HTTP scope requires paths and methods")
    paths, methods = scope["paths"], scope["methods"]
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 100
        or not isinstance(methods, list)
        or not methods
        or any(not isinstance(method, str) for method in methods)
    ):
        raise ValueError("HTTP scope is invalid")
    permitted = READ_METHODS | WRITE_METHODS if write_enabled else READ_METHODS
    if not set(methods) <= permitted:
        raise ValueError("HTTP method is not allowed by the access level")
    return {
        "paths": sorted({validate_path(path) for path in paths}),
        "methods": sorted(set(methods)),
    }


def validate_request(
    config: Mapping[str, Any], scope: Mapping[str, Any], request: Mapping[str, Any], *, write: bool
) -> dict[str, Any]:
    """URL や認証 header を要求に持たせず、原生 body/query の意味を保存する。"""
    if set(request) - {"method", "path", "query", "headers", "body"}:
        raise ValueError("HTTP request contains unsupported fields")
    path = validate_path(request.get("path"))
    method = request.get("method", "GET")
    if not isinstance(method, str) or method not in (WRITE_METHODS if write else READ_METHODS):
        raise ValueError("HTTP method requires the matching read or approved write capability")
    if method not in scope.get("methods", []) or not any(
        path == prefix or path.startswith(prefix.rstrip("/") + "/")
        for prefix in scope.get("paths", [])
    ):
        raise ValueError("HTTP request exceeds the frozen scope")
    headers = request.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(key, str)
        or key.lower() not in _HEADERS
        or not isinstance(value, str)
        or len(value) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        for key, value in headers.items()
    ):
        raise ValueError("HTTP request headers are invalid")
    query = request.get("query", {})
    if (
        not isinstance(query, dict)
        or len(query) > 100
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) > 256
            or len(value) > 4096
            for key, value in query.items()
        )
    ):
        raise ValueError("HTTP query is invalid")
    if not write and "body" in request:
        raise ValueError("HTTP reads do not accept a request body")
    # base path を保持する。urljoin は先頭 / で base path を落とすため使わない。
    return {
        "method": method,
        "url": config["base_url"] + quote(path, safe="/-._~"),
        "path": path,
        "query": dict(query),
        "headers": dict(headers),
        **({"body": request["body"]} if "body" in request else {}),
    }
