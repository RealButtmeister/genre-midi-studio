"""Pitch automation contracts, inspected independently of the MIDI writer."""
from io import BytesIO
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from genre_midi.model import BAR, PPQ, Config, GenerationError, Note, Section, Song, Track
from genre_midi.note_automation import (
    build_note_lanes, evaluate_note_lane, preview_note_lane, render_note_lane,
    static_note_settings, note_values, DENSITIES,
)
from genre_midi.registry import get_profile
from genre_midi.service import create_song


def fixture(bars=2, starts=(0, PPQ)):
    return Song(Config(bpm=150), [Section("drop", "Drop", 0, bars, .9)],
                [Track("kick", "Kick", "kick", 1, notes=[Note(t, 1, 36, 110) for t in starts]),
                 Track("lead", "Lead", "lead", 2, notes=[Note(0, PPQ, 60, 100)])], [])


def curve(target="pan", start=-1, finish=1, bars=2, terminal=None):
    """A score fixture, not a copy of the renderer's compiled private format."""
    terminal = finish if terminal is None else terminal
    points = [[0, start], [1 - 1 / (bars * BAR), finish], [1, terminal]]
    return {"lead": {"lanes": [{"target": target, "start_bar": 0,
                                 "length_bars": bars, "points": points}], "triggers": []}}


def make_lane(song, score):
    with patch("genre_midi.note_automation.build_automation", return_value=score):
        return build_note_lanes(song, {})[0]


def events(data):
    """Independent explicit-status SMF decoder; yields (track,tick,type,data)."""
    if data[:4] != b"MThd":
        raise AssertionError("Not SMF")
    header_size, fmt, count, ppq = struct.unpack(">IHHH", data[4:14])
    if (header_size, fmt, ppq) != (6, 1, 960):
        raise AssertionError("Unexpected MIDI header")
    offset = 8 + header_size
    for track_index in range(count):
        if data[offset:offset + 4] != b"MTrk":
            raise AssertionError("Missing track")
        length = int.from_bytes(data[offset + 4:offset + 8], "big")
        cursor, end, tick = offset + 8, offset + 8 + length, 0
        def vlq():
            nonlocal cursor
            value = 0
            for _ in range(4):
                byte = data[cursor]
                cursor += 1
                value = (value << 7) | (byte & 127)
                if byte < 128:
                    return value
            raise AssertionError("Bad VLQ")
        while cursor < end:
            tick += vlq()
            status = data[cursor]
            cursor += 1
            if status == 255:
                kind = data[cursor]
                cursor += 1
                size = vlq()
                payload = data[cursor:cursor + size]
                cursor += size
                yield track_index, tick, (255, kind), payload
            elif status & 240 in (128, 144):
                payload = data[cursor:cursor + 2]
                cursor += 2
                yield track_index, tick, status, payload
            else:
                raise AssertionError(f"Unexpected non-note message: {status:#x}")
        if cursor != end:
            raise AssertionError("Bad track length")
        offset = end
    if offset != len(data):
        raise AssertionError("Trailing file data")


