#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import sqlite3
import ssl
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, parse, request


ROOT_DIR = Path(__file__).resolve().parent.parent
PROWLARR_CONFIG_PATH = ROOT_DIR / "config" / "prowlarr.json"
SSL_CONTEXT = ssl._create_unverified_context()
ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(:-([^}]*))?\}")
SEERR_MEDIA_SERVER_TYPE_JELLYFIN = 2


@dataclass(frozen=True)
class ArrService:
    service_name: str
    display_name: str
    url_base: str
    api_key_env: str
    root_folder_env: str
    category_env: str
    download_path_env: str
    qbit_implementation_field: str
    qbit_directory_field: str | None
    prowlarr_implementation: str
    internal_base_url: str
    api_version: str


ARR_SERVICES: tuple[ArrService, ...] = (
    ArrService(
        service_name="sonarr",
        display_name="Sonarr",
        url_base="",
        api_key_env="SONARR_API_KEY",
        root_folder_env="SONARR_ROOT_FOLDER",
        category_env="SONARR_QBIT_CATEGORY",
        download_path_env="SONARR_DOWNLOAD_PATH",
        qbit_implementation_field="tvCategory",
        qbit_directory_field=None,
        prowlarr_implementation="Sonarr",
        internal_base_url="http://sonarr:8989",
        api_version="v3",
    ),
    ArrService(
        service_name="radarr",
        display_name="Radarr",
        url_base="",
        api_key_env="RADARR_API_KEY",
        root_folder_env="RADARR_ROOT_FOLDER",
        category_env="RADARR_QBIT_CATEGORY",
        download_path_env="RADARR_DOWNLOAD_PATH",
        qbit_implementation_field="movieCategory",
        qbit_directory_field=None,
        prowlarr_implementation="Radarr",
        internal_base_url="http://radarr:7878",
        api_version="v3",
    ),
)


def log(message: str) -> None:
    print(f"[connections] {message}")


def parse_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values[key] = value

    return values


def resolve_env_values(values: dict[str, str]) -> dict[str, str]:
    def expand(value: str, depth: int = 0) -> str:
        if depth > 10:
            return value

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            default = match.group(3) or ""
            replacement = values.get(key, default)
            return expand(replacement, depth + 1)

        return ENV_VAR_PATTERN.sub(replace, value)

    return {key: expand(value) for key, value in values.items()}


def apply_blank_aware_defaults(env: dict[str, str]) -> dict[str, str]:
    env = dict(env)
    admin_username = env.get("ADMIN_USERNAME") or "admin"
    global_password = env.get("GLOBAL_PASSWORD") or "adminadmin"

    for key in (
        "QBITTORRENT_USERNAME",
        "SONARR_USERNAME",
        "RADARR_USERNAME",
        "PROWLARR_USERNAME",
    ):
        if not env.get(key):
            env[key] = admin_username

    for key in (
        "QBITTORRENT_PASSWORD",
        "SONARR_PASSWORD",
        "RADARR_PASSWORD",
        "PROWLARR_PASSWORD",
    ):
        if not env.get(key):
            env[key] = global_password

    if not env.get("JELLYFIN_ADMIN_USERNAME"):
        env["JELLYFIN_ADMIN_USERNAME"] = admin_username
    if not env.get("JELLYFIN_ADMIN_PASSWORD"):
        env["JELLYFIN_ADMIN_PASSWORD"] = global_password
    if not env.get("SEERR_JELLYFIN_ADMIN_USERNAME"):
        env["SEERR_JELLYFIN_ADMIN_USERNAME"] = env["JELLYFIN_ADMIN_USERNAME"]
    if not env.get("SEERR_JELLYFIN_ADMIN_PASSWORD"):
        env["SEERR_JELLYFIN_ADMIN_PASSWORD"] = env["JELLYFIN_ADMIN_PASSWORD"]
    if not env.get("SEERR_JELLYFIN_ADMIN_EMAIL"):
        env["SEERR_JELLYFIN_ADMIN_EMAIL"] = env["SEERR_JELLYFIN_ADMIN_USERNAME"]
    if not env.get("JELLYFIN_SERVER_NAME"):
        env["JELLYFIN_SERVER_NAME"] = "Jellyfin"

    return env


def get_config_root(env: dict[str, str]) -> Path:
    config_root = env.get("CONFIG_ROOT") or "./runtime"
    path = Path(config_root)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def update_env_file_value(env_path: Path, key: str, value: str, dry_run: bool) -> bool:
    lines = env_path.read_text().splitlines()
    replacement = f"{key}={value}"

    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            if line == replacement:
                return False
            if dry_run:
                log(f"[dry-run] Would update {env_path.name}: {key}")
                return True
            lines[index] = replacement
            env_path.write_text("\n".join(lines) + "\n")
            return True

    if dry_run:
        log(f"[dry-run] Would append {env_path.name}: {key}")
        return True

    lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n")
    return True


def read_xml_text(path: Path, tag_name: str) -> str:
    if not path.exists():
        return ""

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return ""

    item = root.find(tag_name)
    if item is None or item.text is None:
        return ""
    return item.text.strip()


def read_bazarr_api_key(path: Path) -> str:
    if not path.exists():
        return ""

    in_auth_section = False
    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_auth_section = stripped == "auth:"
            continue
        if in_auth_section and stripped.startswith("apikey:"):
            return stripped.split(":", 1)[1].strip().strip("'\"")
    return ""


def sync_generated_api_keys(env: dict[str, str], dry_run: bool) -> bool:
    env_path = ROOT_DIR / ".env"
    config_root = get_config_root(env)
    discovered = {
        "SONARR_API_KEY": read_xml_text(config_root / "sonarr" / "config.xml", "ApiKey"),
        "RADARR_API_KEY": read_xml_text(config_root / "radarr" / "config.xml", "ApiKey"),
        "PROWLARR_API_KEY": read_xml_text(config_root / "prowlarr" / "config.xml", "ApiKey"),
        "BAZARR_API_KEY": read_bazarr_api_key(config_root / "bazarr" / "config" / "config" / "config.yaml"),
    }

    seerr_settings_path = config_root / "seerr" / "settings.json"
    if seerr_settings_path.exists():
        try:
            seerr_settings = json.loads(seerr_settings_path.read_text())
            discovered["SEERR_API_KEY"] = seerr_settings.get("main", {}).get("apiKey", "")
            discovered["JELLYFIN_API_KEY"] = seerr_settings.get("jellyfin", {}).get("apiKey", "")
        except json.JSONDecodeError:
            pass

    changed = False
    for key, value in discovered.items():
        if not value:
            continue
        if env.get(key) == value:
            continue
        changed = update_env_file_value(env_path, key, value, dry_run) or changed
        env[key] = value

    if changed:
        log("Synced generated API keys into .env")
    return changed


def reapply_homepage_label_services(running_services: set[str], dry_run: bool) -> None:
    # Do not run `docker compose up` from inside stack-setup. Relative bind
    # mounts are resolved from the setup container's /stack directory, which can
    # recreate app containers against the wrong config tree.
    if running_services:
        log("Skipped Homepage label reapply; generated API keys were synced without recreating services")


def homepage_service_yaml(service: dict[str, Any]) -> str:
    lines = [
        f"    - {service['name']}:",
        f"        icon: {service['icon']}",
        f"        href: {service['href']}",
        f"        description: {service['description']}",
    ]
    widget = service.get("widget")
    if widget:
        lines.extend(
            [
                "        widget:",
                f"          type: {widget['type']}",
                f"          url: {widget['url']}",
            ]
        )
        for key in ("key", "username", "password"):
            if widget.get(key):
                lines.append(f"          {key}: {widget[key]}")
    return "\n".join(lines)


