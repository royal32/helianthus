from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "configure-app-connections.py"
SPEC = importlib.util.spec_from_file_location("reconciler", MODULE_PATH)
assert SPEC and SPEC.loader
reconciler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconciler
SPEC.loader.exec_module(reconciler)


class ReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_root = reconciler.ROOT_DIR
        self.temporary = tempfile.TemporaryDirectory()
        reconciler.ROOT_DIR = Path(self.temporary.name)

    def tearDown(self) -> None:
        reconciler.ROOT_DIR = self.original_root
        self.temporary.cleanup()

    def test_generated_keys_are_discovered_without_modifying_env(self) -> None:
        root = reconciler.ROOT_DIR
        (root / "runtime/sonarr").mkdir(parents=True)
        (root / "runtime/radarr").mkdir(parents=True)
        (root / "runtime/prowlarr").mkdir(parents=True)
        (root / "runtime/seerr").mkdir(parents=True)
        for service, key in (("sonarr", "sonarr-key"), ("radarr", "radarr-key"), ("prowlarr", "prowlarr-key")):
            (root / f"runtime/{service}/config.xml").write_text(f"<Config><ApiKey>{key}</ApiKey></Config>")
        (root / "runtime/seerr/settings.json").write_text(
            json.dumps({"main": {"apiKey": "seerr-key"}, "jellyfin": {"apiKey": "jellyfin-key"}})
        )
        env_path = root / ".env"
        env_path.write_text("GLOBAL_PASSWORD=example\n")
        before = env_path.read_bytes()

        discovered = reconciler.discover_generated_api_keys({"CONFIG_ROOT": "./runtime"})

        self.assertEqual(discovered["SONARR_API_KEY"], "sonarr-key")
        self.assertEqual(discovered["SEERR_API_KEY"], "seerr-key")
        self.assertEqual(env_path.read_bytes(), before)

    def test_reconciler_config_root_override_supports_container_mount(self) -> None:
        mounted_root = reconciler.ROOT_DIR / "mounted-runtime"
        with mock.patch.dict("os.environ", {"RECONCILER_CONFIG_ROOT": str(mounted_root)}):
            self.assertEqual(
                reconciler.get_config_root({"CONFIG_ROOT": "/host/absolute/runtime"}),
                mounted_root,
            )

    def test_atomic_write_is_idempotent(self) -> None:
        path = reconciler.ROOT_DIR / "generated/config"
        self.assertTrue(reconciler.write_text_if_changed(path, "value\n", False, mode=0o600))
        self.assertFalse(reconciler.write_text_if_changed(path, "value\n", False, mode=0o600))
        self.assertEqual(path.read_text(), "value\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_qbittorrent_hash_reuses_matching_hash(self) -> None:
        first = reconciler.qbit_password_hash("password")
        second = reconciler.qbit_password_hash("password", first)
        self.assertEqual(first, second)
        self.assertNotEqual(first, reconciler.qbit_password_hash("different", first))

    def test_ini_and_yaml_updates_are_idempotent(self) -> None:
        ini = "[Preferences]\nWebUI\\\\Username=old\n[Other]\nValue=1\n"
        desired_ini = reconciler.set_ini_section_values(ini, "Preferences", {r"WebUI\Username": "admin"})
        self.assertEqual(desired_ini, reconciler.set_ini_section_values(desired_ini, "Preferences", {r"WebUI\Username": "admin"}))

        yaml = "general:\n  base_url: /bazarr\nsonarr:\n  ip: old\n"
        updates = {"general": {"base_url": "''"}, "sonarr": {"ip": "sonarr"}}
        desired_yaml = reconciler.set_yaml_section_values(yaml, updates)
        self.assertEqual(desired_yaml, reconciler.set_yaml_section_values(desired_yaml, updates))

    def test_missing_tailnet_domain_produces_no_external_url(self) -> None:
        self.assertEqual(reconciler.build_external_url({"TAILNET_DOMAIN": ""}, "jellyfin"), "")

    def test_unpackerr_config_uses_discovered_keys(self) -> None:
        env = {"SONARR_API_KEY": "sonarr-key", "RADARR_API_KEY": "radarr-key"}
        config = reconciler.unpackerr_config(env, {"sonarr", "radarr"})
        self.assertIn('api_key = "sonarr-key"', config)
        self.assertIn('api_key = "radarr-key"', config)

    def test_public_quality_profile_excludes_cam_and_prefers_4k_cutoff(self) -> None:
        source = {
            "name": "Any",
            "upgradeAllowed": False,
            "cutoff": 1,
            "minFormatScore": 100,
            "cutoffFormatScore": 100,
            "items": [
                {"quality": {"id": 1, "name": "CAM", "resolution": 480}, "allowed": True},
                {"quality": {"id": 2, "name": "Telesync", "resolution": 480}, "allowed": True},
                {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080}, "allowed": False},
                {"quality": {"id": 4, "name": "WEBDL-2160p", "resolution": 2160}, "allowed": False},
            ],
            "formatItems": [{"format": {"id": 10, "name": "Example"}, "score": 250}],
        }

        desired = reconciler.build_public_quality_profile(source, "Public 4K Preferred")

        self.assertEqual(desired["name"], "Public 4K Preferred")
        self.assertTrue(desired["upgradeAllowed"])
        self.assertEqual(desired["cutoff"], 4)
        self.assertEqual(reconciler.quality_allowed_map(desired), {1: False, 2: False, 3: True, 4: True})
        self.assertEqual(desired["minFormatScore"], 0)
        self.assertEqual(desired["cutoffFormatScore"], 0)
        self.assertEqual(desired["formatItems"][0]["score"], 0)

    def test_public_quality_profile_prefers_english_without_requiring_it(self) -> None:
        source = {
            "name": "Any",
            "upgradeAllowed": False,
            "cutoff": 1,
            "minFormatScore": 50,
            "cutoffFormatScore": 500,
            "items": [
                {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080}, "allowed": True},
                {"quality": {"id": 4, "name": "WEBDL-2160p", "resolution": 2160}, "allowed": True},
            ],
            "formatItems": [{"format": {"id": 10, "name": "Example"}, "score": 250}],
        }

        desired = reconciler.build_public_quality_profile(source, "Public 4K Preferred", 11)

        self.assertEqual(desired["minFormatScore"], 0)
        self.assertEqual(desired["cutoffFormatScore"], reconciler.PREFERRED_LANGUAGE_CUSTOM_FORMAT_SCORE)
        self.assertEqual(reconciler.format_score_map(desired), {10: 0, 11: reconciler.PREFERRED_LANGUAGE_CUSTOM_FORMAT_SCORE})

    def test_preferred_language_custom_format_matches_english_language(self) -> None:
        schema = {
            "implementation": "LanguageSpecification",
            "implementationName": "Language",
            "negate": True,
            "required": True,
            "fields": [
                {"name": "value", "value": 0},
                {"name": "exceptLanguage", "value": True},
            ],
            "presets": [{"name": "Unused"}],
        }

        desired = reconciler.build_preferred_language_custom_format(schema)

        self.assertEqual(desired["name"], "English Preferred")
        self.assertEqual(reconciler.custom_format_field_value_map(desired), {"value": 1, "exceptLanguage": False})
        self.assertTrue(reconciler.preferred_language_custom_format_matches(desired, desired))

    def test_jellyfin_user_configuration_prefers_english_without_file_default(self) -> None:
        desired = reconciler.desired_jellyfin_user_configuration(
            {
                "AudioLanguagePreference": "",
                "PlayDefaultAudioTrack": True,
                "RememberAudioSelections": True,
                "SubtitleLanguagePreference": "",
                "SubtitleMode": "Default",
                "RememberSubtitleSelections": True,
            }
        )

        self.assertEqual(desired["AudioLanguagePreference"], "eng")
        self.assertFalse(desired["PlayDefaultAudioTrack"])
        self.assertFalse(desired["RememberAudioSelections"])
        self.assertEqual(desired["SubtitleLanguagePreference"], "eng")
        self.assertEqual(desired["SubtitleMode"], "Smart")
        self.assertFalse(desired["RememberSubtitleSelections"])

    def test_public_quality_profile_falls_back_to_1080p_cutoff_without_4k(self) -> None:
        source = {
            "name": "Any",
            "upgradeAllowed": False,
            "cutoff": 1,
            "items": [
                {"quality": {"id": 1, "name": "DVD", "resolution": 480}, "allowed": False},
                {"quality": {"id": 2, "name": "HDTV-720p", "resolution": 720}, "allowed": False},
                {"quality": {"id": 3, "name": "WEBDL-1080p", "resolution": 1080}, "allowed": False},
            ],
        }

        desired = reconciler.build_public_quality_profile(source, "Public 4K Preferred")

        self.assertEqual(desired["cutoff"], 3)
        self.assertEqual(reconciler.quality_allowed_map(desired), {1: True, 2: True, 3: True})

    def test_arr_quality_cap_converts_gb_per_hour_to_mb_per_minute(self) -> None:
        self.assertEqual(
            reconciler.arr_quality_max_mb_per_minute(
                {"ARR_MAX_GB_PER_HOUR": "9", "SONARR_MAX_GB_PER_HOUR": ""},
                reconciler.ARR_SERVICES[0],
            ),
            153.6,
        )

    def test_arr_quality_profiles_to_delete_keeps_only_managed_profile(self) -> None:
        profiles = [
            {"id": 1, "name": "Any"},
            {"id": 2, "name": "Public 4K Preferred"},
            {"id": 3, "name": "HD-1080p"},
        ]

        self.assertEqual(
            [profile["name"] for profile in reconciler.arr_quality_profiles_to_delete(profiles, 2)],
            ["Any", "HD-1080p"],
        )

    def test_qbittorrent_preferences_enable_category_paths_in_manual_mode(self) -> None:
        updates = reconciler.desired_qbittorrent_preference_updates(
            {
                "save_path": "/data/torrents",
                "temp_path": "/data/torrents/incomplete",
                "temp_path_enabled": True,
                "use_category_paths_in_manual_mode": False,
                "listen_port": 50000,
            },
            {
                "QBITTORRENT_SAVE_PATH": "/data/torrents",
                "QBITTORRENT_TEMP_PATH": "/data/torrents/incomplete",
            },
            50000,
        )

        self.assertEqual(updates, {"use_category_paths_in_manual_mode": True})

    def test_qbittorrent_torrents_marker_is_placed_in_download_root(self) -> None:
        with mock.patch.object(reconciler, "exec_in_service") as exec_in_service:
            reconciler.ensure_qbittorrent_torrents_marker({"QBITTORRENT_SAVE_PATH": "/data/torrents"}, False)

        exec_in_service.assert_called_once()
        service, command, dry_run = exec_in_service.call_args.args
        self.assertEqual(service, "qbittorrent")
        self.assertFalse(dry_run)
        self.assertIn("/data/torrents/THIS_IS_NOT_THE_MEDIA_LIBRARY.txt", command)
        self.assertIn("Jellyfin media library", command)
        self.assertIn("cmp -s", command)

    def test_managed_qbittorrent_category_paths_only_include_running_arrs(self) -> None:
        paths = reconciler.managed_qbittorrent_category_paths(
            {
                "SONARR_QBIT_CATEGORY": "tv-sonarr",
                "SONARR_DOWNLOAD_PATH": "/data/torrents/tv",
                "RADARR_QBIT_CATEGORY": "radarr",
                "RADARR_DOWNLOAD_PATH": "/data/torrents/movies",
            },
            {"radarr"},
        )

        self.assertEqual(paths, {"radarr": "/data/torrents/movies"})

    def test_missing_optional_credentials_stop_only_their_capabilities(self) -> None:
        running = {"sonarr", "radarr", "vpn", "qbittorrent", "tsdproxy"}
        with mock.patch.object(reconciler, "run_compose") as run_compose:
            reconciler.apply_capability_state(
                {"PIA_USER": "", "PIA_PASS": "", "TAILNET_DOMAIN": ""},
                running,
                False,
            )

        self.assertEqual(running, {"sonarr", "radarr"})
        run_compose.assert_has_calls(
            [
                mock.call(["stop", "qbittorrent", "vpn"], check=False),
                mock.call(["stop", "tsdproxy"], check=False),
            ]
        )


if __name__ == "__main__":
    unittest.main()
