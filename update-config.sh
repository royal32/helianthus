#!/bin/bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# See https://stackoverflow.com/a/44864004 for the sed GNU/BSD compatible hack

function strip_wrapping_quotes {
  local value="$1"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

function get_env_value {
  local key="$1"
  local line

  line=$(awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1) }' .env | tail -n 1)
  if [[ -z "$line" ]] && ! grep -q -E "^${key}=" .env; then
    return 1
  fi

  strip_wrapping_quotes "$line"
}

function effective_env_value {
  local key="$1"
  local fallback="$2"
  local value

  value=$(get_env_value "$key" || true)
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return 0
  fi

  printf '%s' "$fallback"
}

function qbit_password_hash {
  python3 - "$1" "${2:-}" <<'PY'
import base64
import hashlib
import os
import re
import sys

password = sys.argv[1].encode("utf-8")
existing = sys.argv[2].strip().strip('"')
match = re.fullmatch(r"@ByteArray\(([^:]+):([^)]+)\)", existing)
if match:
    try:
        salt = base64.b64decode(match.group(1))
        expected = base64.b64decode(match.group(2))
        actual = hashlib.pbkdf2_hmac("sha512", password, salt, 100000)
        if actual == expected:
            print(existing)
            raise SystemExit(0)
    except Exception:
        pass

salt = os.urandom(16)
digest = hashlib.pbkdf2_hmac("sha512", password, salt, 100000)
print(f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()})")
PY
}

function get_qbit_config_value {
    local config_file="$1"
    local key="$2"

    awk -v key="$key" '
      index($0, key "=") == 1 {
        print substr($0, length(key) + 2)
      }
    ' "$config_file" | tail -n 1
}

function set_qbit_config_value {
    local config_file="$1"
    local key="$2"
    local value="$3"
    local tmp_file

    tmp_file=$(mktemp)
    awk -v key="$key" -v value="$value" '
        index($0, key "=") == 1 {
          print key "=" value
          updated=1
          next
        }
        { print }
        END {
          if (!updated) {
            print key "=" value
          }
        }
      ' "$config_file" > "$tmp_file"
    mv "$tmp_file" "$config_file"
}

function set_xml_config_value {
  local config_file="$1"
  local key="$2"
  local value="$3"
  local tmp_file

  tmp_file=$(mktemp)
  awk -v key="$key" -v value="$value" '
    $0 ~ "<" key ">.*</" key ">" {
      print "  <" key ">" value "</" key ">"
      updated = 1
      next
    }
    $0 ~ "</Config>" && !updated {
      print "  <" key ">" value "</" key ">"
      updated = 1
    }
    { print }
  ' "$config_file" > "$tmp_file"
  mv "$tmp_file" "$config_file"
}

function update_arr_config {
  echo "Updating ${container} configuration..."
  local arr_config="${CONFIG_ROOT:-.}"/"$container"/config.xml
  until [ -f "$arr_config" ]; do sleep 1; done
  set_xml_config_value "$arr_config" "UrlBase" "/$1"
  if [[ "$container" == "prowlarr" ]]; then
    set_xml_config_value "$arr_config" "AuthenticationMethod" "External"
    set_xml_config_value "$arr_config" "AuthenticationRequired" "DisabledForLocalAddresses"
  fi
  CONTAINER_NAME_UPPER=$(echo "$container" | tr '[:lower:]' '[:upper:]')
  sed -i.bak 's/^'"${CONTAINER_NAME_UPPER}"'_API_KEY=.*/'"${CONTAINER_NAME_UPPER}"'_API_KEY='"$(sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "$arr_config")"'/' .env && rm .env.bak
  echo "Update of ${container} configuration complete, restarting..."
  docker compose restart "$container"
}

