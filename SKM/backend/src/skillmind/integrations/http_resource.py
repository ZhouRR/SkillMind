"""汎用 HTTP 接続の宛先・認証・操作範囲を一箇所で検証する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

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
    if set(config) - {"auth_mode", "credential_header"}:
        raise ValueError("HTTP config contains unknown fields")
    mode = config.get("auth_mode", "bearer")
    if mode not in {"none", "bearer", "header"}:
        raise ValueError("HTTP authentication mode is invalid")
    result = {"auth_mode": mode}
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


def normalize_url(value: Any) -> str:
    """根 URL と要求 URL の origin/path を同じ規則で正準化する。"""
    if not isinstance(value, str) or len(value) > 2048 or any(
        ord(c) < 33 or ord(c) == 127 for c in value
    ):
        raise ValueError("HTTP URL is invalid")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
        or any((parsed.username, parsed.password, parsed.query, parsed.fragment))
        or "\\" in parsed.netloc):
        raise ValueError("HTTP URL is invalid")
    validate_path(parsed.path or "/")
    host = parsed.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if parsed.port is not None and parsed.port != (443 if parsed.scheme == "https" else 80):
        authority += f":{parsed.port}"
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def normalize_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    """登録するのは根 URL 一覧だけ。HTTP 方法は接続の読取/書込権で決まる。"""
    if set(scope) != {"base_urls"}:
        raise ValueError("HTTP scope requires only base_urls")
    urls = scope["base_urls"]
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
        raise ValueError("HTTP base URL list is invalid")
    return {"base_urls": list(dict.fromkeys(normalize_url(url).rstrip("/") for url in urls))}


def validate_request(
    config: Mapping[str, Any], scope: Mapping[str, Any], request: Mapping[str, Any], *, write: bool
) -> dict[str, Any]:
    """Skill の相対 path または完全 URL を登録根配下で解決し、資格を他へ送らない。"""
    if set(request) - {"method", "path", "base_url", "url", "query", "headers", "body"}:
        raise ValueError("HTTP request contains unsupported fields")
    roots = normalize_scope(scope)["base_urls"]
    if "url" in request:
        if "path" in request or "base_url" in request:
            raise ValueError("HTTP URL and relative path cannot be mixed")
        url = normalize_url(request["url"])
    else:
        path = validate_path(request.get("path"))
        base = request.get("base_url")
        if base is None:
            if len(roots) != 1:
                raise ValueError("Choose a registered base_url when multiple roots exist")
            base = roots[0]
        else:
            base = normalize_url(base).rstrip("/")
        if base not in roots:
            raise ValueError("HTTP base URL is not registered")
        url = base + path
    parsed = urlsplit(url)
    if not any(
        parsed.scheme == urlsplit(root).scheme and parsed.netloc == urlsplit(root).netloc
        and (parsed.path == urlsplit(root).path
             or parsed.path.startswith(urlsplit(root).path + "/"))
        for root in roots
    ):
        raise ValueError("HTTP request exceeds the registered base URLs")
    method = request.get("method", "GET")
    if not isinstance(method, str) or method not in (WRITE_METHODS if write else READ_METHODS):
        raise ValueError("HTTP method requires the matching read or approved write capability")
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
    # 根の path を保持する。urljoin の先頭 / による根 path 消失を避ける。
    return {
        "method": method,
        "url": urlunsplit((parsed.scheme, parsed.netloc, quote(parsed.path, safe="/-._~"), "", "")),
        "query": dict(query),
        "headers": dict(headers),
        **({"body": request["body"]} if "body" in request else {}),
    }
