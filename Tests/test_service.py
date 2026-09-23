"""Portable end-to-end checks for song packs and reproducible recipes."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from genre_midi.midi import parse_midi, song_midi
from genre_midi.model import BAR, PPQ, Config, GenerationError, Note, Section, Song, Track
from genre_midi import registry
from genre_midi.registry import available_genres, get_profile, profile_fingerprint, register_engine, validate_profile
from genre_midi.service import create_song, export_song, load_recipe, validate_song


def _vlq(value):
    result = [value & 127]
    while value > 127:
        value >>= 7
        result.insert(0, (value & 127) | 128)
    return bytes(result)


def template_bytes():
    """Independent miniature SMF writer: named slots, setup data and one note."""
    def chunk(events):
        payload = b"".join(_vlq(delta) + message for delta, message in events)
        return b"MTrk" + struct.pack(">I", len(payload)) + payload
    conductor = chunk([(0, b"\xff\x03\x09Conductor"),
                       (0, b"\xff\x51\x03\x06\x1a\x80"),
                       (3840, b"\xff\x2f\x00")])
    chunks = [conductor]
    slots = [("Kick - tuned", 0, 36, 38), ("Bass - reverse", 1, 30, 38),
             ("Lead - primary", 2, 74, 81), ("Pad - wide", 3, 62, 89),
             ("Snare - tight", 9, 38, None), ("Custom Layer", 4, 74, 80)]
    for name, channel, pitch, program in slots:
        encoded = name.encode("utf-8")
        messages = [(0, b"\xff\x03" + _vlq(len(encoded)) + encoded),
                    (0, bytes((0xb0 | channel, 7, 96)))]
        if program is not None:
            messages.append((0, bytes((0xc0 | channel, program))))
        messages.extend([(0, bytes((0x90 | channel, pitch, 100))),
                         (120, bytes((0x80 | channel, pitch, 0))),
                         (3720, b"\xff\x2f\x00")])
        chunks.append(chunk(messages))
    return b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), 960) + b"".join(chunks)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        # Python 3.14's owner-only temporary directory ACL blocks restricted
        # Windows tokens; ordinary mkdir inherits the usable workspace ACL.
        self.temporary_parent = (ROOT / "Tests" / "_service_test_output").resolve()
        self.temporary_parent.mkdir(exist_ok=True)
        self.folder = self.temporary_parent / uuid.uuid4().hex
        self.folder.mkdir()

    def tearDown(self):
        resolved = self.folder.resolve()
        if resolved.parent != self.temporary_parent or len(resolved.name) != 32:
            raise RuntimeError("Refusing to clean an unexpected test path")
        shutil.rmtree(resolved)

    def short_song(self):
        return create_song(Config(arrangement="drop"))

    def test_full_128_bar_pack_and_checksum_manifest(self):
        song = create_song(Config())
        destination = self.folder / "Full Song"
        result = export_song(song, destination)
        self.assertEqual(result["total_bars"], 128)
        self.assertEqual(len(result["tracks"]), 13)
        self.assertEqual({row["receiver"] for row in result["automation"]}, {"lead", "bass", "pad"})
        required = {"song.mid", "preview_gm.mid", "recipe.json", "genre_profile.json", "chords.json",
                    "report.json", "START_HERE.txt", "SHA256SUMS.txt"}
        self.assertTrue(required <= set(result["files"]))
        self.assertEqual(len(list((destination / "tracks").glob("*.mid"))), 13)
        self.assertGreater(len(list((destination / "automation").glob("*_NOTES.mid"))), 3)
        for path in destination.rglob("*.mid"):
            document = parse_midi(path.read_bytes())
            self.assertEqual(document.ppq, 960)
            for track in document.tracks:
                self.assertEqual(track[-1].meta_type, 0x2f)
                self.assertEqual(track[-1].tick, 128 * BAR, path.name)
                if path.parent.name == "automation":
                    self.assertFalse(any(event.status & 0xf0 in (0xb0,0xe0) for event in track))
        report = json.loads((destination / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["validation"]["notes"], sum(len(track.notes) for track in song.tracks))
        self.assertEqual(report["duration_seconds"], 204.8)
        self.assertEqual(report["composition"]["profile_sha256"], profile_fingerprint(get_profile("hardstyle")))
        manifest = {}
        for line in (destination / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
            checksum, name = line.split("  ", 1)
            manifest[name] = checksum
            self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), checksum)
        self.assertEqual(set(manifest), set(result["files"]) - {"SHA256SUMS.txt"})

    def test_portable_sound_set_recipe_regenerates_identical_midi(self):
        source = self.folder / "original kit.mid"
        source.write_bytes(template_bytes())
        config = Config(arrangement="compact", sound_set=str(source), lane_roles={"track_6": "lead"},
                        seed=440, key="Bb", style="classic")
        song = create_song(config)
        output = self.folder / "pack"
        export_song(song, output, include_automation=False)
        saved = json.loads((output / "recipe.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["config"]["sound_set"], "sound_set.mid")
        self.assertEqual(saved["config"]["lane_roles"]["track_6"], "lead")
        self.assertEqual((output / "sound_set.mid").read_bytes(), source.read_bytes())
        # Relocate by copying the whole portable pack, then make the original
        # source unusable. Regeneration must resolve the bundled relative path.
        portable = self.folder / "moved location" / "portable pack"
        shutil.copytree(output, portable)
        source.write_bytes(b"original sound set is no longer available")
        restored = load_recipe(portable / "recipe.json")
        self.assertEqual(Path(restored.sound_set), (portable / "sound_set.mid").resolve())
        regenerated = create_song(restored)
        self.assertEqual(song_midi(regenerated), (portable / "song.mid").read_bytes())
        self.assertEqual([(track.name, track.channel, track.program, track.controls) for track in regenerated.tracks],
                         [(track.name, track.channel, track.program, track.controls) for track in song.tracks])
        self.assertEqual(len(regenerated.tracks), 6)

    def test_existing_destination_is_refused_without_modification(self):
        output = self.folder / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        with self.assertRaisesRegex(GenerationError, "already exists"):
            export_song(self.short_song(), output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(output.iterdir()), [sentinel])

    def test_changed_sound_set_after_preview_is_refused(self):
        source = self.folder / "kit.mid"
        source.write_bytes(template_bytes())
        song = create_song(Config(arrangement="drop", sound_set=str(source), lane_roles={"track_6": "lead"}))
        source.write_bytes(template_bytes() + b"changed")
        output = self.folder / "changed-source"
        with self.assertRaisesRegex(GenerationError, "Sound set changed"):
            export_song(song, output)
        self.assertFalse(output.exists())

    def test_profile_version_and_contents_must_match_preview(self):
        song = self.short_song()
        output = self.folder / "profile-mismatch"
        song.profile_version = "0.0.0"
        with self.assertRaisesRegex(GenerationError, "profile changed"):
            export_song(song, output)
        song = self.short_song()
        changed = copy.deepcopy(get_profile("hardstyle"))
        changed["section_energy"]["drop"] = 0.5
        with patch("genre_midi.service.get_profile", return_value=changed):
            with self.assertRaisesRegex(GenerationError, "profile contents changed"):
                export_song(song, output)
        self.assertFalse(output.exists())

    def test_recipe_rejects_profile_version_or_content_mismatch(self):
        output = self.folder / "recipe-check"
        export_song(self.short_song(), output, include_automation=False)
        original = json.loads((output / "recipe.json").read_text(encoding="utf-8"))
        for field, value, message in (("profile_version", "0.0.0", "profile version"),
                                      ("profile_sha256", "0" * 64, "profile contents")):
            modified = copy.deepcopy(original)
            modified[field] = value
            path = output / f"invalid-{field}.json"
            path.write_text(json.dumps(modified), encoding="utf-8")
            with self.assertRaisesRegex(GenerationError, message):
                load_recipe(path)

    def test_no_automation_option_omits_all_automation_artifacts(self):
        output = self.folder / "notes-only"
        result = export_song(self.short_song(), output, include_automation=False)
        self.assertEqual(result["automation"], [])
        self.assertFalse((output / "automation").exists())
        self.assertTrue((output / "song.mid").is_file())
        self.assertTrue((output / "recipe.json").is_file())

    def test_failed_render_does_not_publish_partial_output(self):
        output = self.folder / "failed-pack"
        with patch("genre_midi.service.render_note_lane", side_effect=RuntimeError("synthetic rendering failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic rendering"):
                export_song(self.short_song(), output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.folder.glob(".failed-pack.building-*")), [])

    def test_public_validation_rejects_malformed_types(self):
        for field in ("key", "scale"):
            config = Config()
            setattr(config, field, [])
            with self.assertRaises(GenerationError):
                config.validate()
        song = self.short_song()
        song.tracks[0].channel = 1.5
        with self.assertRaisesRegex(GenerationError, "routing"):
            validate_song(song)
        song = self.short_song()
        song.sections[0] = replace(song.sections[0], bars="four")
        with self.assertRaisesRegex(GenerationError, "integer bars"):
            validate_song(song)
        profile = copy.deepcopy(get_profile("hardstyle"))
        profile["schema_version"] = True
        with self.assertRaises(GenerationError):
            validate_profile(profile)

    def test_profile_fingerprint_is_order_independent(self):
        profile = get_profile("hardstyle")
        reordered = dict(reversed(list(profile.items())))
        self.assertEqual(profile_fingerprint(profile), profile_fingerprint(reordered))

    def test_registered_future_genre_uses_the_same_generation_and_export_pipeline(self):
        profiles = self.folder / "profiles"
        profiles.mkdir()
        profile = copy.deepcopy(get_profile("hardstyle"))
        profile.update(id="extension-fixture", name="Extension Fixture", engine="test-composer",
                       styles={"minimal": {"name": "Minimal"}}, test_note_pitch=74)
        (profiles / "extension.json").write_text(json.dumps(profile), encoding="utf-8")
        calls = []

        def future_composer(config, selected_profile):
            calls.append(selected_profile["id"])
            pitch = selected_profile["test_note_pitch"]
            return Song(config, [Section("drop", "Extension section", 0, 2, 0.5)],
                        [Track("test-lead", "Extension lead", "lead", 1, 81,
                               [Note(0, PPQ, pitch, 80), Note(BAR, PPQ, pitch + 3, 70)])],
                        [], selected_profile["version"], [], {"fixture_engine": True})

        with patch.object(registry, "PROFILE_DIR", profiles), patch.dict(registry._ENGINES, {}, clear=True):
            register_engine("test-composer", future_composer)
            self.assertEqual([p["id"] for p in available_genres()], ["extension-fixture"])
            song = create_song(Config(genre="extension-fixture", style="minimal"))
            self.assertEqual(calls, ["extension-fixture"])
            self.assertEqual([note.pitch for note in song.tracks[0].notes], [74, 77])
            result = export_song(song, self.folder / "extension-pack", include_automation=False)
            self.assertEqual(result["total_bars"], 2)
            self.assertEqual(result["tracks"][0]["name"], "Extension lead")
            self.assertEqual(result["automation"], [])
            restored = load_recipe(self.folder / "extension-pack" / "recipe.json")
            self.assertEqual(restored.genre, "extension-fixture")


if __name__ == "__main__":
    unittest.main()