function update_qbittorrent_config {
    echo "Updating ${container} configuration..."
    docker compose stop "$container"
    local qbittorrent_config="${CONFIG_ROOT:-.}"/"$container"/qBittorrent/qBittorrent.conf
    local admin_username
    local global_password
    local qbittorrent_username
    local qbittorrent_password
    local existing_password_hash
    until [ -f "$qbittorrent_config" ]; do sleep 1; done

    admin_username=$(effective_env_value "ADMIN_USERNAME" "admin")
    global_password=$(effective_env_value "GLOBAL_PASSWORD" "adminadmin")
    qbittorrent_username=$(effective_env_value "QBITTORRENT_USERNAME" "$admin_username")
    qbittorrent_password=$(effective_env_value "QBITTORRENT_PASSWORD" "$global_password")
    existing_password_hash=$(get_qbit_config_value "$qbittorrent_config" 'WebUI\Password_PBKDF2')

    set_qbit_config_value "$qbittorrent_config" 'WebUI\Username' "$qbittorrent_username"
    set_qbit_config_value "$qbittorrent_config" 'WebUI\Password_PBKDF2' "\"$(qbit_password_hash "$qbittorrent_password" "$existing_password_hash")\""
    echo "Update of ${container} configuration complete, restarting..."
    docker compose start "$container"
}

function update_bazarr_config {
    echo "Updating ${container} configuration..."
    local bazarr_config="${CONFIG_ROOT:-.}"/"$container"/config/config/config.yaml
    until [ -f "$bazarr_config" ]; do sleep 1; done
    sed -i.bak "s|base_url: ''|base_url: '/$container'|" "$bazarr_config" && rm "$bazarr_config".bak
    sed -i.bak "s/use_radarr: false/use_radarr: true/" "$bazarr_config" && rm "$bazarr_config".bak
    sed -i.bak "s/use_sonarr: false/use_sonarr: true/" "$bazarr_config" && rm "$bazarr_config".bak
    until [ -f "${CONFIG_ROOT:-.}"/sonarr/config.xml ]; do sleep 1; done
    SONARR_API_KEY=$(sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "${CONFIG_ROOT:-.}"/sonarr/config.xml)
    sed -i.bak \
      -e "/sonarr:/,/^radarr:/ s|apikey: .*|apikey: $SONARR_API_KEY|" \
      -e "/sonarr:/,/^radarr:/ s|base_url: .*|base_url: '/sonarr'|" \
      -e "/sonarr:/,/^radarr:/ s|ip: .*|ip: sonarr|" \
      "$bazarr_config" && rm "$bazarr_config".bak
    until [ -f "${CONFIG_ROOT:-.}"/radarr/config.xml ]; do sleep 1; done
    RADARR_API_KEY=$(sed -n 's/.*<ApiKey>\(.*\)<\/ApiKey>.*/\1/p' "${CONFIG_ROOT:-.}"/radarr/config.xml)
    sed -i.bak \
      -e "/radarr:/,/^sonarr:/ s|apikey: .*|apikey: $RADARR_API_KEY|" \
      -e "/radarr:/,/^sonarr:/ s|base_url: .*|base_url: '/radarr'|" \
      -e "/radarr:/,/^sonarr:/ s|ip: .*|ip: radarr|" \
      "$bazarr_config" && rm "$bazarr_config".bak
    sed -i.bak 's/^BAZARR_API_KEY=.*/BAZARR_API_KEY='"$(sed -n 's/.*apikey: \(.*\)*/\1/p' "$bazarr_config" | head -n 1)"'/' .env && rm .env.bak
    echo "Update of ${container} configuration complete, restarting..."
    docker compose restart "$container"
}

for container in $(docker compose ps --services --status running); do
  if [[ "$container" =~ ^(radarr|sonarr|lidarr|prowlarr)$ ]]; then
    update_arr_config "$container"
  elif [[ "$container" =~ ^(bazarr)$ ]]; then
    update_bazarr_config "$container"
  elif [[ "$container" =~ ^(qbittorrent)$ ]]; then
    update_qbittorrent_config "$container"
  fi
done
