"""Independent SMF fixtures for instrument-lane import, assignment and filling."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import hashlib
import io
import shutil
import struct
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from genre_midi.model import Config, Note, Track, Song, Section, GenerationError, PPQ
from genre_midi.midi import parse_midi, song_midi
from genre_midi.sound_sets import inspect_sound_set, fill_sound_set


def vlq(number):
    parts = [number & 127]
    while number >= 128:
        number >>= 7
        parts.insert(0, (number & 127) | 128)
    return bytes(parts)


def meta(kind, payload):
    return bytes((255, kind)) + vlq(len(payload)) + payload


def body(events, end=True):
    result = b"".join(vlq(delta) + message for delta, message in events)
    return result + (b"\x00\xff\x2f\x00" if end else b"")


def smf(track_bodies, fmt=1, ppq=480):
    header = b"MThd" + struct.pack(">IHHH", 6, fmt, len(track_bodies), ppq)
    return header + b"".join(b"MTrk" + struct.pack(">I", len(track)) + track for track in track_bodies)


def lane(name, channel=1, program=81, pitch=60, controls=()):
    channel -= 1
    events = [(0, meta(3, name.encode("utf-8")))]
    events += [(0, bytes((176 | channel, cc, value))) for cc, value in controls]
    if program is not None:
        events.append((0, bytes((192 | channel, program))))
    if pitch is not None:
        events += [(0, bytes((144 | channel, pitch, 100))), (480, bytes((128 | channel, pitch, 0)))]
    return body(events)


CONDUCTOR = body([(0, meta(3, b"Conductor")), (0, meta(81, b"\x06\x1a\x80"))])


@contextmanager
def midi_file(data):
    # Use inherited workspace ACLs; restricted Windows tokens may not access the
    # owner-only directories produced by Python 3.14 TemporaryDirectory.
    parent = (ROOT / "Tests" / "_sound_set_test_output").resolve()
    parent.mkdir(exist_ok=True)
    folder = parent / uuid.uuid4().hex
    folder.mkdir()
    path = folder / "Instruments.mid"
    path.write_bytes(data)
    try:
        yield path
    finally:
        resolved = folder.resolve()
        resolved.relative_to(parent)
        if resolved.parent != parent or resolved == parent:
            raise AssertionError("Unsafe test cleanup path")
        shutil.rmtree(resolved)


def source_song(path, **kwargs):
    config = Config(sound_set=str(path), **kwargs)
    notes = [Note(0, PPQ//2, 66, 104), Note(PPQ, PPQ//2, 69, 97),
             Note(2*PPQ, PPQ//2, 73, 107)]
    tracks = [Track(role, role, role, i+1, notes=list(notes))
              for i, role in enumerate(("lead", "bass", "pad", "arp", "chords", "screech", "fx", "kick"))]
    tracks += [Track("snare", "Snare", "snare", 10, notes=[Note(0, 100, 38, 100)]),
               Track("hat", "Hat", "closed_hat", 10, notes=[Note(0, 100, 42, 75), Note(PPQ, 100, 42, 85)])]
    return Song(config, [Section("drop", "Drop", 0, 1, .8)], tracks, [])


class SoundSetTests(unittest.TestCase):
    def test_exact_whitespace_in_original_name_survives_import_and_export(self):
        name = "  Lead\t Main  "
        whitespace_then_name = body([(0, meta(3, b" \t ")), (0, meta(3, name.encode())),
                                     (0, bytes((192, 81)))])
        with midi_file(smf([CONDUCTOR, whitespace_then_name])) as path:
            info = inspect_sound_set(path)
            self.assertEqual(info["slots"][0]["name"], name)
            song = fill_sound_set(source_song(path), info)
            document = parse_midi(song_midi(song))
            exported_name = next(e.data.decode() for e in document.tracks[1] if e.meta_type == 3)
            self.assertEqual(exported_name, name)

    def test_lane_data_and_hash_use_a_single_bounded_source_snapshot(self):
        old_source = smf([CONDUCTOR, lane("Old Lead")])
        new_source = smf([CONDUCTOR, lane("New Pad", 7, 89)])
        with midi_file(old_source) as path:
            with patch.object(Path, "open", return_value=io.BytesIO(new_source)) as opened:
                info = inspect_sound_set(path)
                opened.assert_called_once_with("rb")
            self.assertEqual(info["slots"][0]["name"], "New Pad")
            self.assertEqual(info["sha256"], hashlib.sha256(new_source).hexdigest())

    def test_preserves_order_duplicate_names_channels_program_banks_and_source(self):
        data = smf([CONDUCTOR, lane("Lead · A", 3, 81, 60, [(0, 4), (32, 9), (7, 110)]),
                    lane("Lead · A", 5, 83, 72), lane("Pad", 9, 89, 48), lane("Kick", 10, None, 36)])
        with midi_file(data) as path:
            info = inspect_sound_set(path)
            original_info = deepcopy(info)
            song = source_song(path)
            original_config = song.config.to_dict()
            output = fill_sound_set(song, info)
            self.assertEqual([t.name for t in output.tracks], ["Lead · A", "Lead · A", "Pad", "Kick"])
            self.assertEqual([t.channel for t in output.tracks], [3, 5, 9, 10])
            self.assertEqual([t.program for t in output.tracks], [81, 83, 89, None])
            self.assertEqual(output.tracks[0].controls, [(0, 4), (32, 9), (7, 110)])
            self.assertEqual([t.id for t in output.tracks], ["track_1", "track_2", "track_3", "track_4"])
            self.assertEqual(output.metadata["sound_set"]["sha256"], hashlib.sha256(data).hexdigest())
            document = parse_midi(song_midi(output))
            names = [next(e.data.decode("utf-8") for e in track if e.meta_type == 3) for track in document.tracks[1:]]
            self.assertEqual(names, [t.name for t in output.tracks])
            initialization = [e for e in document.tracks[1] if e.status != 255 and e.tick == 0]
            self.assertEqual([(e.status, e.data) for e in initialization[:4]],
                             [(178, bytes((0, 4))), (178, bytes((32, 9))), (178, bytes((7, 110))), (194, bytes((81,)))])
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(song.config.to_dict(), original_config)
            self.assertEqual(info, original_info)

    def test_explicit_role_override_unassigned_unknown_mapping_and_skip(self):
        with midi_file(smf([CONDUCTOR, lane("Instrument 17", 2), lane("Odd name", 5)])) as path:
            info = inspect_sound_set(path)
            self.assertEqual([slot["role"] for slot in info["slots"]], ["unassigned", "unassigned"])
            with self.assertRaisesRegex(GenerationError, "Assign a role"):
                fill_sound_set(source_song(path), info)
            mapped = source_song(path, lane_roles={"track_1": "lead", "track_2": "skip"})
            output = fill_sound_set(mapped, info)
            self.assertEqual([t.role for t in output.tracks], ["lead", "skip"])
            self.assertTrue(output.tracks[0].notes)
            self.assertFalse(output.tracks[1].notes)
            self.assertEqual(output.tracks[1].name, "Odd name")
            with self.assertRaisesRegex(GenerationError, "missing slots"):
                fill_sound_set(source_song(path, lane_roles={"no-such-slot": "lead"}), info)
            with self.assertRaises(GenerationError):
                Config(lane_roles={"track_1": "imaginary"}).validate()
            with self.assertRaises(GenerationError):
                fill_sound_set(source_song(path, lane_roles={"track_1": "imaginary", "track_2": "lead"}), info)

    def test_every_lane_skipped_is_rejected(self):
        with midi_file(smf([CONDUCTOR, lane("Lead"), lane("Pad", 2)])) as path:
            info = inspect_sound_set(path)
            with self.assertRaisesRegex(GenerationError, "at least one"):
                fill_sound_set(source_song(path, lane_roles={s["id"]: "skip" for s in info["slots"]}), info)

    def test_empty_named_lanes_are_preserved_and_can_be_mapped(self):
        empty = body([(0, meta(3, b"Pad"))])
        unnamed = body([])
        with midi_file(smf([CONDUCTOR, lane("Lead", 3), empty, unnamed])) as path:
            info = inspect_sound_set(path)
            self.assertEqual(len(info["slots"]), 3)
            self.assertEqual(info["slots"][1]["source_note_count"], 0)
            self.assertEqual(info["slots"][2]["name"], "Track 4")
            output = fill_sound_set(source_song(path, lane_roles={"track_3": "arp"}), info)
            self.assertEqual([t.name for t in output.tracks], ["Lead", "Pad", "Track 4"])
            self.assertTrue(all(t.notes for t in output.tracks))
            self.assertTrue(any("no MIDI channel stored" in w for w in info["warnings"]))

    def test_empty_lane_retains_midi_channel_prefix(self):
        empty = body([(0, meta(3, b"Pad")), (0, meta(32, bytes((6,))))])
        with midi_file(smf([CONDUCTOR, empty])) as path:
            info = inspect_sound_set(path)
            self.assertEqual(info["slots"][0]["channel"], 7)
            self.assertEqual(fill_sound_set(source_song(path), info).tracks[0].channel, 7)

    def test_initial_bank_before_first_note_is_retained_even_after_zero(self):
        events = [(0, meta(3, b"Lead")), (0, bytes((176, 0, 1))),
                  (24, bytes((176, 0, 3))), (0, bytes((176, 32, 7))),
                  (0, bytes((192, 84))), (24, bytes((144, 60, 100))),
                  (240, bytes((128, 60, 0)))]
        with midi_file(smf([CONDUCTOR, body(events)])) as path:
            info = inspect_sound_set(path)
            self.assertEqual(info["slots"][0]["program"], 84)
            controls = dict(info["slots"][0]["controls"])
            self.assertEqual(controls.get(0), 3)
            self.assertEqual(controls.get(32), 7)

    def test_skipped_empty_lane_retains_channel_after_export_and_reimport(self):
        empty = body([(0, meta(3, b"Pad")), (0, meta(32, bytes((6,))))])
        with midi_file(smf([CONDUCTOR, lane("Lead", 2), empty])) as path:
            info = inspect_sound_set(path)
            # Explicit expected input also isolates the writer regression from
            # importer channel-prefix handling.
            info["slots"][1]["channel"] = 7
            result = fill_sound_set(source_song(path, lane_roles={"track_2": "skip"}), info)
            with midi_file(song_midi(result)) as generated:
                roundtrip = inspect_sound_set(generated)
                self.assertEqual(roundtrip["slots"][1]["name"], "Pad")
                self.assertEqual(roundtrip["slots"][1]["channel"], 7)
                self.assertEqual(roundtrip["slots"][1]["source_note_count"], 0)

    def test_drum_pitch_hints_and_pitched_versus_fixed_kicks(self):
        data = smf([CONDUCTOR, lane("Kick Sampler", 1, 38, 50), lane("Kick GM", 10, None, 35),
                    lane("Percussion", 10, None, 67), lane("Unnamed", 10, None, 46)])
        with midi_file(data) as path:
            info = inspect_sound_set(path)
            self.assertEqual(info["slots"][3]["role"], "open_hat")
            song = source_song(path, kick_mode="pitched")
            pitched = fill_sound_set(song, info)
            source_kick = next(t for t in song.tracks if t.role == "kick")
            self.assertEqual([n.pitch for n in pitched.tracks[0].notes], [n.pitch for n in source_kick.notes])
            self.assertEqual({n.pitch for n in pitched.tracks[1].notes}, {35})
            self.assertEqual({n.pitch for n in pitched.tracks[2].notes}, {67})
            fixed = fill_sound_set(source_song(path, kick_mode="fixed"), info)
            self.assertEqual({n.pitch for n in fixed.tracks[0].notes}, {50})
            self.assertEqual({n.pitch for n in fixed.tracks[1].notes}, {35})

    def test_multiple_lead_layers_remain_harmonic_and_timing_identical(self):
        with midi_file(smf([CONDUCTOR, lane("Lead Main", 2), lane("Lead Wide", 4), lane("Lead Third", 6)])) as path:
            info = inspect_sound_set(path)
            song = source_song(path)
            output = fill_sound_set(song, info)
            lead = song.tracks[0]
            for layer in output.tracks:
                self.assertEqual([(n.start, n.duration, n.velocity) for n in layer.notes],
                                 [(n.start, n.duration, n.velocity) for n in lead.notes])
                self.assertEqual([n.pitch % 12 for n in layer.notes], [n.pitch % 12 for n in lead.notes])
            self.assertEqual([n.pitch for n in output.tracks[1].notes], [n.pitch + 12 for n in lead.notes])
            self.assertEqual([n.pitch for n in output.tracks[2].notes], [n.pitch for n in lead.notes])

    def test_role_name_prefix_and_open_hat_specificity(self):
        with midi_file(smf([CONDUCTOR, lane("Lead - Airy Pad", 2), lane("Open hi-hat", 10, None, 46),
                           lane("Reverse Bass", 3), lane("Pad: Long Choir", 4)])) as path:
            self.assertEqual([s["role"] for s in inspect_sound_set(path)["slots"]],
                             ["lead", "open_hat", "bass", "pad"])

    def test_original_name_is_written_as_track_and_instrument_name(self):
        original=" I3  Lead - supersaw "
        with midi_file(smf([CONDUCTOR,lane(original,4)])) as path:
            info=inspect_sound_set(path)
            source=Song(Config(sound_set=str(path)),[Section("drop","Drop",0,1,1)],
                        [Track("lead","Lead","lead",1,81,[Note(0,120,72,100)])],[])
            output=fill_sound_set(source,info)
            events=parse_midi(song_midi(output)).tracks[1]
            self.assertEqual([e.data.decode() for e in events if e.meta_type==3],[original])
            self.assertEqual([e.data.decode() for e in events if e.meta_type==4],[original])

    def test_instrument_name_is_used_when_track_name_is_absent_or_generic(self):
        named=body([(0,meta(4,b"I4  Pad - wide")),(0,bytes((0xc3,89)))])
        generic=body([(0,meta(3,b"Track 2")),(0,meta(4,b"B1  Bass - sub")),(0,bytes((0xc1,38)))])
        with midi_file(smf([CONDUCTOR,named,generic])) as path:
            info=inspect_sound_set(path)
            self.assertEqual([s["name"] for s in info["slots"]],["I4  Pad - wide","B1  Bass - sub"])
            self.assertEqual([s["role"] for s in info["slots"]],["pad","bass"])

    def test_toolkit_open_hat_qualifier_works_with_and_without_placeholder(self):
        tracks = [CONDUCTOR, lane("D3  Hat - open 909", 10, 0, 46),
                  lane("D3  Hat - open 909", 10, 0, None),
                  lane("D6  Hat - closed 909", 10, 0, 42),
                  lane("Closed Hat - open air", 10, 0, 42)]
        with midi_file(smf(tracks)) as path:
            self.assertEqual([slot["role"] for slot in inspect_sound_set(path)["slots"]],
                             ["open_hat", "open_hat", "closed_hat", "closed_hat"])

    def test_multiple_channels_in_one_instrument_lane_are_rejected(self):
        mixed = body([(0, meta(3, b"Lead")), (0, bytes((144, 60, 80))), (1, bytes((145, 64, 80)))])
        with midi_file(smf([CONDUCTOR, mixed])) as path:
            with self.assertRaisesRegex(GenerationError, "multiple MIDI channels"):
                inspect_sound_set(path)

    def test_valid_running_status_program_note_and_zero_velocity(self):
        data = smf([body([(0, meta(3, b"Lead")), (0, bytes((194, 81))), (0, bytes((82,))),
                          (0, bytes((146, 60, 90))), (120, bytes((64, 70))),
                          (120, bytes((60, 0))), (0, bytes((64, 0)))])], fmt=0)
        doc = parse_midi(data)
        messages = [event for event in doc.tracks[0] if event.status != 255]
        self.assertEqual([event.status for event in messages], [194, 194, 146, 146, 146, 146])
        self.assertEqual([event.tick for event in messages], [0, 0, 0, 120, 240, 240])
        with midi_file(data) as path:
            slot = inspect_sound_set(path)["slots"][0]
            self.assertEqual((slot["program"], slot["channel"], slot["source_note_count"]), (82, 3, 2))

    def test_running_status_across_meta_matches_mido_compatibility(self):
        # Some exporters retain channel running status across text metadata.
        # Installed mido's parser accepts this; preserve that useful tolerance.
        data = smf([body([(0, bytes((144, 60, 90))), (0, meta(1, b"text")),
                          (120, bytes((64, 90))), (120, bytes((60, 0))), (0, bytes((64, 0)))])])
        notes = [event for event in parse_midi(data).tracks[0] if event.status == 144]
        self.assertEqual([(event.tick, event.data[0], event.data[1]) for event in notes],
                         [(0, 60, 90), (120, 64, 90), (240, 60, 0), (240, 64, 0)])

    def test_malformed_and_truncated_midi_rejected(self):
        valid = smf([lane("Lead")])
        cases = {
            "wrong header": b"no MIDI file here",
            "truncated track": valid[:-1],
            "missing running status": smf([body([(0, bytes((60, 80)))])]),
            "status byte in data": smf([body([(0, bytes((144, 60, 255)))])]),
            "unterminated variable integer": smf([b"\xff\xff\xff\xff\x01"]),
            "bad end of track payload": smf([b"\x00\xff\x2f\x01\x00"]),
            "data after end of track": smf([b"\x00\xff\x2f\x00\x00\x90\x3c\x40"]),
            "sysex cancels running status": smf([body([(0, bytes((144, 60, 90))), (0, b"\xf0\x01\xf7"), (0, bytes((64, 90)))])]),
            "format two": smf([lane("Lead")], fmt=2),
            "SMPTE timing": smf([lane("Lead")], ppq=0xe728),
            "missing mandatory end of track": smf([body([(0, bytes((192, 81)))], end=False)]),
            "trailing undeclared bytes": valid + b"unexpected tail",
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(GenerationError):
                    parse_midi(data)


if __name__ == "__main__":
    unittest.main()