def write_homepage_services(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "homepage" not in running_services:
        return

    services = [
        {
            "service": "sonarr",
            "group": "Media",
            "name": "Sonarr",
            "icon": "sonarr.png",
            "href": build_external_url(env, "sonarr") or "/",
            "description": "Series management",
            "widget": {
                "type": "sonarr",
                "url": "http://sonarr:8989",
                "key": env.get("SONARR_API_KEY", ""),
            },
        },
        {
            "service": "radarr",
            "group": "Media",
            "name": "Radarr",
            "icon": "radarr.png",
            "href": build_external_url(env, "radarr") or "/",
            "description": "Movies management",
            "widget": {
                "type": "radarr",
                "url": "http://radarr:7878",
                "key": env.get("RADARR_API_KEY", ""),
            },
        },
        {
            "service": "seerr",
            "group": "Media",
            "name": "Seerr",
            "icon": "jellyseerr.png",
            "href": build_external_url(env, "seerr") or "/",
            "description": "Content requests",
            "widget": {
                "type": "jellyseerr",
                "url": "http://seerr:5055",
                "key": env.get("SEERR_API_KEY", ""),
            },
        },
        {
            "service": "jellyfin",
            "group": "Media",
            "name": "Jellyfin",
            "icon": "jellyfin.png",
            "href": build_external_url(env, "jellyfin") or "/",
            "description": "Media server",
            "widget": {
                "type": "jellyfin",
                "url": "http://jellyfin:8096",
                "key": env.get("JELLYFIN_API_KEY", ""),
            },
        },
        {
            "service": "prowlarr",
            "group": "Download",
            "name": "Prowlarr",
            "icon": "prowlarr.png",
            "href": build_external_url(env, "prowlarr") or "/",
            "description": "Indexer management",
            "widget": {
                "type": "prowlarr",
                "url": "http://prowlarr:9696",
                "key": env.get("PROWLARR_API_KEY", ""),
            },
        },
        {
            "service": "profilarr",
            "group": "Media",
            "name": "Profilarr",
            "icon": "profilarr.png",
            "href": build_external_url(env, "profilarr") or "/",
            "description": "Arr profile management",
        },
        {
            "service": "qbittorrent",
            "group": "Download",
            "name": "qBittorrent",
            "icon": "qbittorrent.png",
            "href": build_external_url(env, "qbittorrent") or "/",
            "description": "BitTorrent client",
            "widget": {
                "type": "qbittorrent",
                "url": "http://vpn:8080",
                "username": env.get("QBITTORRENT_USERNAME", ""),
                "password": env.get("QBITTORRENT_PASSWORD", ""),
            },
        },
        {
            "service": "bazarr",
            "group": "Download",
            "name": "Bazarr",
            "icon": "bazarr.png",
            "href": build_external_url(env, "bazarr") or "/",
            "description": "Subtitle management",
            "widget": {
                "type": "bazarr",
                "url": "http://bazarr:6767",
                "key": env.get("BAZARR_API_KEY", ""),
            },
        },
        {
            "service": "suggestarr",
            "group": "Media",
            "name": "Suggestarr",
            "icon": "suggest-arr.png",
            "href": build_external_url(env, "suggestarr") or "/",
            "description": "Media recommendations",
        },
        {
            "service": "autobrr",
            "group": "Download",
            "name": "Autobrr",
            "icon": "autobrr.png",
            "href": build_external_url(env, "autobrr") or "/",
            "description": "Torrent download automation",
            "widget": {
                "type": "autobrr",
                "url": "http://autobrr:7474",
                "key": env.get("AUTOBRR_API_KEY", ""),
            },
        },
        {
            "service": "qui",
            "group": "Download",
            "name": "qui",
            "icon": "qui.png",
            "href": build_external_url(env, "qui") or "/",
            "description": "qBittorrent web interface",
        },
        {
            "service": "cleanuparr",
            "group": "Download",
            "name": "Cleanuparr",
            "icon": "cleanuperr.png",
            "href": build_external_url(env, "cleanuparr") or "/",
            "description": "Download cleanup",
        },
        {
            "service": "sabnzbd",
            "group": "Download",
            "name": "SABnzbd",
            "icon": "sabnzbd.png",
            "href": build_external_url(env, "sabnzbd") or "/",
            "description": "Usenet downloads",
            "widget": {
                "type": "sabnzbd",
                "url": "http://sabnzbd:8080",
                "key": env.get("SABNZBD_API_KEY", ""),
            },
        },
        {
            "service": "vaultwarden",
            "group": "Utilities",
            "name": "Vaultwarden",
            "icon": "vaultwarden.png",
            "href": build_external_url(env, "vaultwarden") or "/",
            "description": "Password manager",
        },
    ]
    services = [service for service in services if service["service"] in running_services]
    if not services:
        return

    grouped_services: dict[str, list[dict[str, Any]]] = {}
    for service in services:
        grouped_services.setdefault(service["group"], []).append(service)

    homepage_dir = get_config_root(env) / "homepage"
    services_path = homepage_dir / "services.yaml"
    docker_path = homepage_dir / "docker.yaml"
    content_lines = ["---"]
    for group, group_services in grouped_services.items():
        content_lines.append(f"- {group}:")
        content_lines.extend(homepage_service_yaml(service) for service in group_services)
    content = "\n".join(content_lines) + "\n"
    docker_content = "---\n# Service discovery is generated in services.yaml after first-run API keys exist.\n"

    if dry_run:
        log(f"[dry-run] Would write Homepage services to {services_path}")
        return

    homepage_dir.mkdir(parents=True, exist_ok=True)
    services_path.write_text(content)
    docker_path.write_text(docker_content)
    run_compose(["restart", "homepage"], check=False)
    log("Wrote generated Homepage services and restarted Homepage")


def next_settings_id(items: list[dict[str, Any]]) -> int:
    return max((int(item.get("id", -1)) for item in items), default=-1) + 1


def build_external_url(env: dict[str, str], service_name: str) -> str:
    tailnet_domain = env.get("TAILNET_DOMAIN", "")
    if not tailnet_domain or "${" in tailnet_domain:
        return ""
    scheme = env.get("TSDPROXY_URL_SCHEME", "https")
    return f"{scheme}://{service_name}.{tailnet_domain}"


def run_compose(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT_DIR,
        check=check,
        capture_output=True,
        text=True,
    )


def compose_running_services() -> set[str]:
    result = run_compose(["ps", "--services", "--status", "running"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def profile_enabled(env: dict[str, str], profile: str) -> bool:
    profiles = {item.strip() for item in env.get("COMPOSE_PROFILES", "").split(",")}
    return profile in profiles


def exec_in_service(service: str, command: str, dry_run: bool) -> None:
    if dry_run:
        log(f"[dry-run] docker compose exec -T {service} sh -lc {command}")
        return

    run_compose(["exec", "-T", service, "sh", "-lc", command])


class JsonClient:
    def __init__(self, base_url: str, default_headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        form_data: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        request_headers = dict(self.default_headers)
        data: bytes | None = None

        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        elif form_data is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = parse.urlencode(form_data).encode("utf-8")

        http_request = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )

        try:
            with request.urlopen(http_request, context=SSL_CONTEXT) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {body}") from exc

        if not expect_json:
            return body

        if not body.strip():
            return None

        return json.loads(body)


class ContainerJsonClient:
    def __init__(
        self,
        service: str,
        base_url: str,
        default_headers: dict[str, str] | None = None,
        cookie_file: str | None = None,
    ) -> None:
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}
        self.cookie_file = cookie_file

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        form_data: dict[str, str] | None = None,
        expect_json: bool = True,
        accepted_statuses: set[int] | None = None,
    ) -> Any:
        command = [
            "exec",
            "-T",
            self.service,
            "curl",
            "-sS",
        ]
        if self.cookie_file:
            command.extend(["-b", self.cookie_file, "-c", self.cookie_file])
        command.extend([
            "-o",
            "-",
            "-w",
            "\n__STATUS__:%{http_code}",
            "-X",
            method,
        ])

        for header_name, header_value in self.default_headers.items():
            command.extend(["-H", f"{header_name}: {header_value}"])

        if payload is not None:
            command.extend(["-H", "Content-Type: application/json", "--data", json.dumps(payload)])
        elif form_data is not None:
            command.extend(["-H", "Content-Type: application/x-www-form-urlencoded", "--data", parse.urlencode(form_data)])

        command.append(f"{self.base_url}{path}")

        last_error = ""
        for attempt in range(20):
            result = run_compose(command, check=False)
            output = result.stdout
            body, _, status_line = output.rpartition("\n__STATUS__:")
            status_code = status_line.strip()

            accepted = accepted_statuses or set(range(200, 300))
            if result.returncode == 0 and status_code.isdigit() and int(status_code) in accepted:
                if not expect_json:
                    return body
                if not body.strip():
                    return None
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    last_error = f"HTTP {status_code} returned non-JSON response"
                    if attempt < 19:
                        time.sleep(2)
                        continue
                    raise RuntimeError(f"{method} {path} failed: {last_error}")

            last_error = result.stderr.strip() or body or f"HTTP {status_code or 'unknown'}"
            if status_code in {"000", "502", "503", "504"} and attempt < 19:
                time.sleep(2)
                continue

            raise RuntimeError(f"{method} {path} failed: {last_error}")

        raise RuntimeError(f"{method} {path} failed: {last_error}")


class QBittorrentClient(JsonClient):
    def __init__(self, username: str, password: str) -> None:
        super().__init__("http://127.0.0.1:8080")
        self.username = username
        self.password = password
        self.cookie_file = "/tmp/qbit-cookies.txt"

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        form_data: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        curl_parts = [
            "curl",
            "-s",
            "-X",
            shlex.quote(method),
            "-b",
            shlex.quote(self.cookie_file),
            "-c",
            shlex.quote(self.cookie_file),
            "-w",
            shlex.quote("\n__STATUS__:%{http_code}"),
        ]
        if payload is not None:
            curl_parts.extend([
                "-H",
                shlex.quote("Content-Type: application/json"),
                "--data",
                shlex.quote(json.dumps(payload)),
            ])
        elif form_data is not None:
            curl_parts.extend([
                "-H",
                shlex.quote("Content-Type: application/x-www-form-urlencoded"),
                "--data",
                shlex.quote(parse.urlencode(form_data)),
            ])

        curl_parts.append(shlex.quote(f"{self.base_url}{path}"))

        result = run_compose(
            ["exec", "-T", "qbittorrent", "sh", "-lc", " ".join(curl_parts)],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl request failed for {method} {path}: {result.stderr.strip()}")

        output = result.stdout
        body, _, status_line = output.rpartition("\n__STATUS__:")
        status_code = status_line.strip()

        if not status_code.isdigit() or not 200 <= int(status_code) < 300:
            response_text = body.strip()
            error_text = result.stderr.strip() or response_text or f"HTTP {status_code or 'unknown'}"
            raise RuntimeError(f"{method} {path} failed: {error_text}")

        if not expect_json:
            return body

        if not body.strip():
            return None

        return json.loads(body)

    def login(self) -> None:
        last_response = ""
        wait_timeout = int(os.environ.get("QBITTORRENT_API_WAIT_TIMEOUT") or os.environ.get("SETUP_WAIT_TIMEOUT") or "300")
        deadline = time.monotonic() + wait_timeout

        while time.monotonic() < deadline:
            try:
                response = self.request_json(
                    "POST",
                    "/api/v2/auth/login",
                    form_data={"username": self.username, "password": self.password},
                    expect_json=False,
                )
                last_response = response.strip()
                if last_response in {"", "Ok."}:
                    return
                if last_response == "Fails.":
                    raise RuntimeError(
                        "qBittorrent login failed: credentials were rejected; check "
                        "QBITTORRENT_USERNAME/QBITTORRENT_PASSWORD or ADMIN_USERNAME/GLOBAL_PASSWORD"
                    )
            except RuntimeError as exc:
                last_response = str(exc)

            time.sleep(2)

        raise RuntimeError(
            f"qBittorrent login failed after {wait_timeout}s: "
            f"{last_response or 'service did not become ready in time'}"
        )


class QuiApi:
    def __init__(self) -> None:
        # The qui image is distroless and has no HTTP client; use a required
        # shared-network service to reach its un-published API.
        self.client = ContainerJsonClient(
            "prowlarr",
            "http://qui:7476",
            cookie_file="/tmp/Helianthus-qui-cookies.txt",
        )

    def setup_required(self) -> bool:
        response = self.client.request_json("GET", "/api/auth/check-setup") or {}
        return bool(response.get("setupRequired"))

    def setup(self, username: str, password: str) -> None:
        self.client.request_json(
            "POST",
            "/api/auth/setup",
            payload={"username": username, "password": password},
        )

    def login(self, username: str, password: str) -> None:
        self.client.request_json(
            "POST",
            "/api/auth/login",
            payload={"username": username, "password": password, "remember_me": False},
        )

    def get_instances(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/instances/") or []

    def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request_json("POST", "/api/instances/", payload=payload) or {}

    def update_instance(self, instance_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request_json("PUT", f"/api/instances/{instance_id}/", payload=payload) or {}

    def test_instance(self, instance_id: int) -> dict[str, Any]:
        return self.client.request_json("POST", f"/api/instances/{instance_id}/test") or {}

    def discover_indexers(self, base_url: str, api_key: str) -> dict[str, Any]:
        return self.client.request_json(
            "POST",
            "/api/torznab/indexers/discover",
            payload={"base_url": base_url, "api_key": api_key},
        ) or {}

    def get_indexers(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/torznab/indexers") or []

    def create_indexer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request_json("POST", "/api/torznab/indexers", payload=payload) or {}

    def update_indexer(self, indexer_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.request_json("PUT", f"/api/torznab/indexers/{indexer_id}", payload=payload) or {}

    def test_indexer(self, indexer_id: int) -> dict[str, Any]:
        return self.client.request_json("POST", f"/api/torznab/indexers/{indexer_id}/test") or {}


class ArrApi:
    def __init__(self, service: ArrService, api_key: str) -> None:
        self.service = service
        service_port = {
            "sonarr": 8989,
            "radarr": 7878,
        }[service.service_name]
        self.client = ContainerJsonClient(
            service.service_name,
            f"http://127.0.0.1:{service_port}{service.url_base}",
            default_headers={"X-Api-Key": api_key},
        )
        self.api_base = f"/api/{service.api_version}"

    def get_root_folders(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", f"{self.api_base}/rootfolder") or []

    def configure_authentication(self, username: str, password: str) -> None:
        host_config = self.client.request_json("GET", f"{self.api_base}/config/host")
        host_config.update(
            {
                "authenticationMethod": "forms",
                "authenticationRequired": "enabled",
                "username": username,
                "password": password,
                "passwordConfirmation": password,
            }
        )
        self.client.request_json("PUT", f"{self.api_base}/config/host", payload=host_config)

    def get_quality_profiles(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", f"{self.api_base}/qualityprofile") or []

    def get_language_profiles(self) -> list[dict[str, Any]]:
        try:
            return self.client.request_json("GET", f"{self.api_base}/languageprofile") or []
        except RuntimeError:
            return []

    def get_metadata_profiles(self) -> list[dict[str, Any]]:
        try:
            return self.client.request_json("GET", f"{self.api_base}/metadataprofile") or []
        except RuntimeError:
            return []

    def create_root_folder(self, path: str) -> None:
        payload: dict[str, Any] = {"path": path}
        self.client.request_json("POST", f"{self.api_base}/rootfolder", payload=payload)

    def get_download_clients(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", f"{self.api_base}/downloadclient") or []

    def get_download_client_schema(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", f"{self.api_base}/downloadclient/schema") or []

    def upsert_download_client(self, payload: dict[str, Any], item_id: int | None) -> None:
        if item_id is None:
            self.client.request_json("POST", f"{self.api_base}/downloadclient", payload=payload)
            return

        self._try_put(f"{self.api_base}/downloadclient", item_id, payload)

    def _try_put(self, base_path: str, item_id: int, payload: dict[str, Any]) -> None:
        errors: list[str] = []

        for path in (f"{base_path}/{item_id}", base_path):
            try:
                self.client.request_json("PUT", path, payload=payload)
                return
            except RuntimeError as exc:
                errors.append(str(exc))

        raise RuntimeError("\n".join(errors))


class ProwlarrApi:
    def __init__(self, api_key: str) -> None:
        self.client = ContainerJsonClient(
            "prowlarr",
            "http://127.0.0.1:9696",
            default_headers={"X-Api-Key": api_key},
        )

    def get_applications(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/applications") or []

    def get_app_profiles(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/appprofile") or []

    def get_tags(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/tag") or []

    def create_tag(self, label: str) -> dict[str, Any]:
        return self.client.request_json("POST", "/api/v1/tag", payload={"label": label}) or {}

    def get_indexer_proxies(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/indexerProxy") or []

    def get_indexer_proxy_schema(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/indexerProxy/schema") or []

    def get_indexers(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/indexer") or []

    def get_indexer_schema(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/indexer/schema") or []

    def configure_authentication(self, username: str, password: str) -> None:
        host_config = self.client.request_json("GET", "/api/v1/config/host")
        host_config.update(
            {
                "authenticationMethod": "forms",
                "authenticationRequired": "enabled",
                "username": username,
                "password": password,
                "passwordConfirmation": password,
            }
        )
        self.client.request_json("PUT", "/api/v1/config/host", payload=host_config)

    def get_schema(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/applications/schema") or []

    def upsert_application(self, payload: dict[str, Any], item_id: int | None) -> None:
        self._upsert("/api/v1/applications", payload, item_id)

    def upsert_app_profile(self, payload: dict[str, Any], item_id: int | None) -> None:
        self._upsert("/api/v1/appprofile", payload, item_id)

    def upsert_indexer_proxy(self, payload: dict[str, Any], item_id: int | None) -> None:
        self._upsert("/api/v1/indexerProxy", payload, item_id)

    def upsert_indexer(self, payload: dict[str, Any], item_id: int | None) -> None:
        self._upsert("/api/v1/indexer", payload, item_id)

    def _upsert(self, base_path: str, payload: dict[str, Any], item_id: int | None) -> None:
        if item_id is None:
            self.client.request_json("POST", base_path, payload=payload)
            return

        errors: list[str] = []
        for path in (f"{base_path}/{item_id}", base_path):
            try:
                self.client.request_json("PUT", path, payload=payload)
                return
            except RuntimeError as exc:
                errors.append(str(exc))

        raise RuntimeError("\n".join(errors))


class ProfilarrApi:
    def __init__(self, origin: str) -> None:
        self.client = ContainerJsonClient(
            "profilarr",
            "http://127.0.0.1:6868",
            default_headers={"Origin": origin},
        )

    def get_arr_instances(self) -> list[dict[str, Any]]:
        page_data = self.client.request_json("GET", "/arr/__data.json") or {}
        for node in reversed(page_data.get("nodes", [])):
            flattened = node.get("data")
            if not isinstance(flattened, list) or not flattened:
                continue
            decoded = decode_sveltekit_data(flattened)
            if isinstance(decoded, dict) and isinstance(decoded.get("instances"), list):
                return decoded["instances"]
        return []

    def create_arr_instance(self, desired: dict[str, Any]) -> None:
        response = self.client.request_json(
            "POST",
            "/arr/new",
            form_data={
                "name": desired["name"],
                "type": desired["type"],
                "url": desired["url"],
                "external_url": desired["external_url"] or "",
                "api_key": desired["api_key"],
                "tags": json.dumps(desired["tags"]),
            },
            accepted_statuses={200, 303},
        )
        if response is None:
            return
        if isinstance(response, dict) and response.get("type") == "redirect" and response.get("status") == 303:
            return
        raise RuntimeError(f"Profilarr did not confirm creation of {desired['name']}")

    def update_arr_instance(self, instance_id: int, desired: dict[str, Any], library_refresh_interval: int) -> None:
        self.client.request_json(
            "POST",
            f"/arr/{instance_id}/settings?/update",
            form_data={
                "name": desired["name"],
                "url": desired["url"],
                "external_url": desired["external_url"] or "",
                "api_key": desired["api_key"],
                "tags": json.dumps(desired["tags"]),
                "library_refresh_interval": str(library_refresh_interval),
            },
            expect_json=False,
            accepted_statuses={200},
        )


def decode_sveltekit_data(flattened: list[Any]) -> Any:
    def decode_reference(index: int) -> Any:
        return decode_value(flattened[index])

    def decode_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: decode_reference(item) if isinstance(item, int) and not isinstance(item, bool) else decode_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                decode_reference(item) if isinstance(item, int) and not isinstance(item, bool) else decode_value(item)
                for item in value
            ]
        return value

    return decode_reference(0)


class JellyfinApi(ContainerJsonClient):
    def __init__(self) -> None:
        super().__init__("jellyfin", "http://127.0.0.1:8096")

    @classmethod
    def authenticated(cls, access_token: str) -> "JellyfinApi":
        api = cls()
        api.default_headers = {"X-Emby-Token": access_token}
        return api

    def get_public_info(self) -> dict[str, Any]:
        command = [
            "exec",
            "-T",
            "jellyfin",
            "curl",
            "-sS",
            f"{self.base_url}/System/Info/Public",
        ]
        result = run_compose(command)
        return json.loads(result.stdout)

    def get_startup_configuration(self) -> dict[str, Any]:
        return self.request_json("GET", "/Startup/Configuration") or {}

    def set_startup_configuration(self, payload: dict[str, Any]) -> None:
        self.request_json("POST", "/Startup/Configuration", payload=payload, expect_json=False)

    def get_startup_user(self) -> dict[str, Any]:
        return self.request_json("GET", "/Startup/User") or {}

    def set_startup_user(self, username: str, password: str) -> None:
        errors = []
        for _ in range(120):
            startup_user = self.get_startup_user()
            if startup_user.get("Name") or startup_user.get("Username"):
                break
            time.sleep(1)

        for attempt in range(60):
            errors = []
            for payload in (
                {"Name": username, "Password": password},
                {"Username": username, "Password": password},
            ):
                try:
                    self.request_json("POST", "/Startup/User", payload=payload, expect_json=False)
                    return
                except RuntimeError as exc:
                    errors.append(str(exc))
            if attempt < 59:
                time.sleep(1)

        raise RuntimeError("\n".join(errors))

    def set_startup_remote_access(self) -> None:
        payload = {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False}
        self.request_json("POST", "/Startup/RemoteAccess", payload=payload, expect_json=False)

    def complete_startup(self) -> None:
        self.request_json("POST", "/Startup/Complete", payload={}, expect_json=False)

    def authenticate(self, username: str, password: str) -> str:
        auth_header = (
            'MediaBrowser Client="Helianthus Setup", '
            'Device="Helianthus", '
            f'DeviceId="{uuid.uuid5(uuid.NAMESPACE_DNS, "Helianthus-jellyfin-setup")}", '
            'Version="1.0"'
        )
        client = JellyfinApi()
        client.default_headers = {"Authorization": auth_header}
        payload = {"Username": username, "Pw": password}
        response = client.request_json("POST", "/Users/AuthenticateByName", payload=payload) or {}
        token = response.get("AccessToken", "")
        if not token:
            raise RuntimeError("Jellyfin authentication did not return an access token")
        return token

    def get_current_user(self) -> dict[str, Any]:
        return self.request_json("GET", "/Users/Me") or {}

    def update_user(self, user_id: str, payload: dict[str, Any]) -> None:
        query = parse.urlencode({"userId": user_id})
        self.request_json("POST", f"/Users?{query}", payload=payload, expect_json=False)

    def get_system_configuration(self) -> dict[str, Any]:
        return self.request_json("GET", "/System/Configuration") or {}

    def set_system_configuration(self, payload: dict[str, Any]) -> None:
        self.request_json("POST", "/System/Configuration", payload=payload, expect_json=False)

    def get_virtual_folders(self) -> list[dict[str, Any]]:
        return self.request_json("GET", "/Library/VirtualFolders") or []

    def create_virtual_folder(self, name: str, collection_type: str, path: str) -> None:
        query = parse.urlencode(
            {
                "name": name,
                "collectionType": collection_type,
                "paths": path,
                "refreshLibrary": "false",
            }
        )
        self.request_json("POST", f"/Library/VirtualFolders?{query}", payload={}, expect_json=False)


class SeerrApi(ContainerJsonClient):
    def __init__(self, api_key: str = "") -> None:
        headers = {"X-API-Key": api_key} if api_key else None
        super().__init__("jellyfin", "http://seerr:5055", default_headers=headers)

    def authenticate_jellyfin_admin(self, env: dict[str, str]) -> dict[str, Any]:
        payload = {
            "username": env["SEERR_JELLYFIN_ADMIN_USERNAME"],
            "password": env["SEERR_JELLYFIN_ADMIN_PASSWORD"],
            "hostname": "jellyfin",
            "port": 8096,
            "urlBase": "",
            "useSsl": False,
            "email": env.get("SEERR_JELLYFIN_ADMIN_EMAIL", "") or env["SEERR_JELLYFIN_ADMIN_USERNAME"],
            "serverType": SEERR_MEDIA_SERVER_TYPE_JELLYFIN,
        }
        return self.request_json("POST", "/api/v1/auth/jellyfin", payload=payload) or {}

    def initialize(self) -> None:
        self.request_json("POST", "/api/v1/settings/initialize")


def field_value_map(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {field["name"]: field.get("value") for field in fields}


def apply_field_overrides(fields: list[dict[str, Any]], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    patched_fields = copy.deepcopy(fields)
    for field in patched_fields:
        if field["name"] in overrides:
            field["value"] = overrides[field["name"]]
    return patched_fields


def schema_by_implementation(schema: list[dict[str, Any]], implementation: str) -> dict[str, Any]:
    for item in schema:
        if item.get("implementation") == implementation:
            return copy.deepcopy(item)

    raise RuntimeError(f"Schema for implementation {implementation} was not found")


def select_profile(profiles: list[dict[str, Any]], preferred_name: str) -> dict[str, Any]:
    if not profiles:
        raise RuntimeError("No quality profiles were returned by the Arr service")

    candidates = []
    if preferred_name:
        candidates.append(preferred_name)
    candidates.extend(["HD-1080p", "HD - 720p/1080p", "Any"])

    for candidate in candidates:
        for profile in profiles:
            if profile.get("name", "").lower() == candidate.lower():
                return profile

    return profiles[0]


def select_language_profile(profiles: list[dict[str, Any]], preferred_name: str) -> dict[str, Any] | None:
    if not profiles:
        return None

    if preferred_name:
        for profile in profiles:
            if profile.get("name", "").lower() == preferred_name.lower():
                return profile

    return profiles[0]


def upsert_seerr_service(
    settings_list: list[dict[str, Any]],
    desired: dict[str, Any],
) -> tuple[str, bool]:
    existing = next(
        (
            item
            for item in settings_list
            if not item.get("is4k") and item.get("name") == desired["name"]
        ),
        None,
    )
    if existing is None:
        existing = next((item for item in settings_list if not item.get("is4k")), None)

    if existing is None:
        created = dict(desired)
        created["id"] = next_settings_id(settings_list)
        settings_list.append(created)
        for item in settings_list:
            if item is not created and item.get("is4k") == created["is4k"] and item.get("isDefault"):
                item["isDefault"] = False
        return "Created", True

    changed = False
    for key, value in desired.items():
        if existing.get(key) != value:
            existing[key] = value
            changed = True

    for item in settings_list:
        if item is existing:
            continue
        if item.get("is4k") == existing.get("is4k") and item.get("isDefault"):
            item["isDefault"] = False
            changed = True

    return ("Updated", True) if changed else ("Already matches", False)


def build_seerr_radarr_settings(arr_api: ArrApi, env: dict[str, str]) -> dict[str, Any]:
    profiles = arr_api.get_quality_profiles()
    profile = select_profile(profiles, env.get("SEERR_RADARR_PROFILE", ""))

    return {
        "name": arr_api.service.display_name,
        "hostname": arr_api.service.service_name,
        "port": 7878,
        "apiKey": env[arr_api.service.api_key_env],
        "useSsl": False,
        "baseUrl": arr_api.service.url_base,
        "activeProfileId": profile["id"],
        "activeProfileName": profile["name"],
        "activeDirectory": env[arr_api.service.root_folder_env],
        "tags": [],
        "is4k": False,
        "isDefault": True,
        "externalUrl": build_external_url(env, arr_api.service.service_name),
        "syncEnabled": env_bool(env, "SEERR_SYNC_ENABLED", True),
        "preventSearch": not env_bool(env, "SEERR_AUTO_SEARCH", True),
        "tagRequests": False,
        "overrideRule": [],
        "minimumAvailability": env.get("SEERR_RADARR_MINIMUM_AVAILABILITY", "released") or "released",
    }


def build_seerr_sonarr_settings(arr_api: ArrApi, env: dict[str, str]) -> dict[str, Any]:
    profiles = arr_api.get_quality_profiles()
    profile = select_profile(profiles, env.get("SEERR_SONARR_PROFILE", ""))
    language_profiles = arr_api.get_language_profiles()
    language_profile = select_language_profile(language_profiles, env.get("SEERR_SONARR_LANGUAGE_PROFILE", ""))

    desired: dict[str, Any] = {
        "name": arr_api.service.display_name,
        "hostname": arr_api.service.service_name,
        "port": 8989,
        "apiKey": env[arr_api.service.api_key_env],
        "useSsl": False,
        "baseUrl": arr_api.service.url_base,
        "activeProfileId": profile["id"],
        "activeProfileName": profile["name"],
        "activeDirectory": env[arr_api.service.root_folder_env],
        "tags": [],
        "is4k": False,
        "isDefault": True,
        "externalUrl": build_external_url(env, arr_api.service.service_name),
        "syncEnabled": env_bool(env, "SEERR_SYNC_ENABLED", True),
        "preventSearch": not env_bool(env, "SEERR_AUTO_SEARCH", True),
        "tagRequests": False,
        "overrideRule": [],
        "seriesType": "standard",
        "animeSeriesType": "anime",
        "activeAnimeProfileId": profile["id"],
        "activeAnimeProfileName": profile["name"],
        "activeAnimeDirectory": env[arr_api.service.root_folder_env],
        "animeTags": [],
        "enableSeasonFolders": env_bool(env, "SEERR_SONARR_SEASON_FOLDERS", True),
        "monitorNewItems": "all",
    }

    if language_profile is not None:
        desired["activeLanguageProfileId"] = language_profile["id"]
        desired["activeAnimeLanguageProfileId"] = language_profile["id"]

    return desired


def seerr_user_count(env: dict[str, str]) -> int | None:
    db_path = get_config_root(env) / "seerr" / "db" / "db.sqlite3"
    if not db_path.exists():
        return None

    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute('select count(*) from "user"').fetchone()
            return int(row[0])
    except sqlite3.Error:
        return None


def clear_seeded_jellyfin_settings_for_initial_setup(settings: dict[str, Any]) -> bool:
    jellyfin_settings = settings.setdefault("jellyfin", {})
    desired_empty_values: dict[str, Any] = {
        "name": "",
        "ip": "",
        "port": 8096,
        "useSsl": False,
        "urlBase": "",
        "externalHostname": "",
        "serverId": "",
        "apiKey": "",
    }

    changed = False
    for key, value in desired_empty_values.items():
        if jellyfin_settings.get(key) != value:
            jellyfin_settings[key] = value
            changed = True

    return changed


def jellyfin_startup_completed(public_info: dict[str, Any]) -> bool:
    for key in ("StartupWizardCompleted", "IsStartupWizardCompleted"):
        if key in public_info:
            return bool(public_info[key])
    return False


def complete_jellyfin_startup(api: JellyfinApi, env: dict[str, str], dry_run: bool) -> bool:
    username = env["JELLYFIN_ADMIN_USERNAME"]
    password = env["JELLYFIN_ADMIN_PASSWORD"]
    server_name = env["JELLYFIN_SERVER_NAME"]

    if dry_run:
        log(f"[dry-run] Would complete Jellyfin startup wizard with admin user {username}")
        return True

    configuration = api.get_startup_configuration()
    configuration.update(
        {
            "ServerName": server_name,
            "UICulture": configuration.get("UICulture") or "en-US",
            "MetadataCountryCode": configuration.get("MetadataCountryCode") or "US",
            "PreferredMetadataLanguage": configuration.get("PreferredMetadataLanguage") or "en",
        }
    )
    api.set_startup_configuration(configuration)
    api.set_startup_user(username, password)
    api.set_startup_remote_access()
    api.complete_startup()
    log(f"Completed Jellyfin startup wizard and created admin user {username}")
    return True


def ensure_jellyfin_admin_login(api: JellyfinApi, env: dict[str, str], dry_run: bool) -> str:
    username = env["JELLYFIN_ADMIN_USERNAME"]
    password = env["JELLYFIN_ADMIN_PASSWORD"]

    try:
        return api.authenticate(username, password)
    except RuntimeError:
        pass

    fallback_usernames = []
    if username != "abc":
        fallback_usernames.append("abc")

    for fallback_username in fallback_usernames:
        try:
            token = api.authenticate(fallback_username, password)
        except RuntimeError:
            continue

        if dry_run:
            log(f"[dry-run] Would rename Jellyfin user {fallback_username} to {username}")
            return token

        authenticated_api = JellyfinApi.authenticated(token)
        user = authenticated_api.get_current_user()
        user_id = user["Id"]
        user["Name"] = username
        authenticated_api.update_user(user_id, user)
        log(f"Renamed Jellyfin user {fallback_username} to {username}")
        return api.authenticate(username, password)

    raise RuntimeError(f"Jellyfin admin login failed for {username}")


def ensure_jellyfin_server_name(api: JellyfinApi, env: dict[str, str], dry_run: bool) -> bool:
    desired_name = env["JELLYFIN_SERVER_NAME"]
    public_info = api.get_public_info()
    if public_info.get("ServerName") == desired_name:
        log("Jellyfin server name already matches the desired state")
        return False

    if dry_run:
        log(f"[dry-run] Would set Jellyfin server name to {desired_name}")
        return True

    token = ensure_jellyfin_admin_login(api, env, dry_run)
    authenticated_api = JellyfinApi.authenticated(token)
    configuration = authenticated_api.get_system_configuration()
    configuration["ServerName"] = desired_name
    authenticated_api.set_system_configuration(configuration)
    log(f"Updated Jellyfin server name to {desired_name}")
    return True


def ensure_jellyfin_libraries(api: JellyfinApi, env: dict[str, str], dry_run: bool) -> None:
    if dry_run:
        for name, _, path in (
            ("Movies", "movies", env["RADARR_ROOT_FOLDER"]),
            ("Shows", "tvshows", env["SONARR_ROOT_FOLDER"]),
        ):
            log(f"[dry-run] Would ensure Jellyfin {name} library at {path}")
        return

    token = ensure_jellyfin_admin_login(api, env, dry_run)
    authenticated_api = JellyfinApi.authenticated(token)
    existing_folders = authenticated_api.get_virtual_folders()

    desired_libraries = (
        ("Movies", "movies", env["RADARR_ROOT_FOLDER"]),
        ("Shows", "tvshows", env["SONARR_ROOT_FOLDER"]),
    )

    for name, collection_type, path in desired_libraries:
        ensure_directory("jellyfin", path, dry_run)

        existing = next(
            (
                folder
                for folder in existing_folders
                if folder.get("Name") == name
                or path in folder.get("Locations", [])
                or path in folder.get("Paths", [])
            ),
            None,
        )
        if existing is not None:
            log(f"Jellyfin library already present: {name}")
            continue

        try:
            authenticated_api.create_virtual_folder(name, collection_type, path)
            log(f"Created Jellyfin {name} library at {path}")
        except RuntimeError as exc:
            log(f"Skipping Jellyfin {name} library creation after API error: {exc}")


def ensure_jellyfin_setup(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "jellyfin" not in running_services:
        log("Skipping Jellyfin automation because the service is not running")
        return

    api = JellyfinApi()
    public_info = api.get_public_info()

    if not jellyfin_startup_completed(public_info):
        complete_jellyfin_startup(api, env, dry_run)
    else:
        log("Jellyfin startup wizard already complete")

    try:
        changed = ensure_jellyfin_server_name(api, env, dry_run)
        if changed and not dry_run:
            run_compose(["restart", "jellyfin"])
            time.sleep(5)
            log("Restarted Jellyfin after server-name update")
    except RuntimeError as exc:
        log(f"Skipping Jellyfin server-name update after API error: {exc}")

    try:
        ensure_jellyfin_libraries(api, env, dry_run)
    except RuntimeError as exc:
        log(f"Skipping Jellyfin library setup after API error: {exc}")


def ensure_seerr_jellyfin_admin_setup(settings_path: Path, settings: dict[str, Any], env: dict[str, str], dry_run: bool) -> dict[str, Any]:
    username = env.get("SEERR_JELLYFIN_ADMIN_USERNAME", "")
    password = env.get("SEERR_JELLYFIN_ADMIN_PASSWORD", "")
    if not username and not password:
        return settings
    if not username or not password:
        log("Skipping Seerr Jellyfin initial setup because SEERR_JELLYFIN_ADMIN_USERNAME or SEERR_JELLYFIN_ADMIN_PASSWORD is empty")
        return settings

    if settings.get("jellyfin", {}).get("apiKey"):
        log("Seerr Jellyfin API key already exists")
        return settings

    user_count = seerr_user_count(env)
    if user_count not in (0, None):
        log("Skipping Seerr Jellyfin initial setup because Seerr already has users")
        return settings

    if dry_run:
        log("[dry-run] Would authenticate Seerr to Jellyfin and create the Seerr Jellyfin API key")
        return settings

    if clear_seeded_jellyfin_settings_for_initial_setup(settings):
        settings_path.write_text(json.dumps(settings, indent=1) + "\n")
        run_compose(["restart", "seerr"])
        log("Cleared pre-seeded Jellyfin settings before Seerr initial setup")

    try:
        SeerrApi().authenticate_jellyfin_admin(env)
    except RuntimeError as exc:
        log(
            "Seerr could not authenticate to Jellyfin. Check "
            "SEERR_JELLYFIN_ADMIN_USERNAME and SEERR_JELLYFIN_ADMIN_PASSWORD in .env."
        )
        log(f"Skipping Seerr Jellyfin initial setup after API error: {exc}")
        return settings
    log("Authenticated Seerr to Jellyfin and created the Seerr Jellyfin API key")
    settings = json.loads(settings_path.read_text())

    seerr_api_key = settings.get("main", {}).get("apiKey", "")
    if seerr_api_key and not settings.get("public", {}).get("initialized"):
        SeerrApi(seerr_api_key).initialize()
        log("Marked Seerr initial setup complete")
        settings = json.loads(settings_path.read_text())

    return settings


def ensure_seerr_integrations(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "seerr" not in running_services:
        log("Skipping Seerr automation because the service is not running")
        return

    settings_path = get_config_root(env) / "seerr" / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings_changed = False
    env_changed = False

    if "jellyfin" in running_services:
        settings = ensure_seerr_jellyfin_admin_setup(settings_path, settings, env, dry_run)

    seerr_api_key = settings.get("main", {}).get("apiKey", "")
    if seerr_api_key:
        env_changed = update_env_file_value(ROOT_DIR / ".env", "SEERR_API_KEY", seerr_api_key, dry_run) or env_changed
        if env_changed:
            env["SEERR_API_KEY"] = seerr_api_key

    jellyfin_api_key = settings.get("jellyfin", {}).get("apiKey", "")
    if jellyfin_api_key:
        env_changed = update_env_file_value(ROOT_DIR / ".env", "JELLYFIN_API_KEY", jellyfin_api_key, dry_run) or env_changed
        if env_changed:
            env["JELLYFIN_API_KEY"] = jellyfin_api_key

    seerr_application_url = build_external_url(env, "seerr")
    if seerr_application_url and settings.get("main", {}).get("applicationUrl") != seerr_application_url:
        settings.setdefault("main", {})["applicationUrl"] = seerr_application_url
        settings_changed = True

    if "jellyfin" in running_services:
        jellyfin_public_info = JellyfinApi().get_public_info()
        jellyfin_external_url = env.get("SEERR_JELLYFIN_EXTERNAL_URL", "") or build_external_url(env, "jellyfin")
        jellyfin_settings = settings.setdefault("jellyfin", {})
        desired_jellyfin = {
            "name": jellyfin_public_info.get("ServerName", jellyfin_settings.get("name", "")),
            "ip": "jellyfin",
            "port": 8096,
            "useSsl": False,
            "urlBase": "",
            "externalHostname": jellyfin_external_url,
            "serverId": jellyfin_public_info.get("Id", jellyfin_settings.get("serverId", "")),
        }
        if env.get("JELLYFIN_API_KEY"):
            desired_jellyfin["apiKey"] = env["JELLYFIN_API_KEY"]

        for key, value in desired_jellyfin.items():
            if jellyfin_settings.get(key) != value:
                jellyfin_settings[key] = value
                settings_changed = True

        if settings.get("main", {}).get("mediaServerType") != SEERR_MEDIA_SERVER_TYPE_JELLYFIN:
            settings.setdefault("main", {})["mediaServerType"] = SEERR_MEDIA_SERVER_TYPE_JELLYFIN
            settings_changed = True

    for arr_service in ARR_SERVICES:
        if arr_service.service_name not in running_services:
            continue
        if not env.get(arr_service.api_key_env):
            log(f"Skipping Seerr {arr_service.display_name} integration because {arr_service.api_key_env} is empty")
            continue

        arr_api = ArrApi(arr_service, env[arr_service.api_key_env])
        if arr_service.service_name == "radarr":
            desired = build_seerr_radarr_settings(arr_api, env)
            action, changed = upsert_seerr_service(settings.setdefault("radarr", []), desired)
        else:
            desired = build_seerr_sonarr_settings(arr_api, env)
            action, changed = upsert_seerr_service(settings.setdefault("sonarr", []), desired)

        if changed:
            settings_changed = True
            log(f"{action} Seerr {arr_service.display_name} service settings")
        else:
            log(f"Seerr {arr_service.display_name} service settings already match the desired state")

    if dry_run:
        if settings_changed:
            log("[dry-run] Would update Seerr settings.json")
        return

    if settings_changed:
        settings_path.write_text(json.dumps(settings, indent=1) + "\n")

    if env_changed:
        run_compose(["restart", "seerr"])
        log("Updated Seerr API key in .env and restarted Seerr")
    elif settings_changed:
        run_compose(["restart", "seerr"])
        log("Updated Seerr settings and restarted Seerr")
    else:
        log("Seerr settings already match the desired state")


def ensure_directory(service: str, path: str, dry_run: bool) -> None:
    command = f"mkdir -p {shlex.quote(path)} && chown -R abc:abc {shlex.quote(path)}"
    exec_in_service(service, command, dry_run)


def ensure_qbittorrent_paths_and_categories(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "qbittorrent" not in running_services:
        log("Skipping qBittorrent automation because the service is not running")
        return

    ensure_directory("qbittorrent", env["QBITTORRENT_SAVE_PATH"], dry_run)
    ensure_directory("qbittorrent", env["QBITTORRENT_TEMP_PATH"], dry_run)

    for arr_service in ARR_SERVICES:
        if arr_service.service_name in running_services:
            ensure_directory("qbittorrent", env[arr_service.download_path_env], dry_run)

    if dry_run:
        log("[dry-run] Would update qBittorrent preferences and categories")
        return

    qbit = QBittorrentClient(env["QBITTORRENT_USERNAME"], env["QBITTORRENT_PASSWORD"])
    qbit.login()
    preferences = qbit.request_json("GET", "/api/v2/app/preferences")

    desired_preferences: dict[str, Any] = {}
    if preferences.get("save_path") != env["QBITTORRENT_SAVE_PATH"]:
        desired_preferences["save_path"] = env["QBITTORRENT_SAVE_PATH"]
    if preferences.get("temp_path") != env["QBITTORRENT_TEMP_PATH"]:
        desired_preferences["temp_path"] = env["QBITTORRENT_TEMP_PATH"]
    if preferences.get("temp_path_enabled") is not True:
        desired_preferences["temp_path_enabled"] = True

    forwarded_port_path = get_config_root(env) / "pia-shared" / "port.dat"
    if forwarded_port_path.exists():
        forwarded_port_text = forwarded_port_path.read_text().strip()
        if forwarded_port_text.isdigit() and preferences.get("listen_port") != int(forwarded_port_text):
            desired_preferences["listen_port"] = int(forwarded_port_text)

    if desired_preferences:
        qbit.request_json(
            "POST",
            "/api/v2/app/setPreferences",
            form_data={"json": json.dumps(desired_preferences)},
            expect_json=False,
        )
        log("Updated qBittorrent save-path preferences")
    else:
        log("qBittorrent save-path preferences already match the desired state")

    categories = qbit.request_json("GET", "/api/v2/torrents/categories") or {}
    for arr_service in ARR_SERVICES:
        if arr_service.service_name not in running_services:
            continue

        category_name = env[arr_service.category_env]
        save_path = env[arr_service.download_path_env]
        existing = categories.get(category_name)

        if existing is None:
            qbit.request_json(
                "POST",
                "/api/v2/torrents/createCategory",
                form_data={"category": category_name, "savePath": save_path},
                expect_json=False,
            )
            log(f"Created qBittorrent category {category_name}")
            continue

        if existing.get("savePath") != save_path:
            qbit.request_json(
                "POST",
                "/api/v2/torrents/editCategory",
                form_data={"category": category_name, "savePath": save_path},
                expect_json=False,
            )
            log(f"Updated qBittorrent category {category_name}")
        else:
            log(f"qBittorrent category {category_name} already matches the desired state")


def ensure_qui_qbittorrent_instance(
    api: QuiApi,
    env: dict[str, str],
    running_services: set[str],
) -> None:
    if "qbittorrent" not in running_services:
        log("Skipping qui qBittorrent connection because qBittorrent is not running")
        return
    instance_payload = {
        "name": "qBittorrent",
        "host": "http://vpn:8080",
        "username": env["QBITTORRENT_USERNAME"],
        "password": env["QBITTORRENT_PASSWORD"],
        "hasLocalFilesystemAccess": True,
    }

    instances = api.get_instances()
    existing = next(
        (
            instance
            for instance in instances
            if instance.get("name") == instance_payload["name"]
            or str(instance.get("host", "")).rstrip("/") == instance_payload["host"]
        ),
        None,
    )

    if existing:
        instance = api.update_instance(int(existing["id"]), instance_payload)
        log("Updated qui qBittorrent connection")
    else:
        instance = api.create_instance(instance_payload)
        log("Created qui qBittorrent connection")

    connection = api.test_instance(int(instance["id"]))
    if not connection.get("connected"):
        raise RuntimeError(f"qui could not connect to qBittorrent: {connection.get('error') or connection}")
    log("Verified qui qBittorrent connection")


def ensure_qui_prowlarr_indexers(api: QuiApi, env: dict[str, str]) -> None:
    prowlarr_api_key = env.get("PROWLARR_API_KEY", "").strip()
    if not prowlarr_api_key:
        log("Skipping qui Prowlarr indexer discovery because PROWLARR_API_KEY is empty")
        return

    prowlarr_url = "http://prowlarr:9696"
    discovery = api.discover_indexers(prowlarr_url, prowlarr_api_key)
    discovered_indexers = discovery.get("indexers", [])
    if not isinstance(discovered_indexers, list):
        raise RuntimeError(f"qui returned an invalid Prowlarr discovery response: {discovery}")

    for warning in discovery.get("warnings", []):
        log(f"qui Prowlarr discovery warning: {warning}")

    existing_by_name = {
        str(indexer.get("name", "")): indexer
        for indexer in api.get_indexers()
        if indexer.get("name")
    }
    synced = 0

    for discovered in discovered_indexers:
        name = str(discovered.get("name", "")).strip()
        prowlarr_indexer_id = str(discovered.get("id", "")).strip()
        if not name or not prowlarr_indexer_id:
            log(f"Skipping invalid qui Prowlarr discovery result: {discovered}")
            continue

        payload = {
            "base_url": prowlarr_url,
            "api_key": prowlarr_api_key,
            "backend": discovered.get("backend") or "prowlarr",
            "indexer_id": prowlarr_indexer_id,
            "capabilities": discovered.get("caps") or [],
            "categories": discovered.get("categories") or [],
        }
        existing = existing_by_name.get(name)
        if existing:
            indexer = api.update_indexer(int(existing["id"]), payload)
            log(f"Updated qui Prowlarr indexer {name}")
        else:
            indexer = api.create_indexer({"name": name, "enabled": True, **payload})
            log(f"Created qui Prowlarr indexer {name}")

        synced += 1
        try:
            result = api.test_indexer(int(indexer["id"]))
            if result.get("status") != "ok":
                log(f"qui Prowlarr indexer {name} test warning: {result}")
        except RuntimeError as exc:
            log(f"qui Prowlarr indexer {name} test warning: {exc}")

    log(f"Synced {synced} Prowlarr indexer(s) into qui")


def ensure_qui_integration(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "qui" not in running_services:
        return
    if "prowlarr" not in running_services:
        log("Skipping qui automation because Prowlarr is not running")
        return

    username = env["ADMIN_USERNAME"]
    password = env["GLOBAL_PASSWORD"]
    if len(password) < 8:
        raise RuntimeError("qui requires GLOBAL_PASSWORD to contain at least 8 characters")

    if dry_run:
        log("[dry-run] Would configure qui credentials, qBittorrent connection, and Prowlarr indexers")
        return

    api = QuiApi()
    if api.setup_required():
        api.setup(username, password)
        log("Configured qui global credentials")
    else:
        try:
            api.login(username, password)
        except RuntimeError as exc:
            raise RuntimeError(
                "qui login failed with ADMIN_USERNAME/GLOBAL_PASSWORD; "
                "reset qui or restore the credentials previously used by setup"
            ) from exc

    ensure_qui_qbittorrent_instance(api, env, running_services)
    ensure_qui_prowlarr_indexers(api, env)


def ensure_arr_root_folder(arr_api: ArrApi, env: dict[str, str], dry_run: bool) -> None:
    path = env[arr_api.service.root_folder_env]
    ensure_directory(arr_api.service.service_name, path, dry_run)

    if dry_run:
        log(f"[dry-run] Would ensure {arr_api.service.display_name} root folder {path}")
        return

    root_folders = arr_api.get_root_folders()
    if any(folder.get("path") == path for folder in root_folders):
        log(f"{arr_api.service.display_name} root folder already present: {path}")
        return

    arr_api.create_root_folder(path)
    log(f"Created {arr_api.service.display_name} root folder {path}")


def ensure_arr_download_client(arr_api: ArrApi, env: dict[str, str], dry_run: bool) -> None:
    schema = schema_by_implementation(arr_api.get_download_client_schema(), "QBittorrent")
    field_overrides = {
        "host": "vpn",
        "port": 8080,
        "useSsl": False,
        "urlBase": "",
        "username": env["QBITTORRENT_USERNAME"],
        "password": env["QBITTORRENT_PASSWORD"],
        arr_api.service.qbit_implementation_field: env[arr_api.service.category_env],
    }
    if arr_api.service.qbit_directory_field is not None:
        field_overrides[arr_api.service.qbit_directory_field] = env[arr_api.service.download_path_env]

    payload = {
        "name": "qBittorrent",
        "enable": True,
        "protocol": schema.get("protocol", "torrent"),
        "priority": schema.get("priority", 1),
        "removeCompletedDownloads": schema.get("removeCompletedDownloads", True),
        "removeFailedDownloads": schema.get("removeFailedDownloads", True),
        "implementationName": schema.get("implementationName", "qBittorrent"),
        "implementation": schema["implementation"],
        "configContract": schema["configContract"],
        "fields": apply_field_overrides(schema["fields"], field_overrides),
        "tags": schema.get("tags", []),
    }

    if dry_run:
        log(f"[dry-run] Would ensure {arr_api.service.display_name} qBittorrent download client")
        return

    existing_clients = arr_api.get_download_clients()
    existing = next(
        (
            client
            for client in existing_clients
            if client.get("implementation") == "QBittorrent" or client.get("name") == "qBittorrent"
        ),
        None,
    )

    if existing is not None:
        desired_values = field_value_map(payload["fields"])
        current_values = field_value_map(existing.get("fields", []))
        same_fields = True
        for field_name, desired_value in desired_values.items():
            if field_name == "password":
                continue
            if current_values.get(field_name) != desired_value:
                same_fields = False
                break

        if existing.get("enable") and same_fields:
            log(f"{arr_api.service.display_name} qBittorrent client already matches the desired state")
            return

        payload["id"] = existing["id"]
        arr_api.upsert_download_client(payload, existing["id"])
        log(f"Updated {arr_api.service.display_name} qBittorrent client")
        return

    arr_api.upsert_download_client(payload, None)
    log(f"Created {arr_api.service.display_name} qBittorrent client")


def ensure_prowlarr_application(
    prowlarr_api: ProwlarrApi,
    arr_service: ArrService,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    schema = schema_by_implementation(prowlarr_api.get_schema(), arr_service.prowlarr_implementation)
    payload = {
        "name": arr_service.display_name,
        "enable": schema.get("enable", True),
        "syncLevel": schema.get("syncLevel", "fullSync"),
        "implementationName": schema.get("implementationName", arr_service.display_name),
        "implementation": schema["implementation"],
        "configContract": schema["configContract"],
        "fields": apply_field_overrides(
            schema["fields"],
            {
                "prowlarrUrl": "http://prowlarr:9696",
                "baseUrl": arr_service.internal_base_url,
                "apiKey": env[arr_service.api_key_env],
            },
        ),
        "tags": schema.get("tags", []),
    }

    if dry_run:
        log(f"[dry-run] Would ensure Prowlarr application link for {arr_service.display_name}")
        return

    existing_apps = prowlarr_api.get_applications()
    existing = next(
        (
            app
            for app in existing_apps
            if app.get("implementation") == arr_service.prowlarr_implementation
            or app.get("name") == arr_service.display_name
        ),
        None,
    )

    if existing is not None:
        desired_values = field_value_map(payload["fields"])
        current_values = field_value_map(existing.get("fields", []))
        same_fields = all(
            name == "apiKey" or current_values.get(name) == value
            for name, value in desired_values.items()
        )
        if existing.get("enable") and existing.get("syncLevel") == payload["syncLevel"] and same_fields:
            log(f"Prowlarr link for {arr_service.display_name} already matches the desired state")
            return

        payload["id"] = existing["id"]
        prowlarr_api.upsert_application(payload, existing["id"])
        log(f"Updated Prowlarr link for {arr_service.display_name}")
        return

    prowlarr_api.upsert_application(payload, None)
    log(f"Created Prowlarr link for {arr_service.display_name}")


def load_prowlarr_config(env: dict[str, str]) -> dict[str, Any]:
    if not PROWLARR_CONFIG_PATH.exists():
        return {}

    config = json.loads(PROWLARR_CONFIG_PATH.read_text())
    if not isinstance(config, dict):
        raise RuntimeError(f"{PROWLARR_CONFIG_PATH} must contain a JSON object")

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return ENV_VAR_PATTERN.sub(lambda match: env.get(match.group(1), match.group(3) or ""), value)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        return value

    return expand(config)


def ensure_schema_fields(schema_fields: list[dict[str, Any]], overrides: dict[str, Any], resource_name: str) -> None:
    available = {field["name"] for field in schema_fields}
    unknown = sorted(set(overrides) - available)
    if unknown:
        raise RuntimeError(f"{resource_name} contains fields not present in the Prowlarr schema: {', '.join(unknown)}")


def managed_prowlarr_resource_matches(
    existing: dict[str, Any],
    payload: dict[str, Any],
    managed_keys: tuple[str, ...],
    managed_fields: dict[str, Any] | None = None,
) -> bool:
    for key in managed_keys:
        current = existing.get(key)
        desired = payload.get(key)
        if key == "tags":
            if set(current or []) != set(desired or []):
                return False
        elif current != desired:
            return False

    if managed_fields is None:
        return True

    current_fields = field_value_map(existing.get("fields", []))
    return all(current_fields.get(name) == value for name, value in managed_fields.items())


def ensure_prowlarr_tags(
    prowlarr_api: ProwlarrApi,
    desired_tags: list[str],
    dry_run: bool,
) -> dict[str, int]:
    existing_tags = prowlarr_api.get_tags()
    tag_ids = {tag["label"]: tag["id"] for tag in existing_tags}

    for label in desired_tags:
        if label in tag_ids:
            log(f"Prowlarr tag {label} already exists")
            continue
        if dry_run:
            tag_ids[label] = -1
            log(f"[dry-run] Would create Prowlarr tag {label}")
            continue

        created = prowlarr_api.create_tag(label)
        if "id" not in created:
            raise RuntimeError(f"Prowlarr did not return an id after creating tag {label}")
        tag_ids[label] = created["id"]
        log(f"Created Prowlarr tag {label}")

    return tag_ids


def ensure_prowlarr_app_profiles(
    prowlarr_api: ProwlarrApi,
    desired_profiles: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, int]:
    existing_profiles = prowlarr_api.get_app_profiles()
    profile_ids = {profile["name"]: profile["id"] for profile in existing_profiles}

    for desired in desired_profiles:
        name = desired["name"]
        existing = next((profile for profile in existing_profiles if profile.get("name") == name), None)
        if existing is not None and all(existing.get(key) == value for key, value in desired.items()):
            log(f"Prowlarr app profile {name} already matches the desired state")
            continue

        if dry_run:
            profile_ids.setdefault(name, -1)
            action = "update" if existing is not None else "create"
            log(f"[dry-run] Would {action} Prowlarr app profile {name}")
            continue

        payload = dict(desired)
        item_id = existing.get("id") if existing is not None else None
        if item_id is not None:
            payload["id"] = item_id
        prowlarr_api.upsert_app_profile(payload, item_id)
        if item_id is None:
            refreshed = next(
                (profile for profile in prowlarr_api.get_app_profiles() if profile.get("name") == name),
                None,
            )
            if refreshed is None:
                raise RuntimeError(f"Prowlarr app profile {name} was not found after creation")
            profile_ids[name] = refreshed["id"]
            existing_profiles.append(refreshed)
            log(f"Created Prowlarr app profile {name}")
        else:
            profile_ids[name] = item_id
            log(f"Updated Prowlarr app profile {name}")

    return profile_ids


def desired_tag_ids(resource: dict[str, Any], tag_ids: dict[str, int]) -> list[int]:
    labels = resource.get("tags", [])
    missing = sorted(label for label in labels if label not in tag_ids)
    if missing:
        raise RuntimeError(f"Unknown Prowlarr tags referenced by {resource['name']}: {', '.join(missing)}")
    return [tag_ids[label] for label in labels]


def ensure_prowlarr_indexer_proxies(
    prowlarr_api: ProwlarrApi,
    desired_proxies: list[dict[str, Any]],
    tag_ids: dict[str, int],
    dry_run: bool,
) -> None:
    existing_proxies = prowlarr_api.get_indexer_proxies()
    schemas = prowlarr_api.get_indexer_proxy_schema()

    for desired in desired_proxies:
        name = desired["name"]
        schema = schema_by_implementation(schemas, desired["implementation"])
        field_overrides = desired.get("fields", {})
        ensure_schema_fields(schema["fields"], field_overrides, f"Prowlarr indexer proxy {name}")
        payload = {
            "name": name,
            "implementationName": schema.get("implementationName", desired["implementation"]),
            "implementation": schema["implementation"],
            "configContract": schema["configContract"],
            "fields": apply_field_overrides(schema["fields"], field_overrides),
            "tags": desired_tag_ids(desired, tag_ids),
            "onHealthIssue": schema.get("onHealthIssue", False),
            "includeHealthWarnings": schema.get("includeHealthWarnings", False),
        }
        existing = next(
            (
                proxy
                for proxy in existing_proxies
                if proxy.get("name") == name or proxy.get("implementation") == desired["implementation"]
            ),
            None,
        )
        if existing is not None and managed_prowlarr_resource_matches(
            existing,
            payload,
            ("name", "implementation", "tags"),
            field_overrides,
        ):
            log(f"Prowlarr indexer proxy {name} already matches the desired state")
            continue

        if dry_run:
            action = "update" if existing is not None else "create"
            log(f"[dry-run] Would {action} Prowlarr indexer proxy {name}")
            continue

        item_id = existing.get("id") if existing is not None else None
        if item_id is not None:
            payload["id"] = item_id
        prowlarr_api.upsert_indexer_proxy(payload, item_id)
        log(f"{'Updated' if item_id is not None else 'Created'} Prowlarr indexer proxy {name}")


def prowlarr_indexer_schema(
    schemas: list[dict[str, Any]],
    definition_name: str,
    implementation: str,
) -> dict[str, Any]:
    for schema in schemas:
        if schema.get("definitionName") == definition_name and schema.get("implementation") == implementation:
            return copy.deepcopy(schema)
    raise RuntimeError(f"Prowlarr indexer schema {definition_name} ({implementation}) was not found")


def ensure_prowlarr_indexers(
    prowlarr_api: ProwlarrApi,
    desired_indexers: list[dict[str, Any]],
    tag_ids: dict[str, int],
    profile_ids: dict[str, int],
    dry_run: bool,
) -> None:
    existing_indexers = prowlarr_api.get_indexers()
    schemas = prowlarr_api.get_indexer_schema()

    for desired in desired_indexers:
        name = desired["name"]
        profile_name = desired["appProfile"]
        if profile_name not in profile_ids:
            raise RuntimeError(f"Unknown Prowlarr app profile referenced by {name}: {profile_name}")

        schema = prowlarr_indexer_schema(schemas, desired["definitionName"], desired["implementation"])
        field_overrides = desired.get("fields", {})
        ensure_schema_fields(schema["fields"], field_overrides, f"Prowlarr indexer {name}")
        payload = {
            "name": name,
            "enable": desired.get("enable", schema.get("enable", True)),
            "redirect": desired.get("redirect", schema.get("redirect", False)),
            "priority": desired.get("priority", schema.get("priority", 25)),
            "appProfileId": profile_ids[profile_name],
            "downloadClientId": desired.get("downloadClientId", schema.get("downloadClientId", 0)),
            "implementationName": schema.get("implementationName", name),
            "implementation": schema["implementation"],
            "configContract": schema["configContract"],
            "fields": apply_field_overrides(schema["fields"], field_overrides),
            "tags": desired_tag_ids(desired, tag_ids),
        }
        existing = next(
            (
                indexer
                for indexer in existing_indexers
                if indexer.get("name") == name
                or (
                    indexer.get("definitionName") == desired["definitionName"]
                    and indexer.get("implementation") == desired["implementation"]
                )
            ),
            None,
        )
        if existing is not None and managed_prowlarr_resource_matches(
            existing,
            payload,
            ("name", "enable", "priority", "appProfileId", "downloadClientId", "implementation", "tags"),
            field_overrides,
        ):
            log(f"Prowlarr indexer {name} already matches the desired state")
            continue

        if dry_run:
            action = "update" if existing is not None else "create"
            log(f"[dry-run] Would {action} Prowlarr indexer {name}")
            continue

        item_id = existing.get("id") if existing is not None else None
        if item_id is not None:
            payload["id"] = item_id
        prowlarr_api.upsert_indexer(payload, item_id)
        log(f"{'Updated' if item_id is not None else 'Created'} Prowlarr indexer {name}")


def ensure_prowlarr_config_resources(
    prowlarr_api: ProwlarrApi,
    env: dict[str, str],
    running_services: set[str],
    dry_run: bool,
) -> None:
    config = load_prowlarr_config(env)
    if not config:
        log(f"Skipping Prowlarr resource automation because {PROWLARR_CONFIG_PATH.relative_to(ROOT_DIR)} is absent")
        return

    tag_ids = ensure_prowlarr_tags(prowlarr_api, config.get("tags", []), dry_run)
    profile_ids = ensure_prowlarr_app_profiles(prowlarr_api, config.get("appProfiles", []), dry_run)
    desired_proxies = []
    for proxy in config.get("indexerProxies", []):
        required_service = proxy.get("requiresService")
        if required_service and required_service not in running_services:
            log(f"Skipping Prowlarr indexer proxy {proxy['name']} because {required_service} is not running")
            continue
        desired_proxies.append(proxy)
    ensure_prowlarr_indexer_proxies(prowlarr_api, desired_proxies, tag_ids, dry_run)
    ensure_prowlarr_indexers(prowlarr_api, config.get("indexers", []), tag_ids, profile_ids, dry_run)


def ensure_arr_integrations(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    for arr_service in ARR_SERVICES:
        api_key = env.get(arr_service.api_key_env, "")
        if arr_service.service_name not in running_services:
            continue
        if not api_key:
            log(f"Skipping {arr_service.display_name} because {arr_service.api_key_env} is empty")
            continue

        arr_api = ArrApi(arr_service, api_key)
        if dry_run:
            log(f"[dry-run] Would configure {arr_service.display_name} forms authentication")
        else:
            prefix = arr_service.service_name.upper()
            arr_api.configure_authentication(env[f"{prefix}_USERNAME"], env[f"{prefix}_PASSWORD"])
            log(f"Configured {arr_service.display_name} forms authentication")
        ensure_arr_root_folder(arr_api, env, dry_run)
        ensure_arr_download_client(arr_api, env, dry_run)


def ensure_prowlarr_integrations(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "prowlarr" not in running_services:
        log("Skipping Prowlarr automation because the service is not running")
        return

    prowlarr_key = env.get("PROWLARR_API_KEY", "")
    if not prowlarr_key:
        log("Skipping Prowlarr automation because PROWLARR_API_KEY is empty")
        return

    prowlarr_api = ProwlarrApi(prowlarr_key)
    if dry_run:
        log("[dry-run] Would configure Prowlarr forms authentication")
    else:
        prowlarr_api.configure_authentication(env["PROWLARR_USERNAME"], env["PROWLARR_PASSWORD"])
        log("Configured Prowlarr forms authentication")
    ensure_prowlarr_config_resources(prowlarr_api, env, running_services, dry_run)
    for arr_service in ARR_SERVICES:
        if arr_service.service_name not in running_services:
            continue
        if not env.get(arr_service.api_key_env, ""):
            continue
        ensure_prowlarr_application(prowlarr_api, arr_service, env, dry_run)


def profilarr_instance_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    managed_keys = ("name", "type", "url", "external_url", "tags")
    return all((existing.get(key) or [] if key == "tags" else existing.get(key)) == desired[key] for key in managed_keys)


def profilarr_runtime_origin() -> str:
    result = run_compose(["exec", "-T", "profilarr", "sh", "-lc", "printf '%s' \"${ORIGIN:-}\""], check=False)
    origin = result.stdout.strip()
    if result.returncode == 0 and origin:
        return origin
    return "http://127.0.0.1:6868"


def wait_for_profilarr_instance(profilarr_api: ProfilarrApi, desired: dict[str, Any]) -> dict[str, Any]:
    for _ in range(20):
        instances = profilarr_api.get_arr_instances()
        existing = next(
            (
                instance
                for instance in instances
                if instance.get("name") == desired["name"]
                or (instance.get("type") == desired["type"] and instance.get("url") == desired["url"])
            ),
            None,
        )
        if existing is not None:
            return existing
        time.sleep(1)
    raise RuntimeError(f"Profilarr instance {desired['name']} was not found after creation")


def ensure_profilarr_integrations(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    if "profilarr" not in running_services:
        if profile_enabled(env, "profilarr"):
            log("Skipping Profilarr automation because the service is not running")
        return

    profilarr_api = ProfilarrApi(profilarr_runtime_origin())
    instances = profilarr_api.get_arr_instances()
    for arr_service in ARR_SERVICES:
        if arr_service.service_name not in {"sonarr", "radarr"}:
            continue
        if arr_service.service_name not in running_services:
            continue

        api_key = env.get(arr_service.api_key_env, "")
        if not api_key:
            log(f"Skipping Profilarr {arr_service.display_name} connection because {arr_service.api_key_env} is empty")
            continue

        desired = {
            "name": arr_service.display_name,
            "type": arr_service.service_name,
            "url": arr_service.internal_base_url,
            "external_url": build_external_url(env, arr_service.service_name) or None,
            "api_key": api_key,
            "tags": [],
        }
        existing = next(
            (
                instance
                for instance in instances
                if instance.get("name") == desired["name"]
                or (instance.get("type") == desired["type"] and instance.get("url") == desired["url"])
            ),
            None,
        )
        if existing is not None and existing.get("type") != desired["type"]:
            raise RuntimeError(
                f"Profilarr instance {desired['name']} exists with type {existing.get('type')}, "
                f"expected {desired['type']}"
            )

        if existing is not None and profilarr_instance_matches(existing, desired):
            log(f"Profilarr {arr_service.display_name} connection already matches the desired state")
            continue

        if dry_run:
            action = "update" if existing is not None else "create"
            log(f"[dry-run] Would {action} Profilarr {arr_service.display_name} connection")
            continue

        if existing is None:
            profilarr_api.create_arr_instance(desired)
            existing = wait_for_profilarr_instance(profilarr_api, desired)
            instances.append(existing)
            log(f"Created Profilarr {arr_service.display_name} connection")
            continue

        profilarr_api.update_arr_instance(existing["id"], desired, existing["library_refresh_interval"])
        log(f"Updated Profilarr {arr_service.display_name} connection")

def main() -> int:
    parser = argparse.ArgumentParser(description="Automate app-to-app connections for the Helianthus stack.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing anything")
    parser.add_argument("--skip-qbittorrent", action="store_true", help="Skip qBittorrent path and category updates")
    parser.add_argument("--skip-arr", action="store_true", help="Skip Sonarr/Radarr root folders and download clients")
    parser.add_argument("--skip-prowlarr", action="store_true", help="Skip Prowlarr resources and application links")
    parser.add_argument("--skip-jellyfin", action="store_true", help="Skip Jellyfin initial setup and library configuration")
    parser.add_argument("--skip-seerr", action="store_true", help="Skip Seerr service and media-server preconfiguration")
    parser.add_argument(
        "--skip-qui",
        action="store_true",
        help="Skip qui initial setup, qBittorrent connection, and Prowlarr indexer discovery",
    )
    parser.add_argument("--skip-profilarr", action="store_true", help="Skip Profilarr Sonarr/Radarr connections")
    args = parser.parse_args()

    env = parse_env_file(ROOT_DIR / ".env.example")
    env.update(parse_env_file(ROOT_DIR / ".env"))
    env = resolve_env_values(env)
    env = apply_blank_aware_defaults(env)
    running_services = compose_running_services()

    if not args.skip_qbittorrent:
        ensure_qbittorrent_paths_and_categories(env, running_services, args.dry_run)

    if not args.skip_arr:
        ensure_arr_integrations(env, running_services, args.dry_run)

    if not args.skip_prowlarr:
        ensure_prowlarr_integrations(env, running_services, args.dry_run)

    if not args.skip_qui:
        ensure_qui_integration(env, running_services, args.dry_run)

    if not args.skip_jellyfin:
        ensure_jellyfin_setup(env, running_services, args.dry_run)

    if not args.skip_seerr:
        ensure_seerr_integrations(env, running_services, args.dry_run)

    if not args.skip_profilarr:
        ensure_profilarr_integrations(env, running_services, args.dry_run)

    sync_generated_api_keys(env, args.dry_run)
    reapply_homepage_label_services(running_services, args.dry_run)
    write_homepage_services(env, running_services, args.dry_run)

    log("App connection automation complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
