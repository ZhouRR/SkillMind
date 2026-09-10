"""Compose が既存 Traefik と内部 network の境界を守るか検証する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"


def as_mapping(value: object, *, name: str) -> dict[str, Any]:
    """YAML node が mapping であることを検証して型を絞り込む。"""

    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return value


def validate() -> None:
    """公開 service、route priority、host port の不変条件を検証する。"""

    document = as_mapping(yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8")), name="compose")
    services = as_mapping(document.get("services"), name="services")
    networks = as_mapping(document.get("networks"), name="networks")

    if "traefik" in services:
        raise ValueError("Skillmind must use the existing Traefik, not define one")

    edge = as_mapping(networks.get("edge"), name="networks.edge")
    if edge.get("external") is not True:
        raise ValueError("networks.edge must remain external")

    # Network 接続と Traefik label を別々に集計し、片側だけの公開漏れも検出する。
    edge_services: set[str] = set()
    routed_services: set[str] = set()
    for service_name, raw_service in services.items():
        service = as_mapping(raw_service, name=f"services.{service_name}")
        if "ports" in service:
            raise ValueError(f"services.{service_name} must not publish host ports")

        attached_networks = service.get("networks", [])
        if isinstance(attached_networks, dict | list):
            attached_network_names = set(attached_networks)
        else:
            raise TypeError(f"services.{service_name}.networks must be a list or mapping")
        if "edge" in attached_network_names:
            edge_services.add(service_name)

        labels = service.get("labels", {})
        if isinstance(labels, dict) and labels.get("traefik.enable") == "true":
            routed_services.add(service_name)

    expected_public_services = {"api", "web"}
    if edge_services != expected_public_services:
        raise ValueError(f"Only api and web may join edge; found {sorted(edge_services)}")
    if routed_services != expected_public_services:
        raise ValueError(f"Only api and web may enable Traefik; found {sorted(routed_services)}")

    # Log と障害調査時の表示を揃えるため、one-shot を含む全 service へ同じ timezone を渡す。
    timezone_expression = "${SKILLMIND_TIMEZONE:-Asia/Tokyo}"
    for service_name, raw_service in services.items():
        service = as_mapping(raw_service, name=f"services.{service_name}")
        environment = as_mapping(
            service.get("environment"), name=f"services.{service_name}.environment"
        )
        if environment.get("TZ") != timezone_expression:
            raise ValueError(f"services.{service_name} must use the shared Skillmind timezone")

    # 同一 tag の並列 export を防ぐため、Backend image の build 所有者は API 一つに限定する。
    backend_service_names = {"api", "worker", "migrate"}
    backend_images = {services[service_name].get("image") for service_name in backend_service_names}
    if backend_images != {"${SKM_BACKEND_IMAGE:-skillmind/backend:0.1.0}"}:
        raise ValueError("API, Worker, and Migrate must share one Backend image")
    if services["web"].get("image") != "${SKM_WEB_IMAGE:-skillmind/web:0.1.0}":
        raise ValueError("Web must support the same explicit immutable-image selection")
    # CLI 補間と container 注入の path は共用 runner が同じ絶対値に固定する。
    source = (
        "${SKM_COMPOSE_ENV_FILE:?select the environment file through the deployment entrypoint}"
    )
    for service_name in backend_service_names:
        if services[service_name].get("env_file") != [source]:
            raise ValueError("API, Worker, and Migrate must share the selected environment file")
    backend_builders = {
        service_name for service_name in backend_service_names if "build" in services[service_name]
    }
    if backend_builders != {"api"}:
        raise ValueError("Only API may build the shared Backend image")

    # 空の object-storage volume でも upload を受け付けられるよう、API/Worker の起動前に
    # bucket を idempotent に作成する one-shot service を必須にする。
    object_storage_init = as_mapping(
        services.get("object-storage-init"), name="services.object-storage-init"
    )
    object_storage_init_depends_on = as_mapping(
        object_storage_init.get("depends_on"), name="services.object-storage-init.depends_on"
    )
    object_storage_dependency = as_mapping(
        object_storage_init_depends_on.get("object-storage"),
        name="services.object-storage-init.depends_on.object-storage",
    )
    if object_storage_dependency.get("condition") != "service_started":
        raise ValueError("Object-storage initializer must wait for object-storage startup")
    for service_name in ("api", "worker"):
        service_depends_on = as_mapping(
            services[service_name].get("depends_on"), name=f"services.{service_name}.depends_on"
        )
        initializer_dependency = as_mapping(
            service_depends_on.get("object-storage-init"),
            name=f"services.{service_name}.depends_on.object-storage-init",
        )
        if initializer_dependency.get("condition") != "service_completed_successfully":
            raise ValueError(
                f"{service_name} must wait for successful object-storage initialization"
            )

    # 同一 Host と context path では API route が Web route より必ず優先されなければならない。
    api_labels = as_mapping(services["api"].get("labels"), name="services.api.labels")
    web_labels = as_mapping(services["web"].get("labels"), name="services.web.labels")
    context_expression = "${SKILLMIND_CONTEXT_PATH:-/skillmind}"
    api_rule = str(api_labels.get("traefik.http.routers.skillmind-api.rule"))
    web_rule = str(web_labels.get("traefik.http.routers.skillmind-web.rule"))
    if f"PathPrefix(`{context_expression}/api/`)" not in api_rule:
        raise ValueError("API router must own the context-path /api route")
    if f"PathPrefix(`{context_expression}/`)" not in web_rule:
        raise ValueError("Web router must remain inside the configured context path")
    api_strip_prefix = api_labels.get(
        "traefik.http.middlewares.skillmind-api-context.stripprefix.prefixes"
    )
    web_strip_prefix = web_labels.get(
        "traefik.http.middlewares.skillmind-web-context.stripprefix.prefixes"
    )
    if api_strip_prefix != context_expression:
        raise ValueError("API router must strip the external context path")
    if web_strip_prefix != context_expression:
        raise ValueError("Web router must strip the external context path")
    backend_environment = as_mapping(
        services["api"].get("environment"), name="services.api.environment"
    )
    if backend_environment.get("SKILLMIND_CONTEXT_PATH") != context_expression:
        raise ValueError("API must receive the same context path used by Traefik")
    web_build = as_mapping(services["web"].get("build"), name="services.web.build")
    web_build_args = as_mapping(web_build.get("args"), name="services.web.build.args")
    if web_build_args.get("SKILLMIND_CONTEXT_PATH") != context_expression:
        raise ValueError("Web build must receive the same context path used by Traefik")
    tls_expression = "${TRAEFIK_TLS:-true}"
    if api_labels.get("traefik.http.routers.skillmind-api.tls") != tls_expression:
        raise ValueError("API router TLS mode must remain environment-configurable")
    if web_labels.get("traefik.http.routers.skillmind-web.tls") != tls_expression:
        raise ValueError("Web router TLS mode must remain environment-configurable")
    if api_labels.get("traefik.http.routers.skillmind-api.priority") != "100":
        raise ValueError("API router priority must remain above the Web fallback")
    if web_labels.get("traefik.http.routers.skillmind-web.priority") != "10":
        raise ValueError("Web router must remain the lower-priority host fallback")

    print(f"Validated {len(services)} Compose services and existing-Traefik ingress invariants.")


if __name__ == "__main__":
    validate()
