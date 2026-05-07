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

Bootstrap the local Docker Compose NAS stack by creating missing env files,
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
  ./scripts/setup-stack.sh --profiles paperless,vaultwarden
  ./scripts/setup-stack.sh --set DATA_ROOT=/srv/data --set DOWNLOAD_ROOT=/srv/data/torrents
  ./scripts/setup-stack.sh --profiles immich,immich-backup --set IMMICH_HOSTNAME=photos.example.com
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
##                           SETUP COMPLETE                                 ##
##                                                                          ##
##                 Docker Compose NAS is fully configured.                  ##
##                                                                          ##
##                  You can now open the configured services.                ##
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

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp_file

  tmp_file=$(mktemp)
  awk -v key="$key" -v value="$value" '
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

set_if_missing_or_default() {
  local file="$1"
  local key="$2"
  local default_value="$3"
  local new_value="$4"
  local current_value

  current_value=$(get_env_value "$file" "$key" || true)
  if [[ -z "$current_value" || "$current_value" == "$default_value" ]]; then
    set_env_value "$file" "$key" "$new_value"
    log "Set $key=$new_value"
  fi
}

copy_if_missing() {
  local source_file="$1"
  local target_file="$2"

  if [[ -f "$target_file" ]]; then
    return 0
  fi

  cp "$source_file" "$target_file"
  log "Created ${target_file#$ROOT_DIR/} from template"
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

detect_local_hostname() {
  local detected=""

  if [[ -n "${NAS_HOST_HOSTNAME:-}" ]]; then
    detected="$NAS_HOST_HOSTNAME"
  fi

  if [[ -z "$detected" && -f /host/etc/hostname ]]; then
    detected=$(cat /host/etc/hostname 2>/dev/null || true)
  fi

  if [[ -z "$detected" ]] && command -v scutil >/dev/null 2>&1; then
    detected=$(scutil --get LocalHostName 2>/dev/null || true)
  fi

  if [[ -z "$detected" ]]; then
    detected=$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)
  fi

  detected="${detected%%.*}"
  detected=$(printf '%s' "$detected" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]//g')

  if [[ -z "$detected" ]]; then
    detected="localhost"
  elif [[ "$detected" != "localhost" ]]; then
    detected="${detected}.local"
  fi

  printf '%s' "$detected"
}

profile_enabled() {
  local target="$1"
  local profiles_csv="$2"

  [[ ",${profiles_csv}," == *",${target},"* ]]
}

ensure_commands() {
  local command_name
  for command_name in docker awk sed cp chmod mktemp id python3 curl openssl; do
    command -v "$command_name" >/dev/null 2>&1 || die "Missing required command: $command_name"
  done

  docker compose version >/dev/null 2>&1 || die "docker compose is not available"
}

