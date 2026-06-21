#!/usr/bin/env bash
set -euo pipefail

WATCH_ROOT="${WATCH_ROOT:-/data/media}"
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-120}"
SCAN_INTERVAL_SECONDS="${SCAN_INTERVAL_SECONDS:-21600}"
STABILITY_CHECKS="${STABILITY_CHECKS:-2}"
STABILITY_INTERVAL_SECONDS="${STABILITY_INTERVAL_SECONDS:-10}"

log() {
  printf '[audio-defaults] %s\n' "$*"
}

stable_file() {
  local file="$1"
  local previous_size=""
  local stable_checks=0

  while [ "$stable_checks" -lt "$STABILITY_CHECKS" ]; do
    if [ ! -f "$file" ]; then
      return 1
    fi

    local current_size
    current_size="$(stat -c '%s' "$file")"
    if [ "$current_size" = "$previous_size" ] && [ "$current_size" -gt 0 ]; then
      stable_checks="$((stable_checks + 1))"
    else
      stable_checks=0
      previous_size="$current_size"
    fi
    sleep "$STABILITY_INTERVAL_SECONDS"
  done
}

normalize_mkv() {
  local file="$1"
  stable_file "$file" || return 0

  local decision
  if ! decision="$(
    mkvmerge -J "$file" | jq -cer '
      def normalized_language($track):
        (($track.properties.language_ietf // $track.properties.language // "") | ascii_downcase);
      def is_english($track):
        (normalized_language($track) | startswith("en")) or (($track.properties.language // "" | ascii_downcase) == "eng");
      def track_name($track):
        ($track.properties.track_name // "" | ascii_downcase);
      def is_commentary($track):
        (track_name($track) | test("commentary|comment|descriptive|description"));
      def is_hearing_impaired($track):
        (($track.properties.flag_hearing_impaired // false) == true) or (track_name($track) | test("\\b(sdh|hi|hearing impaired)\\b"));
      def is_forced($track):
        (($track.properties.forced_track // false) == true) or (track_name($track) | test("\\bforced\\b"));

      [.tracks[] | select(.type == "audio")] as $audio
      | [.tracks[] | select(.type == "subtitles")] as $subtitles
      | ($audio | to_entries | map(select(is_english(.value) and (is_commentary(.value) | not))) | first)
        // ($audio | to_entries | map(select(is_english(.value))) | first) as $chosen
      | if $chosen == null then
          (
            ($subtitles | to_entries | map(select(is_english(.value) and (is_forced(.value) | not) and (is_hearing_impaired(.value) | not))) | first)
            // ($subtitles | to_entries | map(select(is_english(.value) and (is_hearing_impaired(.value) | not))) | first)
            // ($subtitles | to_entries | map(select(is_english(.value))) | first)
          ) as $subtitle
          | {
              english: false,
              audio_count: ($audio | length),
              defaults: ($audio | to_entries | map(select(.value.properties.default_track == true) | .key + 1)),
              subtitle_count: ($subtitles | length),
              subtitle: (if $subtitle == null then null else ($subtitle.key + 1) end),
              subtitle_defaults: ($subtitles | to_entries | map(select(.value.properties.default_track == true) | .key + 1)),
              non_english_forced_subtitles: ($subtitles | to_entries | map(select((is_english(.value) | not) and is_forced(.value)) | .key + 1))
            }
        else
          (
            ($subtitles | to_entries | map(select(is_english(.value) and is_forced(.value) and (is_hearing_impaired(.value) | not))) | first)
            // ($subtitles | to_entries | map(select(is_english(.value) and is_forced(.value))) | first)
          ) as $subtitle
          |
          {
            english: true,
            chosen: ($chosen.key + 1),
            audio_count: ($audio | length),
            defaults: ($audio | to_entries | map(select(.value.properties.default_track == true) | .key + 1)),
            subtitle_count: ($subtitles | length),
            subtitle: (if $subtitle == null then null else ($subtitle.key + 1) end),
            subtitle_defaults: ($subtitles | to_entries | map(select(.value.properties.default_track == true) | .key + 1)),
            non_english_forced_subtitles: ($subtitles | to_entries | map(select((is_english(.value) | not) and is_forced(.value)) | .key + 1))
          }
        end
    '
  )"; then
    log "Skipping unreadable MKV metadata: $file"
    return 0
  fi

  local chosen audio_count defaults
  local command=(mkvpropedit "$file")
  local changed=0
  chosen="$(jq -r '.chosen' <<<"$decision")"
  audio_count="$(jq -r '.audio_count' <<<"$decision")"
  defaults="$(jq -r '.defaults | join(",")' <<<"$decision")"

  if [ "$(jq -r '.english' <<<"$decision")" = "true" ] && [ "$defaults" != "$chosen" ]; then
    local index
    for index in $(seq 1 "$audio_count"); do
      command+=(--edit "track:a${index}" --set "flag-default=0")
    done
    command+=(--edit "track:a${chosen}" --set "flag-default=1")
    changed=1
  fi

  local subtitle subtitle_count subtitle_defaults non_english_forced_subtitles
  subtitle="$(jq -r '.subtitle // ""' <<<"$decision")"
  subtitle_count="$(jq -r '.subtitle_count' <<<"$decision")"
  subtitle_defaults="$(jq -r '.subtitle_defaults | join(",")' <<<"$decision")"
  non_english_forced_subtitles="$(jq -r '.non_english_forced_subtitles | join(" ")' <<<"$decision")"

  if [ "$subtitle_defaults" != "$subtitle" ]; then
    local subtitle_index
    for subtitle_index in $(seq 1 "$subtitle_count"); do
      command+=(--edit "track:s${subtitle_index}" --set "flag-default=0")
    done
    if [ -n "$subtitle" ]; then
      command+=(--edit "track:s${subtitle}" --set "flag-default=1")
    fi
    changed=1
  fi

  if [ -n "$non_english_forced_subtitles" ]; then
    local forced_subtitle
    for forced_subtitle in $non_english_forced_subtitles; do
      command+=(--edit "track:s${forced_subtitle}" --set "flag-forced=0")
    done
    changed=1
  fi

  if [ "$changed" -eq 0 ]; then
    return 0
  fi

  log "Normalizing audio/subtitle defaults: $file"
  "${command[@]}"
}

scan_library() {
  if [ ! -d "$WATCH_ROOT" ]; then
    log "Watch root does not exist yet: $WATCH_ROOT"
    return 0
  fi

  find "$WATCH_ROOT" -type f -iname '*.mkv' -print0 |
    while IFS= read -r -d '' file; do
      normalize_mkv "$file"
    done
}

log "Watching $WATCH_ROOT for MKV files with English audio"
scan_library

while true; do
  if inotifywait -r -q -e close_write,moved_to,create,attrib -t "$SCAN_INTERVAL_SECONDS" "$WATCH_ROOT" >/dev/null 2>&1; then
    sleep "$DEBOUNCE_SECONDS"
    scan_library
  else
    scan_library
  fi
done
