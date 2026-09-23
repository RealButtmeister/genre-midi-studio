"""Arrangement/receiver automation contracts, with independent MIDI inspection."""
from copy import deepcopy
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from genre_midi.automation import build_automation, NEUTRAL, ROLES
from genre_midi.model import BAR, PPQ, Config, GenerationError, Note, Section, Song, Track
from genre_midi.vendor.motion_fx_codec import TARGETS, render_score, validate_score


def fixture(style="euphoric", energy=.8):
    config = Config(style=style, bpm=153, energy=energy, humanize_ms=0)
    sections = [Section("intro", "Intro", 0, 2, .35), Section("build", "Build", 2, 2, .7),
                Section("drop", "Drop", 4, 2, 1), Section("breakdown", "Break", 6, 2, .25),
                Section("outro", "Outro", 8, 1, .3)]
    starts = [0, PPQ, 2*BAR, 2*BAR+PPQ, 4*BAR, 4*BAR+PPQ//4,
              4*BAR+PPQ//2, 5*BAR, 8*BAR, 9*BAR-2, 9*BAR-1]
    kicks = Track("kit-a", "Kick Main", "kick", 4,
                  notes=[Note(tick, 1, 42, 110) for tick in starts])
    layer = Track("kit-b", "Kick Layer", "kick", 7, notes=[Note(4*BAR, 20, 35, 118)])
    tracks = [kicks, layer] + [Track(role, role.title(), role, i+1,
                                   notes=[Note(4*BAR, PPQ//2, 60, 90)]) for i, role in enumerate(ROLES)]
    return Song(config, sections, tracks, [])


def lane_at(score, target, bar):
    return next(lane for lane in score["lanes"] if lane["target"] == target and lane["start_bar"] == bar)


def decode_midi(data):
    """Read SMF events without using the encoder's parsing or timing helpers."""
    assert data[:4] == b"MThd"
    _, fmt, count, division = struct.unpack(">IHHH", data[4:14])
    tracks, offset = [], 14
    for _ in range(count):
        assert data[offset:offset+4] == b"MTrk"
        size = int.from_bytes(data[offset+4:offset+8], "big")
        body = data[offset+8:offset+8+size]
        cursor, tick, events, running = 0, 0, [], None
        def vlq():
            nonlocal cursor
            value = 0
            while True:
                byte = body[cursor]
                cursor += 1
                value = (value << 7) | (byte & 127)
                if byte < 128:
                    return value
        while cursor < len(body):
            tick += vlq()
            status = body[cursor]
            if status >= 128:
                cursor += 1
            else:
                status = running
            if status == 255:
                kind = body[cursor]
                cursor += 1
                length = vlq()
                events.append((tick, status, kind, body[cursor:cursor+length]))
                cursor += length
                running = None
            else:
                running = status
                length = 1 if status & 240 in (192, 208) else 2
                events.append((tick, status, *body[cursor:cursor+length]))
                cursor += length
        tracks.append(events)
        offset += size + 8
    assert offset == len(data)
    return fmt, division, tracks


class SongAutomationTests(unittest.TestCase):
    def test_exact_actual_kicks_deduplicated_and_nonoverlapping(self):
        song = fixture()
        expected = sorted({note.start for track in song.tracks if track.role == "kick" for note in track.notes})
        for score in build_automation(song, {}).values():
            self.assertEqual([round(t["beat"]*PPQ) for t in score["triggers"]], expected)
            self.assertTrue(all(t["note"] == 36 for t in score["triggers"]))
            duplicate = next(t for t in score["triggers"] if t["beat"] == 16)
            self.assertEqual(duplicate["velocity"], 118)
            for i, trigger in enumerate(score["triggers"]):
                end = (trigger["beat"] + trigger["length_beats"]) * PPQ
                next_start = expected[i+1] if i+1 < len(expected) else song.ticks
                self.assertLessEqual(end, next_start + 1e-8)
                self.assertGreaterEqual(round(trigger["length_beats"]*PPQ), 1)

    def test_all_parameters_sections_units_and_final_neutral_states(self):
        song = fixture()
        for role, score in build_automation(song, {}).items():
            validate_score(score)
            self.assertEqual(score["bpm"], 153)
            self.assertEqual(score["bars"], 9)
            self.assertEqual(score["channel"], 1)
            self.assertEqual(score["beats_per_bar"], 4)
            self.assertFalse(score["release_at_end"])
            self.assertEqual({l["target"] for l in score["lanes"] if l["start_bar"] == 0}, set(TARGETS))
            for target, spec in TARGETS.items():
                lanes = [lane for lane in score["lanes"] if lane["target"] == target]
                self.assertEqual([lane["start_bar"] for lane in lanes], [0, 2, 4, 6, 8])
                self.assertEqual(lanes[-1]["start_bar"]+lanes[-1]["length_bars"], 9)
                for lane in lanes:
                    for _, value in lane["points"]:
                        self.assertGreaterEqual(value, spec.low)
                        self.assertLessEqual(value, spec.high)
                expected = -4 if target == "output" and role == "pad" else NEUTRAL[target]
                self.assertEqual(lanes[-1]["points"][-1][1], expected)

    def test_build_rises_drains_then_drop_resets_immediately(self):
        scores = build_automation(fixture(), {})
        cutoff = lane_at(scores["lead"], "cutoff", 2)["points"]
        self.assertGreater(cutoff[1][1], cutoff[0][1])
        self.assertLess(cutoff[-1][1], cutoff[1][1])
        self.assertEqual(lane_at(scores["lead"], "reverbMix", 2)["points"][-1][1], 0)
        self.assertEqual(lane_at(scores["lead"], "delayMix", 2)["points"][-1][1], 0)
        self.assertEqual(lane_at(scores["lead"], "pump", 2)["points"][-1][1], 0)
        self.assertGreater(lane_at(scores["lead"], "cutoff", 4)["points"][0][1], 18000)
        self.assertGreater(lane_at(scores["bass"], "pump", 4)["points"][0][1], 0)
        for target in ("cutoff", "width", "delayMix", "output"):
            self.assertEqual(lane_at(scores["lead"], target, 2)["points"][0][1],
                             lane_at(scores["lead"], target, 0)["points"][-1][1])

    def test_bass_stays_centered_preserves_drop_low_end(self):
        score = build_automation(fixture(style="raw"), {})["bass"]
        for lane in score["lanes"]:
            values = [p[1] for p in lane["points"]]
            if lane["target"] in ("pan", "motionPan", "motionWidth", "motionCutoff"):
                self.assertEqual(set(values), {0})
            if lane["target"] == "width":
                self.assertLessEqual(max(values), 1)
            if lane["target"] == "reverbMix":
                self.assertLessEqual(max(values), .035)
            if lane["target"] == "delayMix":
                self.assertLessEqual(max(values), .02)
        self.assertTrue(all(p[1] == 20 for p in lane_at(score, "highpass", 4)["points"]))

    def test_no_kicks_no_triggers_no_pump_and_unmapped_receivers_omitted(self):
        song = fixture()
        song.tracks = [Track("plink", "Pluck", "arp", 12)]
        scores = build_automation(song, {})
        self.assertEqual(set(scores), {"lead"})
        self.assertEqual(scores["lead"]["triggers"], [])
        self.assertTrue(all(p[1] == 0 for lane in scores["lead"]["lanes"] if lane["target"] == "pump" for p in lane["points"]))
        self.assertIn("Pluck", scores["lead"]["description"])

    def test_quiet_break_pumps_off_pad_has_slow_independent_motion(self):
        scores = build_automation(fixture(), {})
        for role in ROLES:
            self.assertEqual({p[1] for p in lane_at(scores[role], "pump", 6)["points"]}, {0})
            self.assertEqual({p[1] for lane in scores[role]["lanes"] if lane["target"] == "mode" for p in lane["points"]}, {1})
        pad_pan = lane_at(scores["pad"], "pan", 6)["points"]
        self.assertGreater(max(p[1] for p in pad_pan), 0)
        self.assertLess(min(p[1] for p in pad_pan), 0)
        self.assertGreater(lane_at(scores["pad"], "reverbMix", 6)["points"][1][1],
                           lane_at(scores["lead"], "reverbMix", 6)["points"][1][1])

    def test_style_and_energy_intent_are_distinct_and_deterministic(self):
        raw = build_automation(fixture("raw"), {})
        euphoric = build_automation(fixture("euphoric"), {})
        classic = build_automation(fixture("classic"), {})
        self.assertEqual(raw, build_automation(fixture("raw"), {}))
        value = lambda s, target: lane_at(s["lead"], target, 4)["points"][0][1]
        self.assertGreater(value(raw, "drive"), value(euphoric, "drive"))
        self.assertLess(value(raw, "reverbMix"), value(euphoric, "reverbMix"))
        self.assertLess(value(classic, "drive"), value(euphoric, "drive"))
        low = build_automation(fixture(energy=0), {})
        self.assertLess(value(low, "pump"), value(euphoric, "pump"))

    def test_profile_extension_and_rejection_without_mutation(self):
        song = fixture()
        song.config.genre = "future-genre"
        profile = {"automation": {"section_amounts": {"drop": {"lead": {"cutoff": 7777}}},
                                  "role_limits": {"lead": {"width": [1, 1.1]}}}}
        before = deepcopy(profile)
        score = build_automation(song, profile)["lead"]
        self.assertEqual(lane_at(score, "cutoff", 4)["points"][0][1], 7777)
        self.assertEqual(profile, before)
        for bad in ({"wat": 1}, {"section_amounts": {"drop": {"bass": {"pump": 100}}}},
                    {"role_limits": {"bass": {"pan": [.1, .2]}}},
                    {"section_amounts": {"drop": {"lead": {"mode": .5}}}}):
            with self.assertRaises(GenerationError):
                build_automation(song, {"automation": bad})

    def test_rendered_midi_has_no_tempo_and_exact_length_channels_and_tick_zero_ccs(self):
        song = fixture()
        for score in build_automation(song, {}).values():
            data, rows = render_score(score, resolution=8, high_resolution=True)
            self.assertEqual(data, render_score(score, resolution=8, high_resolution=True)[0])
            fmt, division, tracks = decode_midi(data)
            self.assertEqual((fmt, division), (1, PPQ))
            self.assertFalse(any(event[1:3] == (255, 81) for track in tracks for event in track))
            self.assertTrue(all(events[-1][:3] == (song.ticks, 255, 47) for events in tracks))
            messages = [event for track in tracks for event in track if event[1] != 255]
            self.assertTrue(all(event[1] & 15 == 0 for event in messages))
            anchors = {event[2] for event in messages if event[0] == 0 and event[1] == 176}
            self.assertTrue({spec.cc for spec in TARGETS.values()} <= anchors)
            self.assertTrue(set(range(52, 64)) <= anchors)
            note_ons = [event[0] for event in messages if event[1] == 144 and event[3] > 0]
            self.assertEqual(note_ons, [round(t["beat"]*PPQ) for t in score["triggers"]])
            self.assertTrue(all(0 <= row["normalized"] <= 1 for row in rows))
            self.assertFalse(any(event[1] == 176 and event[2] == 121 for event in messages))


if __name__ == "__main__":
    unittest.main()
