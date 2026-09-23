"""Musical and structural contracts for the profile-driven composer."""
from __future__ import annotations

import copy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from genre_midi.genres.hardstyle import compose
from genre_midi.model import BAR, PPQ, ROOTS, SCALES, Config, GenerationError


class ComposerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = json.loads((ROOT / "profiles" / "hardstyle.json").read_text(encoding="utf-8"))

    def generate(self, **kwargs):
        return compose(Config(**kwargs), self.profile)

    @staticmethod
    def track(song, name):
        return next(track for track in song.tracks if track.id == name)

    def assert_valid_notes(self, song):
        self.assertEqual(len(song.tracks), 13)
        self.assertEqual(len({track.id for track in song.tracks}), 13)
        for track in song.tracks:
            previous = {}
            for note in track.notes:
                self.assertIsInstance(note.start, int)
                self.assertIsInstance(note.duration, int)
                self.assertGreaterEqual(note.start, 0)
                self.assertGreater(note.duration, 0)
                self.assertLessEqual(note.end, song.ticks)
                self.assertTrue(0 <= note.pitch <= 127)
                self.assertTrue(1 <= note.velocity <= 127)
                group = 0 if track.id in {"kick", "bass", "lead", "screech"} else note.pitch
                self.assertGreaterEqual(note.start, previous.get(group, 0), (track.id, note))
                previous[group] = note.end
                section = next(s for s in song.sections if s.start_bar * BAR <= note.start < s.end_bar * BAR)
                self.assertLessEqual(note.end, section.end_bar * BAR)

    def test_arrangements_and_complete_tracks(self):
        for arrangement, bars in (("full", 128), ("compact", 64), ("drop", 16)):
            with self.subTest(arrangement=arrangement):
                song = self.generate(arrangement=arrangement)
                self.assertEqual(song.bars, bars)
                self.assertEqual(len(song.chords), bars)
                self.assertEqual(song.ticks, bars * BAR)
                self.assertTrue(all(track.notes for track in song.tracks))
                self.assert_valid_notes(song)

    def test_custom_sections_are_authoritative(self):
        requested = [{"kind": "drop", "bars": 3, "name": "First", "energy": 0.25},
                     {"kind": "breakdown", "bars": 1}, {"kind": "build", "bars": 5},
                     {"kind": "drop", "bars": 7}]
        song = self.generate(sections=requested)
        self.assertEqual([(s.kind, s.bars, s.start_bar) for s in song.sections],
                         [("drop", 3, 0), ("breakdown", 1, 3), ("build", 5, 4), ("drop", 7, 9)])
        self.assertEqual(song.sections[0].name, "First")
        self.assertEqual(song.sections[0].energy, 0.25)
        self.assertEqual(song.bars, 16)
        self.assert_valid_notes(song)

    def test_seed_is_reproducible_and_inputs_are_unchanged(self):
        config = Config(arrangement="compact", sections=[{"kind": "drop", "bars": 9}])
        profile = copy.deepcopy(self.profile)
        before_config, before_profile = asdict(config), copy.deepcopy(profile)
        first, second = compose(config, profile), compose(config, profile)
        self.assertEqual(asdict(first), asdict(second))
        self.assertEqual(asdict(config), before_config)
        self.assertEqual(profile, before_profile)
        first.config.sections[0]["bars"] = 1
        self.assertEqual(config.sections[0]["bars"], 9)

    def test_multiple_keys_scales_styles_and_extreme_controls(self):
        for index, key in enumerate(("C", "F#", "Bb")):
            for scale_name in self.profile["scales"]:
                for style in self.profile["styles"]:
                    with self.subTest(key=key, scale=scale_name, style=style):
                        song = self.generate(key=key, scale=scale_name, style=style, seed=index,
                                             arrangement="drop", complexity=index / 2,
                                             energy=index / 2, variation=index / 2, humanize_ms=15)
                        self.assert_valid_notes(song)
                        allowed = {(ROOTS[key] + interval) % 12 for interval in SCALES[scale_name]}
                        for track in song.tracks:
                            if track.channel != 10:
                                self.assertTrue(all(note.pitch % 12 in allowed for note in track.notes), track.id)
                        for chord in song.chords:
                            self.assertTrue(all(pitch % 12 in allowed for pitch in chord.pitches))

    def test_pitched_kick_tracks_chord_roots_and_fixed_mode(self):
        song = self.generate(arrangement="drop")
        for note in self.track(song, "kick").notes:
            self.assertEqual(note.pitch % 12, song.chords[note.start // BAR].root % 12)
            self.assertTrue(24 <= note.pitch <= 48)
        fixed = self.generate(arrangement="drop", kick_mode="fixed")
        self.assertEqual({n.pitch for n in self.track(fixed, "kick").notes}, {36})
        self.assertEqual([n.start for n in self.track(song, "kick").notes],
                         [n.start for n in self.track(fixed, "kick").notes])

    def test_bass_avoids_kick_attacks_and_tails(self):
        for style in self.profile["styles"]:
            song = self.generate(arrangement="drop", style=style, complexity=1, variation=1)
            kicks = self.track(song, "kick").notes
            for bass in self.track(song, "bass").notes:
                self.assertFalse(any(bass.start < kick.end and kick.start < bass.end for kick in kicks), (style, bass))

    def test_predrop_final_beat_is_silent(self):
        song = self.generate(humanize_ms=15, complexity=1)
        for section in song.sections:
            if section.kind == "build":
                gap_start, gap_end = section.end_bar * BAR - PPQ, section.end_bar * BAR
                for track in song.tracks:
                    self.assertFalse(any(note.start < gap_end and note.end > gap_start for note in track.notes), track.id)

    def test_hook_repeats_at_drop_returns(self):
        song = self.generate(variation=1)
        drops = [section for section in song.sections if section.kind == "drop"]
        lead = self.track(song, "lead")
        excerpts = []
        for section in drops:
            start = section.start_bar * BAR
            excerpts.append([(note.start - start, note.duration, note.pitch) for note in lead.notes
                             if start <= note.start < start + 8 * BAR])
        self.assertEqual(excerpts[0], excerpts[1])
        self.assertEqual(len(song.metadata["lead_motif"]), 8)
        first = [(c.root, c.pitches) for c in song.chords[drops[0].start_bar:drops[0].start_bar + 8]]
        second = [(c.root, c.pitches) for c in song.chords[drops[1].start_bar:drops[1].start_bar + 8]]
        self.assertEqual(first, second)

    def test_style_identity_is_a_musical_difference(self):
        songs = {style: self.generate(style=style, arrangement="drop") for style in self.profile["styles"]}
        mean_duration = lambda song, role: sum(n.duration for n in self.track(song, role).notes) / len(self.track(song, role).notes)
        self.assertGreater(mean_duration(songs["euphoric"], "lead"), mean_duration(songs["raw"], "lead"))
        self.assertGreater(len(self.track(songs["raw"], "screech").notes), len(self.track(songs["euphoric"], "screech").notes))
        self.assertGreater(len(self.track(songs["classic"], "bass").notes), len(self.track(songs["raw"], "bass").notes))
        self.assertLess(mean_duration(songs["classic"], "kick"), mean_duration(songs["euphoric"], "kick"))
        self.assertEqual(songs["classic"].metadata["bass_mode"], "reverse-bass")

    def test_drop_instrument_parts_are_available_across_seeds(self):
        for seed in range(12):
            for style in self.profile["styles"]:
                song = self.generate(seed=seed, style=style, arrangement="drop", complexity=0)
                self.assertTrue(all(track.notes for track in song.tracks), (seed, style))

    def test_build_accelerates_and_breakdown_contrasts_drop(self):
        song = self.generate(arrangement="compact")
        build = next(s for s in song.sections if s.kind == "build")
        snare = self.track(song, "snare").notes
        counts = [sum(bar * BAR <= note.start < (bar + 1) * BAR for note in snare)
                  for bar in range(build.start_bar, build.end_bar)]
        self.assertGreater(counts[-2], counts[0])
        breakdown = next(s for s in song.sections if s.kind == "breakdown")
        self.assertFalse(any(breakdown.start_bar * BAR <= n.start < breakdown.end_bar * BAR
                             for n in self.track(song, "kick").notes))
        drop = next(s for s in song.sections if s.kind == "drop")
        lead = self.track(song, "lead").notes
        density = lambda section: sum(section.start_bar * BAR <= n.start < section.end_bar * BAR for n in lead) / section.bars
        self.assertGreater(density(drop), density(breakdown))

    def test_profile_data_actually_controls_composition(self):
        config = Config(arrangement="drop")
        original = compose(config, self.profile)
        altered = copy.deepcopy(self.profile)
        altered["styles"]["euphoric"]["kick_gate"] = 0.22
        altered["styles"]["euphoric"]["lead_gate"] = 0.4
        altered["styles"]["euphoric"]["progressions"] = [[0, 0, 0, 0]]
        changed = compose(config, altered)
        self.assertLess(self.track(changed, "kick").notes[0].duration, self.track(original, "kick").notes[0].duration)
        self.assertLess(self.track(changed, "lead").notes[0].duration, self.track(original, "lead").notes[0].duration)
        self.assertEqual({c.root for c in changed.chords}, {changed.chords[0].root})

    def test_humanization_does_not_move_kick_or_hook_anchors(self):
        straight = self.generate(arrangement="drop", humanize_ms=0)
        human = self.generate(arrangement="drop", humanize_ms=15)
        for role in ("kick", "lead", "bass"):
            self.assertEqual(self.track(straight, role).notes, self.track(human, role).notes)
        self.assertNotEqual(self.track(straight, "clap").notes, self.track(human, "clap").notes)

    def test_one_bar_sections_and_maximum_song_length(self):
        short = self.generate(sections=[{"kind": kind, "bars": 1} for kind in ("intro", "build", "drop", "breakdown", "outro")])
        self.assert_valid_notes(short)
        long = self.generate(sections=[{"kind": "drop", "bars": 128}] * 4, complexity=1)
        self.assertEqual(long.bars, 512)
        self.assert_valid_notes(long)

    def test_invalid_options_are_rejected(self):
        for options in ({"style": "missing"}, {"bpm": 201}, {"seed": -1}, {"energy": float("nan")},
                        {"sections": [{"kind": "drop", "bars": 0}]}):
            with self.subTest(options=options), self.assertRaises(GenerationError):
                self.generate(**options)


if __name__ == "__main__":
    unittest.main()