class NoteAutomationTests(unittest.TestCase):
    def test_flat_spans_stay_separate_tiny_notes_and_contain_no_controllers(self):
        song = fixture()
        lane = make_lane(song, curve(start=-1, finish=1))
        data, stats = render_note_lane(song, lane)
        note_events = [(tick, status, payload) for _, tick, status, payload in events(data)
                       if isinstance(status, int)]
        self.assertEqual(stats["note_count"], 2 * 4 * 240)
        self.assertEqual(len(note_events), stats["note_count"] * 2)
        repeated = 0
        previous_pitch = None
        for index in range(0, len(note_events), 2):
            on, off = note_events[index:index + 2]
            self.assertEqual(on[0], index // 2 * 4)
            self.assertEqual(off[0], on[0] + 4)
            self.assertEqual(on[1], 0x90)
            self.assertEqual(off[1], 0x80)
            self.assertGreaterEqual(on[2][1], 1)
            self.assertLessEqual(on[2][0], 120)
            self.assertLessEqual(abs(on[2][0] / 120 - on[2][1] / 127), 1 / 120)
            self.assertEqual(off[2], bytes((on[2][0], 0)))
            repeated += on[2][0] == previous_pitch
            previous_pitch = on[2][0]
        self.assertGreater(repeated, 1700)
        self.assertEqual(note_events[-1][0], song.ticks)

    def test_straight_ramp_in_normalized_space_including_logarithmic_targets(self):
        song = fixture()
        for target, low, high in (("pan", -1, 1), ("cutoff", 30, 20000),
                                  ("highpass", 20, 3000), ("release", 10, 1000)):
            lane = make_lane(song, curve(target, low, high))
            span = song.ticks - 1
            for fraction in (.1, .25, .5, .75, .9):
                self.assertAlmostEqual(evaluate_note_lane(song, lane, fraction * span), fraction)
            notes = preview_note_lane(song, lane)
            self.assertLessEqual(max(abs(a["pitch"] - b["pitch"]) for a, b in zip(notes, notes[1:])), 1)
            self.assertEqual(lane["shape"], "linear")

    def test_motion_is_straight_saw_exact_off_grid_kicks_and_zero_without_kicks(self):
        song = fixture(starts=(PPQ, PPQ + 483, 3 * PPQ))
        lane = next(lane for lane in build_note_lanes(song, {}) if lane["target"] == "motion")
        self.assertEqual(evaluate_note_lane(song, lane, 0), 0)
        self.assertEqual(evaluate_note_lane(song, lane, PPQ), 1)
        self.assertEqual(evaluate_note_lane(song, lane, PPQ + 240), .75)
        self.assertEqual(evaluate_note_lane(song, lane, PPQ + 483), 1)
        self.assertEqual(evaluate_note_lane(song, lane, PPQ + 483 + 480), .5)
        self.assertEqual(evaluate_note_lane(song, lane, 6 * PPQ), 0)
        notes = preview_note_lane(song, lane)
        reset = next(note for note in notes if note["start"] == PPQ + 483)
        self.assertEqual(reset["pitch"], 120)
        self.assertEqual(reset["velocity"], 127)
        self.assertTrue(all(1 <= note["duration"] <= 4 for note in notes))
        self.assertTrue(all(a["start"] + a["duration"] == b["start"] for a, b in zip(notes, notes[1:])))
        song.tracks[0].notes.clear()
        self.assertFalse(any(lane["target"] == "motion" for lane in build_note_lanes(song, {})))

    def test_final_tiny_note_has_neutral_value_even_with_late_kicks(self):
        song = fixture(starts=(0, 2 * BAR - 2, 2 * BAR - 1))
        lane = next(lane for lane in build_note_lanes(song, {}) if lane["target"] == "motion")
        notes = preview_note_lane(song, lane)
        self.assertEqual(notes[-1], {"start": song.ticks - 1, "duration": 1, "pitch": 0, "velocity": 1})
        pan = make_lane(song, curve(terminal=0))
        self.assertEqual(preview_note_lane(song, pan)[-1]["pitch"], 60)
        self.assertEqual(preview_note_lane(song, pan)[-1]["velocity"], 64)

    def test_note_and_velocity_are_same_percentage_with_visible_zero_notes(self):
        self.assertEqual(note_values(0), (0, 1))
        self.assertEqual(note_values(.5), (60, 64))
        self.assertEqual(note_values(1), (120, 127))
        for index in range(1001):
            value = index / 1000
            pitch, velocity = note_values(value)
            self.assertLessEqual(abs(pitch / 120 - value), .5 / 120 + 1e-12)
            self.assertLessEqual(abs(velocity / 127 - value), 1 / 127 + 1e-12)
            self.assertGreater(velocity, 0)

    def test_original_lane_names_channels_and_duplicate_names_survive_layer_expansion(self):
        song = fixture()
        song.tracks[1].name = "M1 Supersaw - main"
        song.tracks[1].channel = 7
        song.tracks.append(Track("layer", "M1 Supersaw - main", "lead", 12,
                                 notes=[Note(0, PPQ, 72, 100)]))
        lanes = build_note_lanes(song, {})
        self.assertEqual([lane["id"] for lane in lanes], ["lead_motion", "layer_motion"])
        self.assertIs(lanes[0]["_kick_ticks"], lanes[1]["_kick_ticks"])
        for lane, expected_channel in zip(lanes, (7, 12)):
            self.assertEqual(lane["name"], "M1 Supersaw - main | Motion")
            self.assertEqual(lane["source_channel"], expected_channel)
            self.assertEqual(lane["channel"], 1)
            data, stats = render_note_lane(song, lane)
            metadata = {status[1]: payload.decode() for track, _, status, payload in events(data)
                        if track == 1 and isinstance(status, tuple) and status[1] in (3, 4)}
            self.assertEqual(metadata[3], lane["name"])
            self.assertEqual(metadata[4], "M1 Supersaw - main")
            self.assertEqual(stats["source_channel"], expected_channel)

    def test_only_changing_continuous_controls_export_static_settings_remain_available(self):
        song = fixture()
        lanes = build_note_lanes(song, {})
        self.assertEqual([lane["target"] for lane in lanes], ["motion"])
        settings = static_note_settings(song, {})["lead"]
        self.assertEqual(settings["filterType"], 0)
        self.assertEqual(settings["cutoff"], 19000)
        self.assertGreater(settings["pump"], 0)
        self.assertEqual(len(settings), 25)

    def test_full_song_default_density_never_drops_and_markers_match(self):
        song = create_song(Config(arrangement="full", humanize_ms=0))
        lanes = build_note_lanes(song, get_profile("hardstyle"))
        self.assertGreater(len(lanes), 20)
        lane = next(lane for lane in lanes if lane["id"] == "lead_cutoff")
        data, stats = render_note_lane(song, lane)
        self.assertEqual(stats["notes_per_beat"], 240)
        self.assertEqual(stats["note_length_ticks"], 4)
        self.assertEqual(stats["note_count"], 122880)
        count, end_ticks, markers, last_end = 0, [], [], 0
        for track, tick, status, payload in events(data):
            if status == (255, 6):
                markers.append((tick, payload.decode()))
            elif status == (255, 47):
                end_ticks.append(tick)
            elif status == 144:
                self.assertEqual(tick, last_end)
                count += 1
            elif status == 128:
                self.assertEqual(tick - last_end, 4)
                last_end = tick
        self.assertEqual(count, 122880)
        self.assertEqual(end_ticks, [song.ticks, song.ticks])
        self.assertEqual(markers, [(section.start_bar * BAR, section.name) for section in song.sections])

    def test_density_choices_are_exact_and_deterministic(self):
        song = fixture()
        lane = make_lane(song, curve())
        for density in DENSITIES:
            first, stats = render_note_lane(song, lane, density)
            self.assertEqual(first, render_note_lane(song, lane, density)[0])
            self.assertEqual(stats["note_count"], song.bars * 4 * density)
            self.assertEqual(stats["note_length_ticks"], 960 // density)
            self.assertEqual(preview_note_lane(song, lane, density)[0]["duration"], 960 // density)
        for density in (0, 4, 239, 241, 1920, True, 240.0, "240"):
            with self.assertRaises(GenerationError):
                render_note_lane(song, lane, density)

    def test_largest_song_at_one_tick_density_exceeds_import_event_limit_safely(self):
        song = fixture(bars=512)
        lane = make_lane(song, curve(bars=512))
        data, stats = render_note_lane(song, lane, 960)
        self.assertEqual(stats["note_count"], 1_966_080)
        self.assertEqual(stats["note_length_ticks"], 1)
        self.assertLess(len(data), 32 * 1024 * 1024)
        self.assertTrue(data.endswith(b"\x00\xff\x2f\x00"))

    def test_preview_returns_same_absolute_notes_as_export_grid(self):
        song = fixture(bars=4, starts=(0, BAR + 17))
        lane = next(lane for lane in build_note_lanes(song, {}) if lane["target"] == "motion")
        notes = preview_note_lane(song, lane, 240, start_bar=1, bars=2)
        data, _ = render_note_lane(song, lane)
        exported = [(tick, payload[0]) for _, tick, status, payload in events(data)
                    if status == 144 and BAR <= tick < 3 * BAR]
        self.assertEqual([(note["start"], note["pitch"]) for note in notes], exported)
        for start, bars in ((-1, 2), (4, 1), (0, 0), (0, 9), (float("nan"), 1)):
            with self.assertRaises(GenerationError):
                preview_note_lane(song, lane, start_bar=start, bars=bars)

    def test_independent_mido_reads_note_timing(self):
        try:
            import mido
        except ImportError:
            self.skipTest("Optional independent mido reader is unavailable")
        song = fixture()
        lane = make_lane(song, curve())
        data, _ = render_note_lane(song, lane)
        midi = mido.MidiFile(file=BytesIO(data))
        self.assertEqual(midi.ticks_per_beat, 960)
        self.assertEqual(len(midi.tracks), 2)
        self.assertEqual(sum(message.time for message in midi.tracks[1]), song.ticks)
        self.assertEqual(sum(message.type == "note_on" for message in midi.tracks[1]), 1920)
        self.assertTrue(all(message.is_meta or message.type in ("note_on", "note_off")
                            for track in midi.tracks for message in track))


if __name__ == "__main__":
    unittest.main()
