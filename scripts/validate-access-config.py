#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
TAILNET_PATTERN = re.compile(r"^[a-z0-9-]+\.ts\.net$")
ALLOWED_PUBLISHED_PORTS = {
    ("jellyfin", "1900", "udp"),
    ("jellyfin", "7359", "udp"),
}


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def compose_config() -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def env_bool(env: dict[str, str], key: str, default: bool) -> bool | None:
    value = env.get(key, "")
    if not value:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def main() -> int:
    env = {**load_env(), **os.environ}
    config = compose_config()
    services = config.get("services", {})
    errors: list[str] = []
    warnings: list[str] = []
    proxy_names: dict[str, str] = {}
    disable_tls = env.get("TSDPROXY_DISABLE_TLS", "false").lower()
    local_network_http_access = env_bool(env, "LOCAL_NETWORK_HTTP_ACCESS", True)
    allowed_published_ports = set(ALLOWED_PUBLISHED_PORTS)
    local_network_ports = {
        ("jellyfin", env.get("JELLYFIN_LOCAL_NETWORK_PORT", "8096"), "tcp"),
        ("seerr", env.get("SEERR_LOCAL_NETWORK_PORT", "5055"), "tcp"),
    }
    seen_published_ports: set[tuple[str, str, str]] = set()

    if local_network_http_access is None:
        errors.append("LOCAL_NETWORK_HTTP_ACCESS must be true or false")
    elif local_network_http_access:
        allowed_published_ports.update(local_network_ports)

    if disable_tls in {"1", "true", "yes", "on"}:
        expected_access_mode = "80/http"
        expected_url_scheme = "http"
    elif disable_tls in {"0", "false", "no", "off", ""}:
        expected_access_mode = "443/https"
        expected_url_scheme = "https"
    else:
        errors.append("TSDPROXY_DISABLE_TLS must be true or false")
        expected_access_mode = env.get("TSDPROXY_ACCESS_MODE", "")
        expected_url_scheme = env.get("TSDPROXY_URL_SCHEME", "")

    if env.get("TSDPROXY_ACCESS_MODE", expected_access_mode) != expected_access_mode:
        errors.append("TSDPROXY_ACCESS_MODE does not match TSDPROXY_DISABLE_TLS; run setup-stack.sh")
    if env.get("TSDPROXY_URL_SCHEME", expected_url_scheme) != expected_url_scheme:
        errors.append("TSDPROXY_URL_SCHEME does not match TSDPROXY_DISABLE_TLS; run setup-stack.sh")

    for forbidden in ("traefik", "mdns-publisher", "traefik-certs-dumper"):
        if forbidden in services:
            errors.append(f"obsolete access service is still configured: {forbidden}")

    for service_name, service in services.items():
        labels = service.get("labels") or {}
        for label_name in labels:
            if label_name.startswith("traefik."):
                errors.append(f"{service_name} still has obsolete label {label_name}")

        if str(labels.get("tsdproxy.enable", "")).lower() != "true":
            continue
        proxy_name = labels.get("tsdproxy.name") or service_name
        previous = proxy_names.get(proxy_name)
        if previous:
            errors.append(f"duplicate TSDProxy name {proxy_name}: {previous}, {service_name}")
        proxy_names[proxy_name] = service_name

        port_labels = [name for name in labels if name.startswith("tsdproxy.port.")]
        if not port_labels:
            errors.append(f"{service_name} has no explicit tsdproxy.port.N label")
        elif not any(str(labels[name]).startswith(f"{expected_access_mode}:") for name in port_labels):
            errors.append(
                f"{service_name} does not use {expected_access_mode}; "
                "run setup-stack.sh after changing TSDPROXY_DISABLE_TLS"
            )

    for service_name, service in services.items():
        for port in service.get("ports") or []:
            published = str(port.get("published", ""))
            host_ip = str(port.get("host_ip", ""))
            protocol = port.get("protocol", "tcp")
            if protocol == "tcp" and published in {"80", "443"}:
                errors.append(f"{service_name} publishes reserved proxy port {published}")
            if service_name == "tsdproxy":
                if host_ip not in {"127.0.0.1", "::1"}:
                    errors.append("TSDProxy dashboard must only bind to localhost")
                continue
            published_port = (service_name, published, protocol)
            seen_published_ports.add(published_port)
            if published_port not in allowed_published_ports:
                errors.append(
                    f"{service_name} publishes unexpected host port {published}/{protocol}; "
                    "web applications must use TSDProxy or LOCAL_NETWORK_HTTP_ACCESS"
                )

    if local_network_http_access:
        missing_ports = sorted(local_network_ports - seen_published_ports)
        for service_name, published, protocol in missing_ports:
            errors.append(
                f"{service_name} is missing local-network host port {published}/{protocol}; "
                "run setup-stack.sh after changing LOCAL_NETWORK_HTTP_ACCESS"
            )

    tailnet_domain = env.get("TAILNET_DOMAIN", "")
    if not tailnet_domain:
        warnings.append("TAILNET_DOMAIN is blank; external application URLs cannot be finalized")
    elif not TAILNET_PATTERN.fullmatch(tailnet_domain):
        errors.append("TAILNET_DOMAIN must look like example-tailnet.ts.net")

    for required_proxy in (
        "tsdproxy-dashboard",
        "homepage",
        "sonarr",
        "radarr",
        "seerr",
        "prowlarr",
        "qbittorrent",
        "jellyfin",
    ):
        if required_proxy not in proxy_names:
            errors.append(f"required TSDProxy route is missing: {required_proxy}")

    for warning in warnings:
        print(f"[access-validation] warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"[access-validation] error: {error}", file=sys.stderr)

    if errors:
        return 1

    print(f"[access-validation] validated {len(proxy_names)} unique TSDProxy routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
