#!/bin/bash

set -euo pipefail

log() {
  printf '[mdns] %s\n' "$1"
}

mdns_enabled="${MDNS_ENABLED:-true}"
if [[ "$mdns_enabled" =~ ^([Ff][Aa][Ll][Ss][Ee]|0|[Nn][Oo])$ ]]; then
  log "mDNS alias publishing is disabled"
  sleep infinity
fi

detect_name() {
  local name="${LOCAL_MDNS_HOSTNAME:-}"

  if [[ -z "$name" && -f /host/etc/hostname ]]; then
    name=$(cat /host/etc/hostname)
  fi

  name="${name%%.*}"
  name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]//g')

  if [[ -z "$name" || "$name" == "localhost" || "$name" == localhost.* || "$name" == *.localhost.local ]]; then
    return 1
  fi

  printf '%s.local' "$name"
}

normalize_name() {
  local name="$1"

  name="${name%%:*}"
  name="${name%.}"
  name=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9.-]//g')

  if [[ -z "$name" || "$name" == "localhost" || "$name" == localhost.* || "$name" == *.localhost.local ]]; then
    return 1
  fi

  if [[ "$name" != *.local ]]; then
    name="${name}.local"
  fi

  if [[ "$name" == "localhost.local" || "$name" == localhost.* || "$name" == *.localhost.local ]]; then
    return 1
  fi

  printf '%s' "$name"
}

detect_names() {
  local names="${LOCAL_MDNS_HOSTS:-}"
  local name
  local normalized

  IFS=',' read -r -a name_list <<< "$names"
  for name in "${name_list[@]}"; do
    normalized=$(normalize_name "$name" || true)
    [[ -n "$normalized" ]] || continue
    printf '%s\n' "$normalized"
  done | awk '!seen[$0]++'
}

detect_ip() {
  local ip="${MDNS_ADVERTISE_IP:-}"

  if [[ -n "$ip" ]]; then
    printf '%s' "$ip"
    return 0
  fi

  return 1
}

mapfile -t names < <(detect_names)

if (( ${#names[@]} == 0 )); then
  log "No .local hostnames to advertise"
  sleep infinity
fi

ip=$(detect_ip || true)
if [[ -z "$ip" ]]; then
  log "MDNS_ADVERTISE_IP is required before advertising aliases from a container"
  sleep infinity
fi

mkdir -p /run/dbus
rm -f /run/dbus/pid /run/dbus/dbus.pid
dbus-daemon --system --fork
avahi-daemon --no-chroot --daemonize

declare -A address_pids
declare -A service_pids

log "Publishing ${names[*]} at ${ip}"
while true; do
  for name in "${names[@]}"; do
    pid="${address_pids[$name]:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      :
    else
      avahi-publish -a -R "$name" "$ip" >/dev/null 2>&1 &
      address_pids[$name]=$!
      log "Advertising address ${name} at ${ip}"
    fi

    pid="${service_pids[$name]:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      continue
    fi
    avahi-publish -s -H "$name" "$name" _http._tcp 80 "path=/" >/dev/null 2>&1 &
    service_pids[$name]=$!
    log "Advertising HTTP service ${name} at ${name}:80"
  done
  sleep 60
done
