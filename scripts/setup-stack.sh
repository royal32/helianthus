#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
ENV_TEMPLATE="$ROOT_DIR/.env.example"
WAIT_TIMEOUT=300
SKIP_UP=0
SKIP_BOOTSTRAP=0
SKIP_CONNECTIONS=0
SKIP_WAIT=0
PROFILES_OVERRIDE=""
SET_OVERRIDES=()

usage() {
  cat <<'EOF'
Usage: ./scripts/setup-stack.sh [options]

Bootstrap the local Helianthus stack by creating missing env files,
filling safe local defaults, validating the compose config, starting the stack,
and running the first-run post-start configuration.

Options:
  --profiles <csv>        Set COMPOSE_PROFILES in .env
  --set KEY=VALUE         Override a root .env variable, can be used multiple times
  --wait-timeout <secs>   Health wait timeout in seconds (default: 300)
  --no-up                 Skip docker compose up -d
  --no-bootstrap          Skip ./scripts/update-config.sh
  --no-connections        Skip automated app-to-app connection setup
  --no-wait               Skip waiting for container health
  --help                  Show this help

Examples:
  ./scripts/setup-stack.sh
  ./scripts/setup-stack.sh --profiles vaultwarden
  ./scripts/setup-stack.sh --set DATA_ROOT=/srv/data --set DOWNLOAD_ROOT=/srv/data/torrents
EOF
}

log() {
  printf '[setup] %s\n' "$1"
}

warn() {
  printf '[setup] warning: %s\n' "$1" >&2
}

print_setup_complete_banner() {
  cat <<'EOF'
##############################################################################
##############################################################################
##                                                                          ##
##                             SETUP COMPLETE                               ##
##                                                                          ##
##                     Helianthus is fully configured.                      ##
##                                                                          ##
##                You can now open the configured services                  ##
##                at any of the tailscale address formats.                  ##
##                                                                          ##
##                jellyfin/                                                 ##
##                jellyfin.<tailnet>.ts.net                                 ##
##                100.x.y.z                                                 ##
##                                                                          ##
##                                                                          ##
##############################################################################
EOF
}

die() {
  printf '[setup] error: %s\n' "$1" >&2
  exit 1
}

strip_wrapping_quotes() {
  local value="$1"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

get_env_value() {
  local file="$1"
  local key="$2"
  local line

  if [[ ! -f "$file" ]]; then
    return 1
  fi

  line=$(awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1) }' "$file" | tail -n 1)
  if [[ -z "$line" ]] && ! grep -q -E "^${key}=" "$file"; then
    return 1
  fi

  strip_wrapping_quotes "$line"
}

resolve_env_references() {
  local value="$1"
  local key
  local replacement
  local depth=0

  while [[ "$value" =~ \$\{([A-Za-z_][A-Za-z0-9_]*)\} && $depth -lt 10 ]]; do
    key="${BASH_REMATCH[1]}"
    replacement=$(get_env_value "$ENV_FILE" "$key" || true)
    value="${value//\$\{${key}\}/$replacement}"
    depth=$((depth + 1))
  done

  printf '%s' "$value"
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp_file
  local current_line
  local formatted_value

  current_line=$(awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1) }' "$file" | tail -n 1)
  formatted_value="$value"
  if [[ "$current_line" == \"*\" && "$current_line" == *\" && ! ( "$formatted_value" == \"*\" && "$formatted_value" == *\" ) ]]; then
    formatted_value="\"${value//\"/\\\"}\""
  fi

  tmp_file=$(mktemp)
  awk -v key="$key" -v value="$formatted_value" '
    BEGIN { updated = 0 }
    index($0, key "=") == 1 {
      print key "=" value
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print key "=" value
      }
    }
  ' "$file" > "$tmp_file"
  mv "$tmp_file" "$file"
}

set_env_value_quoted() {
  local file="$1"
  local key="$2"
  local value="$3"
  local escaped_value

  escaped_value="${value//\"/\\\"}"
  set_env_value "$file" "$key" "\"$escaped_value\""
}

normalize_env_value_quotes() {
  local file="$1"
  local key="$2"
  local value

  value=$(get_env_value "$file" "$key" || true)
  [[ -n "$value" ]] || return 0
  set_env_value_quoted "$file" "$key" "$value"
}