ensure_root_env() {
  local local_hostname

  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_TEMPLATE" "$ENV_FILE"
    log "Created .env from .env.example"
  fi

  local_hostname=$(detect_local_hostname)
  set_if_missing_or_default "$ENV_FILE" "USER_ID" "1000" "$(id -u)"
  set_if_missing_or_default "$ENV_FILE" "GROUP_ID" "1000" "$(id -g)"
  set_if_missing_or_default "$ENV_FILE" "TIMEZONE" "America/New_York" "$(detect_timezone)"
  set_if_missing_or_default "$ENV_FILE" "HOSTNAME" "localhost" "$local_hostname"
  set_if_missing_or_default "$ENV_FILE" "BASE_HOSTNAME" "localhost" "$local_hostname"
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

ensure_acme_storage() {
  mkdir -p "$ROOT_DIR/letsencrypt"
  touch "$ROOT_DIR/letsencrypt/acme.json"
  chmod 600 "$ROOT_DIR/letsencrypt/acme.json"
}

ensure_seerr_config_permissions() {
  local config_root
  local seerr_config_dir

  config_root=$(get_env_value "$ENV_FILE" "CONFIG_ROOT" || printf '.')
  if [[ "$config_root" = /* ]]; then
    seerr_config_dir="$config_root/seerr"
  else
    seerr_config_dir="$ROOT_DIR/$config_root/seerr"
  fi

  mkdir -p "$seerr_config_dir/logs"
  chmod -R a+rwX "$seerr_config_dir"
}

repair_seerr_config_permissions_with_image() {
  if docker compose run --rm --no-deps --user root --entrypoint sh seerr -lc \
    'mkdir -p /app/config/logs && chmod -R a+rwX /app/config' >/dev/null 2>&1; then
    log "Repaired Seerr config volume permissions"
  else
    warn "Seerr config volume permission repair used host chmod fallback only"
  fi
}

ensure_local_tls_certificate() {
  local cert_dir="$ROOT_DIR/traefik/certs"
  local dynamic_dir="$ROOT_DIR/traefik/dynamic"
  local cert_file="$cert_dir/local.crt"
  local key_file="$cert_dir/local.key"
  local config_file="$cert_dir/local-openssl.cnf"
  local dynamic_file="$dynamic_dir/local-tls.yml"
  local host_name
  local extra_hosts
  local san_entries
  local index
  local host
  local extra_hosts_array=()

  host_name=$(get_env_value "$ENV_FILE" "HOSTNAME" || true)
  extra_hosts=$(get_env_value "$ENV_FILE" "LOCAL_TLS_HOSTS" || true)

  mkdir -p "$cert_dir" "$dynamic_dir"

  san_entries=("DNS.1 = localhost" "DNS.2 = *.local")
  index=3

  if [[ -n "$host_name" && "$host_name" != "localhost" ]]; then
    san_entries+=("DNS.${index} = ${host_name}")
    index=$((index + 1))
  fi

  if [[ -n "$extra_hosts" ]]; then
    IFS=',' read -r -a extra_hosts_array <<< "$extra_hosts"
    for host in "${extra_hosts_array[@]}"; do
      host=$(printf '%s' "$host" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      [[ -n "$host" ]] || continue
      san_entries+=("DNS.${index} = ${host}")
      index=$((index + 1))
    done
  fi

  {
    printf '[req]\n'
    printf 'distinguished_name = req_distinguished_name\n'
    printf 'x509_extensions = v3_req\n'
    printf 'prompt = no\n\n'
    printf '[req_distinguished_name]\n'
    printf 'CN = %s\n\n' "${host_name:-localhost}"
    printf '[v3_req]\n'
    printf 'keyUsage = keyEncipherment, dataEncipherment\n'
    printf 'extendedKeyUsage = serverAuth\n'
    printf 'subjectAltName = @alt_names\n\n'
    printf '[alt_names]\n'
    printf '%s\n' "${san_entries[@]}"
  } > "$config_file"

  if [[ ! -f "$cert_file" || ! -f "$key_file" ]] || ! openssl x509 -checkend 2592000 -noout -in "$cert_file" >/dev/null 2>&1; then
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
      -keyout "$key_file" \
      -out "$cert_file" \
      -config "$config_file" >/dev/null 2>&1
    chmod 600 "$key_file"
    log "Generated local Traefik TLS certificate for ${host_name:-localhost}"
  fi

  cat > "$dynamic_file" <<EOF
tls:
  stores:
    default:
      defaultCertificate:
        certFile: /traefik-certs/local.crt
        keyFile: /traefik-certs/local.key
EOF
}

provision_service_envs() {
  local profiles_csv="$1"
  local timezone_value="$2"

  if profile_enabled "tandoor" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/tandoor/.env.example" "$ROOT_DIR/tandoor/.env"
    set_if_missing_or_default "$ROOT_DIR/tandoor/.env" "TZ" "America/New_York" "$timezone_value"
    set_generated_secret_if_blank "$ROOT_DIR/tandoor/.env" "SECRET_KEY"
  fi

  if profile_enabled "joplin" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/joplin/.env.example" "$ROOT_DIR/joplin/.env"
  fi

  if profile_enabled "vaultwarden" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/vaultwarden/.env.example" "$ROOT_DIR/vaultwarden/.env"
  fi

  if profile_enabled "paperless" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/paperless/.env.example" "$ROOT_DIR/paperless/.env"
    set_if_missing_or_default "$ROOT_DIR/paperless/.env" "PAPERLESS_TIME_ZONE" "America/New_York" "$timezone_value"
    set_generated_secret_if_blank "$ROOT_DIR/paperless/.env" "PAPERLESS_SECRET_KEY"
  fi

  if profile_enabled "tandoor-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/tandoor/backup.env.example" "$ROOT_DIR/tandoor/backup.env"
    set_if_missing_or_default "$ROOT_DIR/tandoor/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

  if profile_enabled "joplin-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/joplin/backup.env.example" "$ROOT_DIR/joplin/backup.env"
    set_if_missing_or_default "$ROOT_DIR/joplin/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

  if profile_enabled "homeassistant-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/homeassistant/backup.env.example" "$ROOT_DIR/homeassistant/backup.env"
    set_if_missing_or_default "$ROOT_DIR/homeassistant/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

  if profile_enabled "vaultwarden-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/vaultwarden/backup.env.example" "$ROOT_DIR/vaultwarden/backup.env"
    set_if_missing_or_default "$ROOT_DIR/vaultwarden/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

  if profile_enabled "paperless-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/paperless/backup.env.example" "$ROOT_DIR/paperless/backup.env"
    set_if_missing_or_default "$ROOT_DIR/paperless/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi

  if profile_enabled "immich-backup" "$profiles_csv"; then
    copy_if_missing "$ROOT_DIR/immich/backup.env.example" "$ROOT_DIR/immich/backup.env"
    set_if_missing_or_default "$ROOT_DIR/immich/backup.env" "TIMEZONE" "America/New_York" "$timezone_value"
  fi
}

validate_compose() {
  (cd "$ROOT_DIR" && docker compose config --quiet)
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
  local profiles_csv="$1"
  local pia_user
  local pia_pass
  local traefik_resolver
  local host_name
  local immich_hostname
  local homeassistant_hostname
  local adguard_hostname

  pia_user=$(get_env_value "$ENV_FILE" "PIA_USER" || true)
  pia_pass=$(get_env_value "$ENV_FILE" "PIA_PASS" || true)
  traefik_resolver=$(get_env_value "$ENV_FILE" "TRAEFIK_CERT_RESOLVER" || true)
  host_name=$(get_env_value "$ENV_FILE" "HOSTNAME" || true)
  immich_hostname=$(get_env_value "$ENV_FILE" "IMMICH_HOSTNAME" || true)
  homeassistant_hostname=$(get_env_value "$ENV_FILE" "HOMEASSISTANT_HOSTNAME" || true)
  adguard_hostname=$(get_env_value "$ENV_FILE" "ADGUARD_HOSTNAME" || true)

  printf '\n'
  log "Remaining manual setup"

  if [[ -z "$pia_user" || -z "$pia_pass" ]]; then
    printf '  - Set PIA_USER and PIA_PASS in .env before relying on the VPN-backed qBittorrent path.\n'
  fi

  if [[ -n "$traefik_resolver" ]]; then
    printf '  - Fill your ACME/DNS provider credentials in .env for TRAEFIK_CERT_RESOLVER=%s.\n' "$traefik_resolver"
  fi

  if profile_enabled "immich" "$profiles_csv" && [[ -z "$immich_hostname" ]]; then
    printf '  - Set IMMICH_HOSTNAME in .env before using Immich behind Traefik.\n'
  fi

  if profile_enabled "homeassistant" "$profiles_csv" && [[ "$homeassistant_hostname" == *localhost* ]]; then
    printf '  - Replace HOMEASSISTANT_HOSTNAME with a resolvable hostname if you plan to use Home Assistant through Traefik.\n'
  fi

  if profile_enabled "adguardhome" "$profiles_csv" && [[ "$adguard_hostname" == *localhost* ]]; then
    printf '  - Replace ADGUARD_HOSTNAME with a resolvable hostname before enabling AdGuard TLS features.\n'
  fi

  if [[ "$host_name" == "localhost" ]]; then
    printf "  - Local access is available at https://localhost/ with Traefik's default certificate.\n"
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
ensure_acme_storage
ensure_seerr_config_permissions
ensure_local_tls_certificate

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

print_setup_complete_banner
print_remaining_manual_steps "$ACTIVE_PROFILES"
