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
SSL_CONTEXT = ssl._create_unverified_context()
ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(:-([^}]*))?\}")
SEERR_MEDIA_SERVER_TYPE_JELLYFIN = 2


@dataclass(frozen=True)
class ArrService:
    service_name: str
    display_name: str
    url_base: str
    api_key_env: str
    hostname_env: str
    root_folder_env: str
    category_env: str
    download_path_env: str
    qbit_implementation_field: str
    qbit_directory_field: str | None
    prowlarr_implementation: str
    internal_base_url: str


ARR_SERVICES: tuple[ArrService, ...] = (
    ArrService(
        service_name="sonarr",
        display_name="Sonarr",
        url_base="",
        api_key_env="SONARR_API_KEY",
        hostname_env="SONARR_HOSTNAME",
        root_folder_env="SONARR_ROOT_FOLDER",
        category_env="SONARR_QBIT_CATEGORY",
        download_path_env="SONARR_DOWNLOAD_PATH",
        qbit_implementation_field="tvCategory",
        qbit_directory_field=None,
        prowlarr_implementation="Sonarr",
        internal_base_url="http://sonarr:8989",
    ),
    ArrService(
        service_name="radarr",
        display_name="Radarr",
        url_base="",
        api_key_env="RADARR_API_KEY",
        hostname_env="RADARR_HOSTNAME",
        root_folder_env="RADARR_ROOT_FOLDER",
        category_env="RADARR_QBIT_CATEGORY",
        download_path_env="RADARR_DOWNLOAD_PATH",
        qbit_implementation_field="movieCategory",
        qbit_directory_field=None,
        prowlarr_implementation="Radarr",
        internal_base_url="http://radarr:7878",
    ),
    ArrService(
        service_name="lidarr",
        display_name="Lidarr",
        url_base="",
        api_key_env="LIDARR_API_KEY",
        hostname_env="LIDARR_HOSTNAME",
        root_folder_env="LIDARR_ROOT_FOLDER",
        category_env="LIDARR_QBIT_CATEGORY",
        download_path_env="LIDARR_DOWNLOAD_PATH",
        qbit_implementation_field="category",
        qbit_directory_field="directory",
        prowlarr_implementation="Lidarr",
        internal_base_url="http://lidarr:8686",
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
        "ADGUARD_USERNAME",
        "CALIBRE_USERNAME",
        "PAPERLESS_ADMIN_USER",
    ):
        if not env.get(key):
            env[key] = admin_username

    for key in (
        "QBITTORRENT_PASSWORD",
        "ADGUARD_PASSWORD",
        "CALIBRE_PASSWORD",
        "PAPERLESS_ADMIN_PASSWORD",
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
        "LIDARR_API_KEY": read_xml_text(config_root / "lidarr" / "config.xml", "ApiKey"),
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
            "href": build_external_url(env, "", env.get("SONARR_HOSTNAME", "")) or "/",
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
            "href": build_external_url(env, "", env.get("RADARR_HOSTNAME", "")) or "/",
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
            "href": build_external_url(env, "", env.get("SEERR_HOSTNAME", "")) or "/",
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
            "href": build_external_url(env, "", env.get("JELLYFIN_HOSTNAME", "")) or "/",
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
            "href": build_external_url(env, "", env.get("PROWLARR_HOSTNAME", "")) or "/",
            "description": "Indexer management",
            "widget": {
                "type": "prowlarr",
                "url": "http://prowlarr:9696",
                "key": env.get("PROWLARR_API_KEY", ""),
            },
        },
        {
            "service": "qbittorrent",
            "group": "Download",
            "name": "qBittorrent",
            "icon": "qbittorrent.png",
            "href": build_external_url(env, "", env.get("QBITTORRENT_HOSTNAME", "")) or "/",
            "description": "BitTorrent client",
            "widget": {
                "type": "qbittorrent",
                "url": "http://vpn:8080",
                "username": env.get("QBITTORRENT_USERNAME", ""),
                "password": env.get("QBITTORRENT_PASSWORD", ""),
            },
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


def build_external_url(env: dict[str, str], path: str = "", host: str = "") -> str:
    scheme = env.get("PUBLIC_SCHEME") or "https"
    host = host or env.get("PUBLIC_HOSTNAME") or env.get("HOSTNAME", "")
    if not host or "${" in host:
        return ""
    return f"{scheme}://{host}{path}"


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
    def __init__(self, service: str, base_url: str, default_headers: dict[str, str] | None = None) -> None:
        self.service = service
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
        command = [
            "exec",
            "-T",
            self.service,
            "curl",
            "-sS",
            "-o",
            "-",
            "-w",
            "\n__STATUS__:%{http_code}",
            "-X",
            method,
        ]

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

            if result.returncode == 0 and status_code.isdigit() and 200 <= int(status_code) < 300:
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


class ArrApi:
    def __init__(self, service: ArrService, api_key: str) -> None:
        self.service = service
        service_port = {
            "sonarr": 8989,
            "radarr": 7878,
            "lidarr": 8686,
        }[service.service_name]
        self.client = ContainerJsonClient(
            service.service_name,
            f"http://127.0.0.1:{service_port}{service.url_base}",
            default_headers={"X-Api-Key": api_key},
        )

    def get_root_folders(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v3/rootfolder") or []

    def get_quality_profiles(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v3/qualityprofile") or []

    def get_language_profiles(self) -> list[dict[str, Any]]:
        try:
            return self.client.request_json("GET", "/api/v3/languageprofile") or []
        except RuntimeError:
            return []

    def create_root_folder(self, path: str) -> None:
        self.client.request_json("POST", "/api/v3/rootfolder", payload={"path": path})

    def get_download_clients(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v3/downloadclient") or []

    def get_download_client_schema(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v3/downloadclient/schema") or []

    def upsert_download_client(self, payload: dict[str, Any], item_id: int | None) -> None:
        if item_id is None:
            self.client.request_json("POST", "/api/v3/downloadclient", payload=payload)
            return

        self._try_put("/api/v3/downloadclient", item_id, payload)

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

    def get_schema(self) -> list[dict[str, Any]]:
        return self.client.request_json("GET", "/api/v1/applications/schema") or []

    def upsert_application(self, payload: dict[str, Any], item_id: int | None) -> None:
        if item_id is None:
            self.client.request_json("POST", "/api/v1/applications", payload=payload)
            return

        errors: list[str] = []
        for path in (f"/api/v1/applications/{item_id}", "/api/v1/applications"):
            try:
                self.client.request_json("PUT", path, payload=payload)
                return
            except RuntimeError as exc:
                errors.append(str(exc))

        raise RuntimeError("\n".join(errors))


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
            'MediaBrowser Client="Docker Compose NAS Setup", '
            'Device="Docker Compose NAS", '
            f'DeviceId="{uuid.uuid5(uuid.NAMESPACE_DNS, "docker-compose-nas-jellyfin-setup")}", '
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
        "externalUrl": build_external_url(env, "", env.get(arr_api.service.hostname_env, "")),
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
        "externalUrl": build_external_url(env, "", env.get(arr_api.service.hostname_env, "")),
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
            ("Music", "music", env["LIDARR_ROOT_FOLDER"]),
        ):
            log(f"[dry-run] Would ensure Jellyfin {name} library at {path}")
        return

    token = ensure_jellyfin_admin_login(api, env, dry_run)
    authenticated_api = JellyfinApi.authenticated(token)
    existing_folders = authenticated_api.get_virtual_folders()

    desired_libraries = (
        ("Movies", "movies", env["RADARR_ROOT_FOLDER"]),
        ("Shows", "tvshows", env["SONARR_ROOT_FOLDER"]),
        ("Music", "music", env["LIDARR_ROOT_FOLDER"]),
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

    seerr_application_url = build_external_url(env, "", env.get("SEERR_HOSTNAME", ""))
    if seerr_application_url and settings.get("main", {}).get("applicationUrl") != seerr_application_url:
        settings.setdefault("main", {})["applicationUrl"] = seerr_application_url
        settings_changed = True

    if "jellyfin" in running_services:
        jellyfin_public_info = JellyfinApi().get_public_info()
        jellyfin_external_url = env.get("SEERR_JELLYFIN_EXTERNAL_URL", "") or build_external_url(
            env,
            "",
            env.get("JELLYFIN_HOSTNAME", ""),
        )
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
        if arr_service.service_name == "lidarr":
            continue
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
        same_fields = all(current_values.get(name) == value for name, value in desired_values.items())
        if existing.get("enable") and existing.get("syncLevel") == payload["syncLevel"] and same_fields:
            log(f"Prowlarr link for {arr_service.display_name} already matches the desired state")
            return

        payload["id"] = existing["id"]
        prowlarr_api.upsert_application(payload, existing["id"])
        log(f"Updated Prowlarr link for {arr_service.display_name}")
        return

    prowlarr_api.upsert_application(payload, None)
    log(f"Created Prowlarr link for {arr_service.display_name}")


def ensure_arr_integrations(env: dict[str, str], running_services: set[str], dry_run: bool) -> None:
    for arr_service in ARR_SERVICES:
        api_key = env.get(arr_service.api_key_env, "")
        if arr_service.service_name not in running_services:
            continue
        if not api_key:
            log(f"Skipping {arr_service.display_name} because {arr_service.api_key_env} is empty")
            continue

        arr_api = ArrApi(arr_service, api_key)
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
    for arr_service in ARR_SERVICES:
        if arr_service.service_name not in running_services:
            continue
        if not env.get(arr_service.api_key_env, ""):
            continue
        ensure_prowlarr_application(prowlarr_api, arr_service, env, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Automate app-to-app connections for the Docker Compose NAS stack.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing anything")
    parser.add_argument("--skip-qbittorrent", action="store_true", help="Skip qBittorrent path and category updates")
    parser.add_argument("--skip-arr", action="store_true", help="Skip Sonarr/Radarr/Lidarr root folders and download clients")
    parser.add_argument("--skip-prowlarr", action="store_true", help="Skip Prowlarr application links")
    parser.add_argument("--skip-jellyfin", action="store_true", help="Skip Jellyfin initial setup and library configuration")
    parser.add_argument("--skip-seerr", action="store_true", help="Skip Seerr service and media-server preconfiguration")
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

    if not args.skip_jellyfin:
        ensure_jellyfin_setup(env, running_services, args.dry_run)

    if not args.skip_seerr:
        ensure_seerr_integrations(env, running_services, args.dry_run)

    sync_generated_api_keys(env, args.dry_run)
    reapply_homepage_label_services(running_services, args.dry_run)
    write_homepage_services(env, running_services, args.dry_run)

    log("App connection automation complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