get_config_root() {
  local config_root

  config_root=$(get_env_value "$ENV_FILE" "CONFIG_ROOT" || printf './runtime')
  if [[ "$config_root" = /* ]]; then
    printf '%s' "$config_root"
  else
    printf '%s/%s' "$ROOT_DIR" "$config_root"
  fi
}

set_if_missing_or_default() {
  local file="$1"
  local key="$2"
  local default_value="$3"
  local new_value="$4"
  local current_value

  current_value=$(get_env_value "$file" "$key" || true)
  if [[ -z "$current_value" || ( "$current_value" == "$default_value" && "$current_value" != "$new_value" ) ]]; then
    set_env_value "$file" "$key" "$new_value"
    log "Set $key=$new_value"
  fi
}

set_if_changed() {
  local file="$1"
  local key="$2"
  local value="$3"
  local current_value

  current_value=$(get_env_value "$file" "$key" || true)
  if [[ "$current_value" != "$value" ]]; then
    set_env_value "$file" "$key" "$value"
    log "Set $key=$value"
  fi
}

sync_tsdproxy_access_mode() {
  local disable_tls

  disable_tls=$(get_env_value "$ENV_FILE" "TSDPROXY_DISABLE_TLS" || printf 'false')
  disable_tls=$(printf '%s' "$disable_tls" | tr '[:upper:]' '[:lower:]')
  case "$disable_tls" in
    1|true|yes|on)
      set_if_changed "$ENV_FILE" "TSDPROXY_ACCESS_MODE" "80/http"
      set_if_changed "$ENV_FILE" "TSDPROXY_URL_SCHEME" "http"
      ;;
    0|false|no|off|"")
      set_if_changed "$ENV_FILE" "TSDPROXY_ACCESS_MODE" "443/https"
      set_if_changed "$ENV_FILE" "TSDPROXY_URL_SCHEME" "https"
      ;;
    *)
      die "TSDPROXY_DISABLE_TLS must be true or false"
      ;;
  esac
}

copy_if_missing() {
  local source_file="$1"
  local target_file="$2"

  if [[ -f "$target_file" ]]; then
    return 0
  fi

  mkdir -p "$(dirname "$target_file")"
  cp "$source_file" "$target_file"
  log "Created ${target_file#$ROOT_DIR/} from template"
}

prepare_homepage_config() {
  local config_root
  local homepage_config
  local file

  config_root="$(get_config_root)"
  homepage_config="$config_root/homepage"
  mkdir -p "$homepage_config"

  if [[ -d "$ROOT_DIR/homepage/tpl" ]]; then
    cp -f "$ROOT_DIR"/homepage/tpl/*.yaml "$homepage_config"/
  fi

  for file in custom.css custom.js; do
    if [[ -f "$ROOT_DIR/homepage/$file" && ! -f "$homepage_config/$file" ]]; then
      cp "$ROOT_DIR/homepage/$file" "$homepage_config/$file"
    fi
  done
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    LC_ALL=C od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    printf '\n'
  fi
}

set_generated_secret_if_blank() {
  local file="$1"
  local key="$2"
  local current_value

  current_value=$(get_env_value "$file" "$key" || true)
  if [[ -n "$current_value" ]]; then
    return 0
  fi

  set_env_value "$file" "$key" "$(generate_secret)"
  log "Generated $key in ${file#$ROOT_DIR/}"
}

detect_timezone() {
  local timezone=""

  if [[ -n "${TZ:-}" ]]; then
    timezone="$TZ"
  elif command -v systemsetup >/dev/null 2>&1; then
    timezone=$(systemsetup -gettimezone 2>/dev/null | awk -F': ' 'NF > 1 { print $2 }' || true)
  fi

  if [[ -z "$timezone" && -L /etc/localtime ]]; then
    timezone=$(readlink /etc/localtime | sed 's|^.*/zoneinfo/||' || true)
  fi

  if [[ -z "$timezone" && -f /etc/timezone ]]; then
    timezone=$(tr -d '[:space:]' </etc/timezone)
  fi

  if [[ -z "$timezone" ]]; then
    timezone="America/New_York"
  fi

  printf '%s' "$timezone"
}

profile_enabled() {
  local target="$1"
  local profiles_csv="$2"

  [[ ",${profiles_csv}," == *",${target},"* ]]
}

ensure_commands() {
  local command_name
  for command_name in docker awk sed cp chmod mktemp id python3 curl; do
    command -v "$command_name" >/dev/null 2>&1 || die "Missing required command: $command_name"
  done

  docker compose version >/dev/null 2>&1 || die "docker compose is not available"
}

ensure_root_env() {
  local data_root
  local config_root
  local user_id
  local group_id

  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_TEMPLATE" "$ENV_FILE"
    log "Created .env from .env.example"
  fi

  user_id="${USER_ID:-$(id -u)}"
  group_id="${GROUP_ID:-$(id -g)}"
  if [[ -f /.dockerenv && "$user_id" == "0" ]]; then
    user_id="1000"
  fi
  if [[ -f /.dockerenv && "$group_id" == "0" ]]; then
    group_id="1000"
  fi

  set_if_missing_or_default "$ENV_FILE" "USER_ID" "1000" "$user_id"
  set_if_missing_or_default "$ENV_FILE" "GROUP_ID" "1000" "$group_id"
  set_if_missing_or_default "$ENV_FILE" "TIMEZONE" "America/New_York" "$(detect_timezone)"
  set_if_missing_or_default "$ENV_FILE" "CONFIG_ROOT" "." "./runtime"
  set_if_missing_or_default "$ENV_FILE" "TSDPROXY_DASHBOARD_PORT" "" "8080"
  set_if_missing_or_default "$ENV_FILE" "TSDPROXY_AUTHKEY_PATH" "" "./secrets/tsdproxy_authkey"
  set_if_missing_or_default "$ENV_FILE" "TSDPROXY_DISABLE_TLS" "" "false"
  set_if_missing_or_default "$ENV_FILE" "TSDPROXY_WAKE_CHECK_INTERVAL" "" "30"
  set_if_missing_or_default "$ENV_FILE" "TSDPROXY_WAKE_THRESHOLD_SECONDS" "" "120"
  set_if_missing_or_default "$ENV_FILE" "TSDPROXY_WAKE_GRACE_SECONDS" "" "15"

  data_root=$(get_env_value "$ENV_FILE" "DATA_ROOT" || true)
  if [[ -n "$data_root" ]]; then
    set_if_missing_or_default "$ENV_FILE" "DOWNLOAD_ROOT" "/mnt/data/torrents" "${data_root%/}/torrents"
  fi

  config_root=$(get_env_value "$ENV_FILE" "CONFIG_ROOT" || true)
  if [[ -n "$data_root" && "$config_root" == "${data_root%/}"* ]]; then
    warn "CONFIG_ROOT is inside DATA_ROOT. Keep app config/databases on a local POSIX filesystem such as ./runtime; removable media can create AppleDouble sidecar files that break XML/SQLite readers."
  elif [[ "$config_root" == /Volumes/* ]]; then
    warn "CONFIG_ROOT is on /Volumes. For reliable app databases and XML keyrings, prefer a local path such as ./runtime and keep only media/download data on external storage."
  fi

  normalize_env_value_quotes "$ENV_FILE" "DATA_ROOT"
  normalize_env_value_quotes "$ENV_FILE" "DOWNLOAD_ROOT"
  normalize_env_value_quotes "$ENV_FILE" "CONFIG_ROOT"
  prepare_homepage_config
}

apply_root_overrides() {
  local entry
  local key
  local value

  if [[ -n "$PROFILES_OVERRIDE" ]]; then
    set_env_value "$ENV_FILE" "COMPOSE_PROFILES" "$PROFILES_OVERRIDE"
    log "Set COMPOSE_PROFILES=$PROFILES_OVERRIDE"
  fi

  if (( ${#SET_OVERRIDES[@]} > 0 )); then
    for entry in "${SET_OVERRIDES[@]}"; do
      key="${entry%%=*}"
      value="${entry#*=}"
      [[ -n "$key" ]] || die "Invalid override: $entry"
      set_env_value "$ENV_FILE" "$key" "$value"
      log "Set $key=$value"
    done
  fi
}

remove_obsolete_root_env() {
  local tmp_file
  local profiles
  local normalized_profiles

  tmp_file=$(mktemp)
  awk -F= '
    BEGIN {
      split("PUBLIC_HOSTNAME PUBLIC_SCHEME BASE_HOSTNAME HOSTNAME TRAEFIK_CERT_RESOLVER FORCE_HTTPS LOCAL_TLS_HOSTS MDNS_ENABLED MDNS_ALIASES MDNS_ADVERTISE_IP GENERATED_MDNS_ALIASES HOMEPAGE_HOSTNAME HOMEPAGE_ALIAS_HOSTNAME SONARR_HOSTNAME RADARR_HOSTNAME PROWLARR_HOSTNAME QBITTORRENT_HOSTNAME JELLYFIN_HOSTNAME SEERR_HOSTNAME HOMEASSISTANT_HOSTNAME IMMICH_HOSTNAME ADGUARD_HOSTNAME DNS_CHALLENGE DNS_CHALLENGE_PROVIDER LETS_ENCRYPT_CA_SERVER LETS_ENCRYPT_EMAIL CLOUDFLARE_EMAIL CLOUDFLARE_DNS_API_TOKEN CLOUDFLARE_ZONE_API_TOKEN TSDPROXY_AUTHKEYFILE TSDPROXY_CONTROLURL TSDPROXY_EXPOSE_DASHBOARD TSDPROXY_DASHBOARD_NAME", keys, " ")
      for (key_index in keys) {
        obsolete[keys[key_index]] = 1
      }
    }
    !($1 in obsolete) { print }
  ' "$ENV_FILE" > "$tmp_file"
  mv "$tmp_file" "$ENV_FILE"

  profiles=$(get_env_value "$ENV_FILE" "COMPOSE_PROFILES" || true)
  normalized_profiles=$(printf '%s' "$profiles" | tr ',' '\n' | awk 'NF && $0 != "tsdproxy" && !seen[$0]++' | paste -sd, -)
  if [[ "$normalized_profiles" != "$profiles" ]]; then
    set_env_value "$ENV_FILE" "COMPOSE_PROFILES" "$normalized_profiles"
    log "Removed obsolete tsdproxy profile; TSDProxy is now always enabled"
  fi
}

ensure_tsdproxy_authkey_secret() {
  local authkey_path
  local legacy_authkey

  authkey_path=$(get_env_value "$ENV_FILE" "TSDPROXY_AUTHKEY_PATH" || printf './secrets/tsdproxy_authkey')
  if [[ "$authkey_path" != /* ]]; then
    authkey_path="$ROOT_DIR/${authkey_path#./}"
  fi

  mkdir -p "$(dirname "$authkey_path")"
  chmod 700 "$(dirname "$authkey_path")"

  legacy_authkey=$(get_env_value "$ENV_FILE" "TSDPROXY_AUTHKEY" || true)
  if [[ ! -s "$authkey_path" && -n "$legacy_authkey" ]]; then
    printf '%s\n' "$legacy_authkey" > "$authkey_path"
    chmod 600 "$authkey_path"
    log "Migrated TSDPROXY_AUTHKEY from .env to ${authkey_path#$ROOT_DIR/}"
  elif [[ ! -e "$authkey_path" ]]; then
    : > "$authkey_path"
    chmod 600 "$authkey_path"
    warn "Created empty ${authkey_path#$ROOT_DIR/}; add a reusable Tailscale auth key before starting TSDProxy"
  fi

  set_env_value "$ENV_FILE" "TSDPROXY_AUTHKEY" ""
  remove_obsolete_root_env
  tmp_file=$(mktemp)
  awk -F= '$1 != "TSDPROXY_AUTHKEY" { print }' "$ENV_FILE" > "$tmp_file"
  mv "$tmp_file" "$ENV_FILE"
}

clean_appledouble_files() {
  local config_root

  config_root=$(get_config_root)

  find "$config_root" -name '._*' -delete 2>/dev/null || true
}

ensure_seerr_config_permissions() {
  local seerr_config_dir

  seerr_config_dir="$(get_config_root)/seerr"

  mkdir -p "$seerr_config_dir/logs"
  chmod -R a+rwX "$seerr_config_dir"
}

repair_seerr_config_permissions_with_image() {
  if docker compose run --rm --no-deps --label tsdproxy.enable=false --user root --entrypoint sh seerr -lc \
    'mkdir -p /app/config/logs && chmod -R a+rwX /app/config' >/dev/null 2>&1; then
    log "Repaired Seerr config volume permissions"
  else
    warn "Seerr config volume permission repair used host chmod fallback only"
  fi
}

provision_service_envs() {
  local profiles_csv="$1"
  local timezone_value="$2"
  local config_root

  config_root="$(get_config_root)"

  if profile_enabled "tandoor" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/tandoor/.env.example" "$config_root/tandoor/.env"
    set_if_missing_or_default "$config_root/tandoor/.env" "TZ" "America/New_York" "$timezone_value"
    set_generated_secret_if_blank "$config_root/tandoor/.env" "SECRET_KEY"
  fi

  if profile_enabled "vaultwarden" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/vaultwarden/.env.example" "$config_root/vaultwarden/.env"
  fi

  if profile_enabled "tandoor-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/tandoor/backup.env.example" "$config_root/tandoor/backup.env"
    set_if_missing_or_default "$config_root/tandoor/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

  if profile_enabled "vaultwarden-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/vaultwarden/backup.env.example" "$config_root/vaultwarden/backup.env"
    set_if_missing_or_default "$config_root/vaultwarden/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

}

validate_compose() {
  (cd "$ROOT_DIR" && docker compose config --quiet)
  (cd "$ROOT_DIR" && python3 ./scripts/validate-access-config.py)
  log "Compose configuration is valid"
}

wait_for_stack() {
  local deadline
  local container_id
  local status_line
  local status_value
  local pending_count

  deadline=$((SECONDS + WAIT_TIMEOUT))

  while (( SECONDS < deadline )); do
    pending_count=0

    while IFS= read -r container_id; do
      [[ -n "$container_id" ]] || continue
      status_line=$(docker inspect --format '{{.Name}} {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")
      status_value="${status_line##* }"

      case "$status_value" in
        healthy|running)
          ;;
        starting|created)
          pending_count=$((pending_count + 1))
          ;;
        *)
          die "Container status check failed: $status_line"
          ;;
      esac
    done < <(cd "$ROOT_DIR" && docker compose ps -q)

    if (( pending_count == 0 )); then
      log "All running compose services are healthy"
      return 0
    fi

    sleep 5
  done

  die "Timed out waiting for compose services to become healthy"
}

print_remaining_manual_steps() {
  local pia_user
  local pia_pass
  local tailnet_domain

  pia_user=$(get_env_value "$ENV_FILE" "PIA_USER" || true)
  pia_pass=$(get_env_value "$ENV_FILE" "PIA_PASS" || true)
  tailnet_domain=$(get_env_value "$ENV_FILE" "TAILNET_DOMAIN" || true)

  printf '\n'
  log "Remaining manual setup"

  if [[ -z "$pia_user" || -z "$pia_pass" ]]; then
    printf '  - Set PIA_USER and PIA_PASS in .env before relying on the VPN-backed qBittorrent path.\n'
  fi

  if [[ -z "$tailnet_domain" ]]; then
    printf '  - Set TAILNET_DOMAIN in .env to the DNS suffix shown in the Tailscale admin console.\n'
  fi
}

while (( $# > 0 )); do
  case "$1" in
    --profiles)
      [[ $# -ge 2 ]] || die "--profiles requires a value"
      PROFILES_OVERRIDE="$2"
      shift 2
      ;;
    --set)
      [[ $# -ge 2 ]] || die "--set requires KEY=VALUE"
      [[ "$2" == *=* ]] || die "--set requires KEY=VALUE"
      SET_OVERRIDES+=("$2")
      shift 2
      ;;
    --wait-timeout)
      [[ $# -ge 2 ]] || die "--wait-timeout requires a value"
      WAIT_TIMEOUT="$2"
      shift 2
      ;;
    --no-up)
      SKIP_UP=1
      shift
      ;;
    --no-bootstrap)
      SKIP_BOOTSTRAP=1
      shift
      ;;
    --no-connections)
      SKIP_CONNECTIONS=1
      shift
      ;;
    --no-wait)
      SKIP_WAIT=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

ensure_commands
ensure_root_env
apply_root_overrides
sync_tsdproxy_access_mode
ensure_tsdproxy_authkey_secret
ensure_seerr_config_permissions
clean_appledouble_files

ACTIVE_PROFILES=$(get_env_value "$ENV_FILE" "COMPOSE_PROFILES" || true)
TIMEZONE_VALUE=$(get_env_value "$ENV_FILE" "TIMEZONE" || printf 'America/New_York')
provision_service_envs "$ACTIVE_PROFILES" "$TIMEZONE_VALUE"
validate_compose

if (( SKIP_UP == 0 )); then
  log "Starting compose stack"
  (cd "$ROOT_DIR" && docker compose up -d --scale stack-setup=0)
  repair_seerr_config_permissions_with_image
fi

if (( SKIP_BOOTSTRAP == 0 )); then
  log "Running first-run post-start configuration"
  (cd "$ROOT_DIR" && ./scripts/update-config.sh)
fi

if (( SKIP_WAIT == 0 )); then
  wait_for_stack
fi

if (( SKIP_CONNECTIONS == 0 )); then
  log "Automating app-to-app connections"
  (cd "$ROOT_DIR" && python3 ./scripts/configure-app-connections.py)
fi

if (( SKIP_WAIT == 0 )); then
  wait_for_stack
fi

if ! (( SKIP_UP == 1 && SKIP_BOOTSTRAP == 1 && SKIP_CONNECTIONS == 1 && SKIP_WAIT == 1 )); then
  print_setup_complete_banner
  print_remaining_manual_steps "$ACTIVE_PROFILES"
fi
