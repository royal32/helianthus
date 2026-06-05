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
    ("adguardhome", "53", "tcp"),
    ("adguardhome", "53", "udp"),
    ("adguardhome", "68", "tcp"),
    ("adguardhome", "68", "udp"),
    ("adguardhome", "853", "tcp"),
    ("jellyfin", "1900", "udp"),
    ("jellyfin", "7359", "udp"),
    ("mosquitto", "1883", "tcp"),
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


def main() -> int:
    env = {**load_env(), **os.environ}
    config = compose_config()
    services = config.get("services", {})
    errors: list[str] = []
    warnings: list[str] = []
    proxy_names: dict[str, str] = {}

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

    dashboard_list_path = ROOT_DIR / "tsdproxy" / "dashboard.json"
    try:
        dashboard_routes = json.loads(dashboard_list_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot load TSDProxy dashboard route list: {exc}")
        dashboard_routes = {}

    for proxy_name, route in dashboard_routes.items():
        previous = proxy_names.get(proxy_name)
        if previous:
            errors.append(f"duplicate TSDProxy name {proxy_name}: {previous}, dashboard list")
        proxy_names[proxy_name] = "dashboard list"
        targets = route.get("ports", {}).get("443/https", {}).get("targets", [])
        if targets != ["http://127.0.0.1:8080"]:
            errors.append("TSDProxy dashboard list route must target http://127.0.0.1:8080")

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
            if (service_name, published, protocol) not in ALLOWED_PUBLISHED_PORTS:
                errors.append(
                    f"{service_name} publishes unexpected host port {published}/{protocol}; "
                    "web applications must use TSDProxy"
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
