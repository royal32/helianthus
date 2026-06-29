#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEDIA_TYPES = {
    "MediaBrowser.Controller.Entities.Movies.Movie",
    "MediaBrowser.Controller.Entities.TV.Episode",
    "MediaBrowser.Controller.Entities.TV.Series",
    "MediaBrowser.Controller.Entities.TV.Season",
    "MediaBrowser.Controller.Entities.Audio.Audio",
}

ITEM_FIELDS = (
    "Id",
    "Type",
    "Name",
    "Path",
    "DateCreated",
    "DateModified",
    "DateLastMediaAdded",
    "DateLastRefreshed",
    "DateLastSaved",
    "PremiereDate",
    "ProductionYear",
    "RunTimeTicks",
    "IndexNumber",
    "ParentIndexNumber",
    "SeriesName",
    "SeasonName",
    "EpisodeTitle",
    "PresentationUniqueKey",
    "SeriesPresentationUniqueKey",
    "ParentId",
    "TopParentId",
    "SeriesId",
    "SeasonId",
    "SortName",
    "OriginalTitle",
    "ExternalId",
    "ExternalSeriesId",
    "Size",
    "Width",
    "Height",
)

USER_FIELDS = (
    "Id",
    "Username",
    "InternalId",
    "LastActivityDate",
    "LastLoginDate",
)

