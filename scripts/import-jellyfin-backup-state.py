#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
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

PROVIDER_PRIORITY = ("Imdb", "Tmdb", "Tvdb", "MusicBrainzAlbum", "MusicBrainzArtist", "wikidata")


def normalize_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def path_leaf(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\\", "/").rstrip("/").split("/")[-1].casefold()


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


def latest_datetime(existing: str | None, incoming: str | None) -> str | None:
    existing_dt = parse_datetime(existing)
    incoming_dt = parse_datetime(incoming)
    if existing_dt and incoming_dt:
        return incoming if incoming_dt > existing_dt else existing
    return incoming or existing


def bool_int(value: Any) -> int:
    return 1 if value is True or value == 1 else 0


def load_export(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def fetch_items(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT Id, Type, Name, Path, DateCreated, DateLastMediaAdded, PremiereDate, ProductionYear,
               RunTimeTicks, IndexNumber, ParentIndexNumber, SeriesName, SeasonName,
               PresentationUniqueKey, SeriesPresentationUniqueKey, Size
        FROM BaseItems
        """
    ).fetchall()
    return {row["Id"]: dict(row) for row in rows}


def fetch_providers(connection: sqlite3.Connection) -> dict[str, dict[str, str]]:
    providers: dict[str, dict[str, str]] = defaultdict(dict)
    rows = connection.execute("SELECT ItemId, ProviderId, ProviderValue FROM BaseItemProviders").fetchall()
    for row in rows:
        providers[row["ItemId"]][row["ProviderId"]] = row["ProviderValue"]
    return dict(providers)


def fetch_users(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT Id, Username FROM Users").fetchall()
    return {normalize_text(row["Username"]): dict(row) for row in rows}


class ItemMatcher:
    def __init__(self, items: dict[str, dict[str, Any]], providers: dict[str, dict[str, str]]) -> None:
        self.items = items
        self.providers = providers
        self.provider_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.path_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.movie_index: dict[tuple[str, str, Any], list[str]] = defaultdict(list)
        self.series_index: dict[tuple[str, str, Any], list[str]] = defaultdict(list)
        self.episode_index: dict[tuple[str, str, Any, Any], list[str]] = defaultdict(list)

        for item_id, item in items.items():
            item_type = item.get("Type")
            if item_type not in MEDIA_TYPES:
                continue
            for provider_id, provider_value in providers.get(item_id, {}).items():
                self.provider_index[(provider_id.casefold(), str(provider_value))].append(item_id)
            leaf = path_leaf(item.get("Path"))
            if leaf:
                self.path_index[(item_type, leaf)].append(item_id)
            if item_type == "MediaBrowser.Controller.Entities.Movies.Movie":
                self.movie_index[(item_type, normalize_text(item.get("Name")), item.get("ProductionYear"))].append(item_id)
            elif item_type == "MediaBrowser.Controller.Entities.TV.Series":
                self.series_index[(item_type, normalize_text(item.get("Name")), item.get("ProductionYear"))].append(item_id)
            elif item_type == "MediaBrowser.Controller.Entities.TV.Episode":
                self.episode_index[
                    (
                        item_type,
                        normalize_text(item.get("SeriesName")),
                        item.get("ParentIndexNumber"),
                        item.get("IndexNumber"),
                    )
                ].append(item_id)

    def unique_candidate(
        self,
        candidate_ids: list[str],
        source_type: str | None = None,
    ) -> tuple[str | None, str | None]:
        filtered = [
            item_id
            for item_id in candidate_ids
            if not source_type or source_type == "PLACEHOLDER" or self.items[item_id].get("Type") == source_type
        ]
        unique = sorted(set(filtered))
        if len(unique) == 1:
            return unique[0], None
        if len(unique) > 1:
            return None, f"ambiguous candidates: {', '.join(unique[:5])}"
        return None, None

    def by_provider_ids(self, source_item: dict[str, Any]) -> tuple[str | None, str | None]:
        source_type = source_item.get("Type")
        provider_ids = source_item.get("ProviderIds") or {}
        ordered_provider_ids = sorted(
            provider_ids,
            key=lambda name: PROVIDER_PRIORITY.index(name) if name in PROVIDER_PRIORITY else len(PROVIDER_PRIORITY),
        )
        for provider_id in ordered_provider_ids:
            provider_value = provider_ids.get(provider_id)
            item_id, error = self.unique_candidate(
                self.provider_index.get((provider_id.casefold(), str(provider_value)), []),
                source_type,
            )
            if item_id or error:
                return item_id, f"provider {provider_id}={provider_value}" if item_id else error
        return None, None

    def by_custom_data_key_matches(self, row: dict[str, Any]) -> tuple[str | None, str | None]:
        custom_data_key = str(row.get("CustomDataKey") or "")
        for match in row.get("CustomDataKeyMatches") or []:
            provider_id = str(match.get("ProviderId") or "")
            if not provider_id or not custom_data_key:
                continue
            item_id, error = self.unique_candidate(self.provider_index.get((provider_id.casefold(), custom_data_key), []))
            if item_id or error:
                return item_id, f"custom data key provider {provider_id}={custom_data_key}" if item_id else error
        return None, None

    def by_fallbacks(self, source_item: dict[str, Any]) -> tuple[str | None, str | None]:
        source_type = source_item.get("Type")
        leaf = path_leaf(source_item.get("Path"))
        if source_type and leaf:
            item_id, error = self.unique_candidate(self.path_index.get((source_type, leaf), []), source_type)
            if item_id or error:
                return item_id, "path leaf" if item_id else error
        if source_type == "MediaBrowser.Controller.Entities.Movies.Movie":
            key = (source_type, normalize_text(source_item.get("Name")), source_item.get("ProductionYear"))
            item_id, error = self.unique_candidate(self.movie_index.get(key, []), source_type)
            if item_id or error:
                return item_id, "movie name/year" if item_id else error
        if source_type == "MediaBrowser.Controller.Entities.TV.Series":
            key = (source_type, normalize_text(source_item.get("Name")), source_item.get("ProductionYear"))
            item_id, error = self.unique_candidate(self.series_index.get(key, []), source_type)
            if item_id or error:
                return item_id, "series name/year" if item_id else error
        if source_type == "MediaBrowser.Controller.Entities.TV.Episode":
            key = (
                source_type,
                normalize_text(source_item.get("SeriesName")),
                source_item.get("ParentIndexNumber"),
                source_item.get("IndexNumber"),
            )
            item_id, error = self.unique_candidate(self.episode_index.get(key, []), source_type)
            if item_id or error:
                return item_id, "episode series/season/index" if item_id else error
        return None, None

    def match_item(self, source_item: dict[str, Any] | None, user_data_row: dict[str, Any] | None = None) -> tuple[str | None, str]:
        if source_item:
            item_id, reason = self.by_provider_ids(source_item)
            if item_id:
                return item_id, reason or "provider"
            if reason:
                return None, reason
        if user_data_row:
            item_id, reason = self.by_custom_data_key_matches(user_data_row)
            if item_id:
                return item_id, reason or "custom data key provider"
            if reason:
                return None, reason
        if source_item:
            item_id, reason = self.by_fallbacks(source_item)
            if item_id:
                return item_id, reason or "fallback"
            if reason:
                return None, reason
        return None, "no match"


def user_data_custom_keys(row: dict[str, Any], new_item_id: str) -> set[str]:
    keys = {new_item_id}
    custom_key = str(row.get("CustomDataKey") or "")
    source_item = row.get("Item") or {}
    if custom_key and custom_key != source_item.get("Id"):
        keys.add(custom_key)
    return keys


def upsert_user_data(
    connection: sqlite3.Connection,
    item_id: str,
    user_id: str,
    custom_data_key: str,
    row: dict[str, Any],
) -> str:
    existing = connection.execute(
        """
        SELECT AudioStreamIndex, IsFavorite, LastPlayedDate, Likes, PlayCount, PlaybackPositionTicks,
               Played, Rating, SubtitleStreamIndex, RetentionDate
        FROM UserData
        WHERE ItemId = ? AND UserId = ? AND CustomDataKey = ?
        """,
        (item_id, user_id, custom_data_key),
    ).fetchone()

    incoming = {
        "AudioStreamIndex": row.get("AudioStreamIndex"),
        "IsFavorite": bool_int(row.get("IsFavorite")),
        "LastPlayedDate": row.get("LastPlayedDate"),
        "Likes": row.get("Likes"),
        "PlayCount": int(row.get("PlayCount") or 0),
        "PlaybackPositionTicks": int(row.get("PlaybackPositionTicks") or 0),
        "Played": bool_int(row.get("Played")),
        "Rating": row.get("Rating"),
        "SubtitleStreamIndex": row.get("SubtitleStreamIndex"),
        "RetentionDate": row.get("RetentionDate"),
    }

    if existing:
        merged = {
            "AudioStreamIndex": incoming["AudioStreamIndex"]
            if incoming["AudioStreamIndex"] is not None
            else existing["AudioStreamIndex"],
            "IsFavorite": 1 if existing["IsFavorite"] or incoming["IsFavorite"] else 0,
            "LastPlayedDate": latest_datetime(existing["LastPlayedDate"], incoming["LastPlayedDate"]),
            "Likes": incoming["Likes"] if incoming["Likes"] is not None else existing["Likes"],
            "PlayCount": max(int(existing["PlayCount"] or 0), incoming["PlayCount"]),
            "PlaybackPositionTicks": max(
                int(existing["PlaybackPositionTicks"] or 0),
                incoming["PlaybackPositionTicks"],
            ),
            "Played": 1 if existing["Played"] or incoming["Played"] else 0,
            "Rating": incoming["Rating"] if incoming["Rating"] is not None else existing["Rating"],
            "SubtitleStreamIndex": incoming["SubtitleStreamIndex"]
            if incoming["SubtitleStreamIndex"] is not None
            else existing["SubtitleStreamIndex"],
            "RetentionDate": latest_datetime(existing["RetentionDate"], incoming["RetentionDate"]),
        }
        connection.execute(
            """
            UPDATE UserData
            SET AudioStreamIndex = ?, IsFavorite = ?, LastPlayedDate = ?, Likes = ?,
                PlayCount = ?, PlaybackPositionTicks = ?, Played = ?, Rating = ?,
                SubtitleStreamIndex = ?, RetentionDate = ?
            WHERE ItemId = ? AND UserId = ? AND CustomDataKey = ?
            """,
            (
                merged["AudioStreamIndex"],
                merged["IsFavorite"],
                merged["LastPlayedDate"],
                merged["Likes"],
                merged["PlayCount"],
                merged["PlaybackPositionTicks"],
                merged["Played"],
                merged["Rating"],
                merged["SubtitleStreamIndex"],
                merged["RetentionDate"],
                item_id,
                user_id,
                custom_data_key,
            ),
        )
        return "updated"

    connection.execute(
        """
        INSERT INTO UserData (
            ItemId, UserId, CustomDataKey, AudioStreamIndex, IsFavorite, LastPlayedDate,
            Likes, PlayCount, PlaybackPositionTicks, Played, Rating, SubtitleStreamIndex,
            RetentionDate
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            user_id,
            custom_data_key,
            incoming["AudioStreamIndex"],
            incoming["IsFavorite"],
            incoming["LastPlayedDate"],
            incoming["Likes"],
            incoming["PlayCount"],
            incoming["PlaybackPositionTicks"],
            incoming["Played"],
            incoming["Rating"],
            incoming["SubtitleStreamIndex"],
            incoming["RetentionDate"],
        ),
    )
    return "inserted"


def database_backup_path(database_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return database_path.with_name(f"{database_path.name}.before-jellyfin-state-import-{timestamp}.bak")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import exported Jellyfin date-added and play-state data into jellyfin.db.")
    parser.add_argument("export", type=Path, help="JSON produced by export-jellyfin-backup-state.py")
    parser.add_argument("database", type=Path, help="Target Jellyfin jellyfin.db")
    parser.add_argument("--apply", action="store_true", help="Actually write changes. Omit for dry-run.")
    parser.add_argument(
        "--map-user",
        action="append",
        default=[],
        metavar="OLD:NEW",
        help="Map an exported Jellyfin username to a target-server username. Can be used multiple times.",
    )
    args = parser.parse_args()

    export = load_export(args.export)
    connection = connect_database(args.database)
    items = fetch_items(connection)
    providers = fetch_providers(connection)
    users = fetch_users(connection)
    matcher = ItemMatcher(items, providers)
    user_map: dict[str, str] = {}
    for mapping in args.map_user:
        if ":" not in mapping:
            raise SystemExit(f"--map-user requires OLD:NEW, got {mapping!r}")
        old_username, new_username = mapping.split(":", 1)
        user_map[normalize_text(old_username)] = normalize_text(new_username)

    summary: dict[str, Any] = {
        "dry_run": not args.apply,
        "date_created_matched": 0,
        "date_created_changed": 0,
        "date_created_unmatched": 0,
        "date_created_ambiguous": 0,
        "play_rows_matched": 0,
        "play_rows_unmatched_item": 0,
        "play_rows_unmatched_user": 0,
        "play_custom_keys_inserted": 0,
        "play_custom_keys_updated": 0,
        "play_custom_keys_would_write": 0,
    }
    unmatched_examples: list[str] = []

    if args.apply:
        backup_path = database_backup_path(args.database)
        shutil.copy2(args.database, backup_path)
        summary["database_backup"] = str(backup_path)

    with connection:
        for source_item in export.get("MediaItems", []):
            source_date = source_item.get("DateCreated")
            if not source_date:
                continue
            item_id, reason = matcher.match_item(source_item)
            if not item_id:
                if reason.startswith("ambiguous"):
                    summary["date_created_ambiguous"] += 1
                else:
                    summary["date_created_unmatched"] += 1
                if len(unmatched_examples) < 10:
                    unmatched_examples.append(f"date: {source_item.get('Type')} {source_item.get('Name')} ({reason})")
                continue
            summary["date_created_matched"] += 1
            current_date = items[item_id].get("DateCreated")
            if current_date != source_date:
                summary["date_created_changed"] += 1
                if args.apply:
                    connection.execute("UPDATE BaseItems SET DateCreated = ? WHERE Id = ?", (source_date, item_id))

        for row in export.get("RawUserData", []):
            exported_username = normalize_text((row.get("User") or {}).get("Username"))
            user = users.get(user_map.get(exported_username, exported_username))
            if not user:
                summary["play_rows_unmatched_user"] += 1
                if len(unmatched_examples) < 10:
                    unmatched_examples.append(f"play: user {(row.get('User') or {}).get('Username')} not found")
                continue
            item_id, reason = matcher.match_item(row.get("Item"), row)
            if not item_id:
                summary["play_rows_unmatched_item"] += 1
                if len(unmatched_examples) < 10:
                    unmatched_examples.append(f"play: {row.get('CustomDataKey')} ({reason})")
                continue
            summary["play_rows_matched"] += 1
            for custom_data_key in user_data_custom_keys(row, item_id):
                if args.apply:
                    action = upsert_user_data(connection, item_id, user["Id"], custom_data_key, row)
                    summary[f"play_custom_keys_{action}"] += 1
                else:
                    summary["play_custom_keys_would_write"] += 1

        if not args.apply:
            connection.rollback()

    summary["unmatched_examples"] = unmatched_examples
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.apply:
        print("Dry-run only. Re-run with --apply after stopping Jellyfin to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
