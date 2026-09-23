"""Dense, straight-line piano-roll automation carried by note pitch.

Every sample is a separate short note, including repeated equal pitches.  The
files contain note-on/off messages and timing metadata only: no controllers or
pitch bend. Values map to FL Studio's C0..C10 (MIDI pitch 0..120), with velocity
following the same percentage. A zero value uses velocity 1 to remain a note.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import heapq
import math
import struct
from typing import Iterator

from .automation import build_automation, RECEIVER_ROLES
from .midi import encode_track, meta_event
from .model import BAR, PPQ, GenerationError, Song
from .vendor.motion_fx_codec import TARGETS

DENSITIES = (120, 240, 480, 960)
DEFAULT_DENSITY = 240
NOTE_MIN = 0
NOTE_MAX = 120
PITCH_MAX = NOTE_MAX
VELOCITY_MIN = 1
CONTINUOUS_TARGETS = tuple(name for name, spec in TARGETS.items() if not spec.choice)
TARGET_NAMES = {
    "motion": "Motion", "pump": "Pump Depth", "cutoff": "Cutoff",
    "resonance": "Resonance", "drive": "Drive", "width": "Width",
    "pan": "Pan", "delayMix": "Delay Mix", "feedback": "Delay Feedback",
    "reverbMix": "Reverb Mix", "gate": "Gate Depth", "mix": "Wet Mix",
    "output": "Output", "highpass": "High-pass", "motionCutoff": "Motion Cutoff",
    "motionPan": "Motion Pan", "motionWidth": "Motion Width", "attack": "Attack",
    "release": "Release", "threshold": "Threshold", "smooth": "Smoothing",
    "reverbSize": "Reverb Size",
}


def _check_song(song: Song) -> None:
    if not 1 <= song.bars <= 512 or song.ticks != song.bars * BAR:
        raise GenerationError("Note automation requires a song from 1 to 512 bars")


def _step(density: int) -> int:
    if isinstance(density, bool) or not isinstance(density, int) or density not in DENSITIES:
        raise GenerationError("Note density must be 120, 240, 480 or 960 notes per beat")
    return PPQ // density


def note_values(normalized: float) -> tuple[int, int]:
    """Quantize a shared percentage to pitch and visible-note velocity."""
    if isinstance(normalized, bool) or not isinstance(normalized, (int, float)) or not math.isfinite(normalized):
        raise GenerationError("Note automation value must be a finite number")
    value = min(1.0, max(0.0, normalized))
    return (int(math.floor(value * NOTE_MAX + .5)),
            max(VELOCITY_MIN, int(math.floor(value * 127 + .5))))


def _musical_values(regions: list[dict], total_ticks: int) -> list[float]:
    """Ignore the terminal reset when deciding whether a musical control moves."""
    values = []
    for region in regions:
        end = round((region["start_bar"] + region["length_bars"]) * BAR)
        points = region["points"][:-1] if end == total_ticks else region["points"]
        values.extend(point[1] for point in points)
    return values


def static_note_settings(song: Song, profile: dict) -> dict[str, dict[str, float]]:
    """Return initial physical settings for each receiver, including omitted constants.

    Discrete selectors are deliberately not represented as pitch automation.
    The generated musical policy keeps those selectors fixed during the song.
    """
    _check_song(song)
    result = {}
    for receiver, score in build_automation(song, profile).items():
        settings = {}
        for region in score["lanes"]:
            settings.setdefault(region["target"], region["points"][0][1])
        result[receiver] = settings
    return result


def build_note_lanes(song: Song, profile: dict) -> list[dict]:
    """Compile lightweight lane descriptions; never allocate full-song samples.

    Public fields identify the lane and its units.  Underscored fields contain
    normalized straight-line anchors or exact kick ticks for the renderer.
    """
    _check_song(song)
    result = []
    for receiver, score in build_automation(song, profile).items():
        grouped = {name: [] for name in TARGETS}
        for region in score["lanes"]:
            grouped[region["target"]].append(region)
        kicks = sorted({round(trigger["beat"] * PPQ) for trigger in score["triggers"]
                        if trigger["note"] == 36})
        if kicks and any(value > 0 for value in _musical_values(grouped["pump"], song.ticks)):
            result.append({"id": f"{receiver}_motion", "receiver": receiver,
                           "target": "motion", "name": f"{receiver.title()} / Motion",
                           "shape": "linear", "initial_value": 1.0 if kicks[0] == 0 else 0.0,
                           "final_value": 0.0, "unit": "normalized", "channel": 1,
                           "_kick_ticks": kicks, "_terminal": 0.0})
        for target in CONTINUOUS_TARGETS:
            regions = sorted(grouped[target], key=lambda region: region["start_bar"])
            values = _musical_values(regions, song.ticks)
            if not values or max(values) - min(values) <= 1e-10:
                continue
            spec = TARGETS[target]
            ticks, normalized = [], []
            for region in regions:
                for position, value in region["points"]:
                    ticks.append((region["start_bar"] + position * region["length_bars"]) * BAR)
                    normalized.append(min(1.0, max(0.0, spec.normalize(value))))
            result.append({"id": f"{receiver}_{target}", "receiver": receiver,
                           "target": target, "name": f"{receiver.title()} / {TARGET_NAMES[target]}",
                           "shape": "linear", "initial_value": regions[0]["points"][0][1],
                           "final_value": regions[-1]["points"][-1][1], "unit": spec.unit,
                           "channel": 1, "_ticks": ticks, "_values": normalized,
                           "_terminal": normalized[-1]})
    # Sound-set layers retain their own source names and routing identity. The
    # source arrays are immutable here and shared between matching layers.
    expanded = []
    for track in song.tracks:
        for lane in result:
            if track.role not in RECEIVER_ROLES[lane["receiver"]]:
                continue
            expanded.append({**lane, "id": f"{track.id}_{lane['target']}",
                             "name": f"{track.name} | {TARGET_NAMES[lane['target']]}",
                             "source_track_id": track.id, "source_track_name": track.name,
                             "source_channel": track.channel})
    return expanded


def evaluate_note_lane(song: Song, lane: dict, tick: float) -> float:
    """Evaluate normalized value using straight segments; never ease or curve."""
    if not math.isfinite(tick) or tick < 0 or tick > song.ticks:
        raise GenerationError("Automation preview tick is outside the song")
    if tick >= song.ticks:
        return lane["_terminal"]
    if lane["target"] == "motion":
        kicks = lane["_kick_ticks"]
        index = bisect_right(kicks, tick) - 1
        return max(0.0, 1.0 - (tick - kicks[index]) / PPQ) if index >= 0 else 0.0
    ticks, values = lane["_ticks"], lane["_values"]
    index = bisect_right(ticks, tick) - 1
    if index < 0:
        return values[0]
    if index >= len(ticks) - 1:
        return values[-1]
    fraction = (tick - ticks[index]) / (ticks[index + 1] - ticks[index])
    return values[index] + fraction * (values[index + 1] - values[index])


def _notes(song: Song, lane: dict, density: int, start: int = 0,
           end: int | None = None) -> Iterator[dict]:
    step = _step(density)
    end = song.ticks if end is None else min(song.ticks, end)
    start = max(0, start)
    if end <= start:
        return
    first = start // step * step
    stop = min(song.ticks, ((end + step - 1) // step) * step)
    grid = range(first, stop + 1, step)
    # A kick that falls between grid points splits that tiny note, preserving
    # exact timing without ever increasing the maximum note length.
    kicks = lane.get("_kick_ticks", [])
    extra = kicks[bisect_left(kicks, first):bisect_right(kicks, stop)]
    previous = None
    for boundary in heapq.merge(grid, extra):
        if boundary == previous:
            continue
        if previous is not None and boundary > start and previous < end:
            value = (lane["_terminal"] if boundary == song.ticks
                     else evaluate_note_lane(song, lane, previous))
            pitch, velocity = note_values(value)
            yield {"start": previous, "duration": boundary - previous,
                   "pitch": pitch, "velocity": velocity}
        previous = boundary


def preview_note_lane(song: Song, lane: dict, density: int = DEFAULT_DENSITY,
                      start_bar: float = 0, bars: float = 2) -> list[dict]:
    """Return the actual exported note rectangles intersecting a bounded view."""
    _check_song(song)
    if (isinstance(start_bar, bool) or not isinstance(start_bar, (int, float))
            or not math.isfinite(start_bar) or not 0 <= start_bar < song.bars):
        raise GenerationError("Preview start bar must lie inside the song")
    if (isinstance(bars, bool) or not isinstance(bars, (int, float))
            or not math.isfinite(bars) or not 0 < bars <= 8):
        raise GenerationError("Preview length must be greater than zero and at most 8 bars")
    return list(_notes(song, lane, density, round(start_bar * BAR),
                       round(min(song.bars, start_bar + bars) * BAR)))


def render_note_lane(song: Song, lane: dict, density: int = DEFAULT_DENSITY) -> tuple[bytes, dict]:
    """Render one lane at the exact requested density, independent of song length.

    Writing the note messages directly avoids millions of allocated event
    objects, a global event sort, and the sound-set reader's import-event cap.
    """
    _check_song(song)
    step = _step(density)
    title = lane["name"].encode("utf-8")
    conductor = [(0, 0, meta_event(3, title)),
                 (0, 2, meta_event(0x58, bytes((4, 2, 24, 8))))]
    conductor.extend((section.start_bar * BAR, 3, meta_event(6, section.name.encode("utf-8")))
                     for section in song.sections)
    conductor_chunk = encode_track(conductor, song.ticks)
    body = bytearray(b"\x00" + meta_event(3, title)
                     + b"\x00" + meta_event(4, lane["source_track_name"].encode("utf-8"))
                     + b"\x00" + meta_event(1, b"Straight note automation: FL C0=0%, C10=100%; velocity follows value (minimum 1).")
                     + b"\x00" + meta_event(0x20, b"\x00"))
    count, low, high, max_jump, previous_pitch = 0, NOTE_MAX, 0, 0, None
    min_velocity, max_velocity = 127, 1
    # Note lengths are at most eight ticks, so each delta is a single VLQ byte.
    for note in _notes(song, lane, density):
        pitch = note["pitch"]
        body.extend((0, 0x90, pitch, note["velocity"], note["duration"], 0x80, pitch, 0))
        count += 1
        low, high = min(low, pitch), max(high, pitch)
        min_velocity, max_velocity = min(min_velocity, note["velocity"]), max(max_velocity, note["velocity"])
        if previous_pitch is not None:
            max_jump = max(max_jump, abs(pitch - previous_pitch))
        previous_pitch = pitch
    body.extend(b"\x00\xff\x2f\x00")
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 2, PPQ)
    data = header + conductor_chunk + b"MTrk" + struct.pack(">I", len(body)) + body
    stats = {"id": lane["id"], "name": lane["name"], "receiver": lane["receiver"],
             "target": lane["target"], "shape": "linear", "encoding": "note_pitch",
             "channel": 1, "ppq": PPQ, "notes_per_beat": density,
             "note_length_ticks": step, "maximum_note_length_ticks": step,
             "note_length_ms": step / PPQ * 60_000 / song.config.bpm,
             "note_count": count, "notes": count, "min_pitch": low, "max_pitch": high,
             "max_pitch_step": max_jump, "end_tick": song.ticks,
             "pitch_mapping": "C0=0%, C10=100% (MIDI notes 0..120)",
             "velocity_mapping": "Same normalized value, scaled 0..127; minimum 1 keeps zero notes visible",
             "min_velocity": min_velocity, "max_velocity": max_velocity,
             "pitch_minimum": NOTE_MIN, "pitch_maximum": NOTE_MAX,
             "initial_value": lane["initial_value"], "final_value": lane["final_value"],
             "unit": lane["unit"], "midi_bytes": len(data),
             "source_track_id": lane["source_track_id"], "source_track_name": lane["source_track_name"],
             "source_channel": lane["source_channel"]}
    return data, stats