USER_DATA_FIELDS = (
    "CustomDataKey",
    "ItemId",
    "UserId",
    "Rating",
    "PlaybackPositionTicks",
    "PlayCount",
    "IsFavorite",
    "LastPlayedDate",
    "Played",
    "AudioStreamIndex",
    "SubtitleStreamIndex",
    "Likes",
    "RetentionDate",
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def path_leaf(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("\\", "/").rstrip("/").split("/")[-1]


def compact_dict(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        if "." in normalized:
            prefix, suffix = normalized.split(".", 1)
            timezone_suffix = "+00:00" if normalized.endswith("+00:00") else ""
            fractional = suffix.removesuffix("+00:00")[:6]
            try:
                return datetime.fromisoformat(f"{prefix}.{fractional}{timezone_suffix}")
            except ValueError:
                return None
        return None


def playback_sort_key(row: dict[str, Any]) -> tuple[datetime, int, int]:
    last_played = parse_datetime(row.get("LastPlayedDate")) or datetime.min.replace(tzinfo=timezone.utc)
    return (
        last_played,
        int(row.get("PlayCount") or 0),
        int(row.get("PlaybackPositionTicks") or 0),
    )


def provider_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    providers: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        item_id = row.get("ItemId")
        provider_id = row.get("ProviderId")
        provider_value = row.get("ProviderValue")
        if item_id and provider_id and provider_value:
            providers[item_id][provider_id] = provider_value
    return dict(providers)


def provider_value_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        item_id = row.get("ItemId")
        provider_id = row.get("ProviderId")
        provider_value = row.get("ProviderValue")
        if item_id and provider_id and provider_value:
            index[provider_value].append({"ItemId": item_id, "ProviderId": provider_id})
    return dict(index)


def build_item_export(item: dict[str, Any], providers: dict[str, dict[str, str]]) -> dict[str, Any]:
    exported = compact_dict(item, ITEM_FIELDS)
    exported["PathLeaf"] = path_leaf(item.get("Path"))
    exported["ProviderIds"] = providers.get(item.get("Id"), {})
    return exported


def relevant_user_data(row: dict[str, Any]) -> bool:
    return any(
        (
            row.get("Played") is True,
            int(row.get("PlayCount") or 0) > 0,
            int(row.get("PlaybackPositionTicks") or 0) > 0,
            row.get("LastPlayedDate"),
            row.get("IsFavorite") is True,
            row.get("Rating") is not None,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Jellyfin backup date-added and play-state data for later database patching."
    )
    parser.add_argument("backup", type=Path, help="Jellyfin backup directory containing Database/*.json")
    parser.add_argument("output", type=Path, help="Output JSON file")
    args = parser.parse_args()

    database = args.backup / "Database"
    manifest_path = args.backup / "manifest.json"
    items = load_json(database / "BaseItems.json")
    users = load_json(database / "Users.json")
    user_data = load_json(database / "UserData.json")
    provider_rows = load_json(database / "BaseItemProviders.json")
    providers = provider_map(provider_rows)
    providers_by_value = provider_value_index(provider_rows)
    manifest = load_json(manifest_path) if manifest_path.exists() else {}

    items_by_id = {item.get("Id"): item for item in items if item.get("Id")}
    media_items = [build_item_export(item, providers) for item in items if item.get("Type") in MEDIA_TYPES]
    users_by_id = {user.get("Id"): compact_dict(user, USER_FIELDS) for user in users if user.get("Id")}

    raw_user_data = []
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}
    aggregate_source_rows: dict[tuple[str, str], int] = defaultdict(int)
    placeholder_user_data_rows = 0
    unmatched_user_data_rows = 0

    for row in user_data:
        if not relevant_user_data(row):
            continue

        item = items_by_id.get(row.get("ItemId"))
        if not item:
            unmatched_user_data_rows += 1
        elif item.get("Type") == "PLACEHOLDER":
            placeholder_user_data_rows += 1

        exported = compact_dict(row, USER_DATA_FIELDS)
        exported["CustomDataKeyMatches"] = providers_by_value.get(str(row.get("CustomDataKey") or ""), [])
        exported["User"] = users_by_id.get(row.get("UserId"), {})
        exported["Item"] = build_item_export(item, providers) if item else None
        raw_user_data.append(exported)

        item_key = str(row.get("ItemId") or "")
        if item and item.get("Type") == "PLACEHOLDER":
            item_key = f"{item_key}:{row.get('CustomDataKey') or ''}"
        key = (str(row.get("UserId") or ""), item_key)
        aggregate_source_rows[key] += 1
        current = aggregate.get(key)
        if current is None or playback_sort_key(row) > playback_sort_key(current):
            aggregate[key] = row

    play_state_by_user_item = []
    for (user_id, aggregate_item_key), row in sorted(aggregate.items()):
        item = items_by_id.get(row.get("ItemId"))
        exported = compact_dict(row, USER_DATA_FIELDS)
        exported["AggregateItemKey"] = aggregate_item_key
        exported["SourceRowCount"] = aggregate_source_rows[(user_id, aggregate_item_key)]
        exported["CustomDataKeyMatches"] = providers_by_value.get(str(row.get("CustomDataKey") or ""), [])
        exported["User"] = users_by_id.get(user_id, {})
        exported["Item"] = build_item_export(item, providers) if item else None
        play_state_by_user_item.append(exported)

    export = {
        "ExportedAt": datetime.now(timezone.utc).isoformat(),
        "SourceBackup": str(args.backup),
        "Manifest": manifest,
        "Notes": [
            "Date-added data comes from BaseItems.DateCreated.",
            "Play-state data is preserved as raw UserData rows and as a de-duplicated per-user/per-item view.",
            "Future patching should match new-server items by ProviderIds first, then path leaf/name/year/index fallback.",
        ],
        "Summary": {
            "BaseItemRows": len(items),
            "ExportedMediaItemRows": len(media_items),
            "UserRows": len(users),
            "RelevantRawUserDataRows": len(raw_user_data),
            "DeduplicatedPlayStateRows": len(play_state_by_user_item),
            "PlaceholderUserDataRows": placeholder_user_data_rows,
            "UnmatchedUserDataRows": unmatched_user_data_rows,
        },
        "Users": list(users_by_id.values()),
        "MediaItems": media_items,
        "RawUserData": raw_user_data,
        "PlayStateByUserItem": play_state_by_user_item,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(export, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.output}")
    print(json.dumps(export["Summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
