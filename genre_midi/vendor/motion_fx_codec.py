#!/usr/bin/env python3
"""Generate Motion FX automation clips using only the Python standard library.

Values are supplied in parameter units; conversion to MIDI matches the VST3.
See SCORE_FORMAT.md and MIDI_MAP.md beside this file for the public interface.
"""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import random
import struct
import sys
from typing import Any


PPQN = 960
MAX_EVENTS = 1_000_000
PRESETS = ("drop-pump", "build-rise", "break-wash", "transition-fill")
PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"


@dataclass(frozen=True)
class Target:
    cc: int
    low: float
    high: float
    unit: str
    logarithmic: bool = False
    choice: bool = False

    def normalize(self, value: float) -> float:
        if self.logarithmic:
            return math.log(value / self.low) / math.log(self.high / self.low)
        return (value - self.low) / (self.high - self.low)


# This table mirrors Source/AutomationParameters.h. Ratios are deliberately
# represented as 0..1 (or the documented range), not display percentages.
TARGETS: dict[str, Target] = {
    "pump": Target(20, 0, 1, "ratio"),
    "cutoff": Target(21, 30, 20000, "Hz", logarithmic=True),
    "resonance": Target(22, 0.1, 0.9, "ratio"),
    "drive": Target(23, 0, 24, "dB"),
    "width": Target(24, 0, 2, "x"),
    "pan": Target(25, -1, 1, "position"),
    "delayMix": Target(26, 0, 0.65, "ratio"),
    "feedback": Target(27, 0, 0.85, "ratio"),
    "reverbMix": Target(28, 0, 0.65, "ratio"),
    "gate": Target(29, 0, 1, "ratio"),
    "mix": Target(30, 0, 1, "ratio"),
    "output": Target(31, -24, 6, "dB"),
    "highpass": Target(71, 20, 3000, "Hz", logarithmic=True),
    "motionCutoff": Target(73, -4, 4, "octaves"),
    "motionPan": Target(74, 0, 1, "ratio"),
    "motionWidth": Target(75, 0, 1, "ratio"),
    "attack": Target(76, 0.1, 100, "ms", logarithmic=True),
    "release": Target(77, 10, 1000, "ms", logarithmic=True),
    "threshold": Target(78, -60, 0, "dB"),
    "smooth": Target(79, 1, 200, "ms", logarithmic=True),
    "cycle": Target(80, 0, 7, "index", choice=True),
    "mode": Target(81, 0, 2, "index", choice=True),
    "filterType": Target(82, 0, 3, "index", choice=True),
    "delayTime": Target(83, 0, 7, "index", choice=True),
    "reverbSize": Target(84, 0.1, 1, "ratio"),
}


class ScoreError(ValueError):
    """A readable error in a score or command line parameter."""


def _number(value: Any, label: str, low: float, high: float, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoreError(f"{label} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise ScoreError(f"{label} must be finite and between {low:g} and {high:g}")
    if integer and value != int(value):
        raise ScoreError(f"{label} must be an integer")
    return value


def _keys(obj: dict, allowed: set[str], label: str) -> None:
    unexpected = set(obj) - allowed
    if unexpected:
        raise ScoreError(f"{label}: unknown field(s): {', '.join(sorted(unexpected))}")


def _value(value: Any, target: Target, label: str) -> float:
    return _number(value, label, target.low, target.high, integer=target.choice)


def _rounded(value: float) -> int:
    return int(math.floor(value + 0.5))


def validate_score(score: Any) -> dict:
    """Return a validated copy with defaults expanded. Never silently clamp input."""
    if not isinstance(score, dict):
        raise ScoreError("The score must be a JSON object")
    score = copy.deepcopy(score)
    _keys(score, {"name", "description", "bpm", "bars", "beats_per_bar", "channel", "lanes", "triggers", "release_at_end"}, "score")
    for name in ("name", "description"):
        if name in score and not isinstance(score[name], str):
            raise ScoreError(f"{name} must be text")
    score["bpm"] = _number(score.get("bpm", 128), "bpm", 20, 400)
    score["bars"] = _number(score.get("bars", 8), "bars", 1 / PPQN, 4096)
    score["beats_per_bar"] = int(_number(score.get("beats_per_bar", 4), "beats_per_bar", 1, 16, integer=True))
    score["channel"] = int(_number(score.get("channel", 1), "channel", 1, 16, integer=True))
    if not isinstance(score.get("release_at_end", False), bool):
        raise ScoreError("release_at_end must be true or false")
    score.setdefault("release_at_end", False)
    score.setdefault("lanes", [])
    score.setdefault("triggers", [])
    if not isinstance(score["lanes"], list) or not isinstance(score["triggers"], list):
        raise ScoreError("lanes and triggers must be arrays")
    if not score["lanes"] and not score["triggers"]:
        raise ScoreError("The score needs at least one lane or trigger")
    if len(score["lanes"]) > 4096 or len(score["triggers"]) > 65536:
        raise ScoreError("Score has too many lanes or triggers")
    regions: dict[str, list[tuple[int, int, int]]] = {}
    total_beats = score["bars"] * score["beats_per_bar"]
    for index, lane in enumerate(score["lanes"]):
        prefix = f"lanes[{index}]"
        if not isinstance(lane, dict):
            raise ScoreError(f"{prefix} must be an object")
        _keys(lane, {"name", "target", "shape", "from", "to", "start_bar", "length_bars", "exponent", "cycles", "phase", "duty", "values", "points", "interpolation", "seed", "steps_per_beat"}, prefix)
        if "name" in lane and not isinstance(lane["name"], str):
            raise ScoreError(f"{prefix}.name must be text")
        target_name = lane.get("target")
        if not isinstance(target_name, str) or target_name not in TARGETS:
            raise ScoreError(f"{prefix}.target must be one of: {', '.join(TARGETS)}")
        target = TARGETS[target_name]
        shape = lane.setdefault("shape", "line")
        if shape not in ("line", "smoothstep", "exponential", "sine", "triangle", "steps", "points", "random-hold", "pump"):
            raise ScoreError(f"{prefix}.shape is not supported: {shape!r}")
        lane["start_bar"] = _number(lane.get("start_bar", 0), f"{prefix}.start_bar", 0, score["bars"])
        remaining_bars = score["bars"] - lane["start_bar"]
        lane["length_bars"] = _number(lane.get("length_bars", remaining_bars), f"{prefix}.length_bars", 1 / PPQN, score["bars"])
        end = lane["start_bar"] + lane["length_bars"]
        if end > score["bars"] + 1e-9:
            raise ScoreError(f"{prefix} extends past the end of the score")
        start_tick = _rounded(lane["start_bar"] * score["beats_per_bar"] * PPQN)
        end_tick = _rounded(end * score["beats_per_bar"] * PPQN)
        if start_tick >= end_tick:
            raise ScoreError(f"{prefix} is shorter than one MIDI tick")
        regions.setdefault(target_name, []).append((start_tick, end_tick, index))
        if shape in ("steps", "points"):
            forbidden = {"from", "to", "exponent", "duty", "seed", "steps_per_beat"} & set(lane)
        else:
            forbidden = {"values", "points", "interpolation"} & set(lane)
        if shape not in ("sine", "triangle", "steps", "pump"):
            forbidden |= {"cycles", "phase"} & set(lane)
        if shape != "random-hold":
            forbidden |= {"seed", "steps_per_beat"} & set(lane)
        if shape not in ("exponential", "pump"):
            forbidden |= {"exponent"} & set(lane)
        if shape != "pump":
            forbidden |= {"duty"} & set(lane)
        if shape != "points":
            forbidden |= {"interpolation", "points"} & set(lane)
        if shape != "steps":
            forbidden |= {"values"} & set(lane)
        if forbidden:
            raise ScoreError(f"{prefix}: field(s) do not apply to {shape}: {', '.join(sorted(forbidden))}")
        if shape not in ("steps", "points"):
            if "from" not in lane or "to" not in lane:
                raise ScoreError(f"{prefix} requires from and to values in parameter units")
            for key in ("from", "to"):
                _value(lane[key], target, f"{prefix}.{key}")
        if shape in ("sine", "triangle", "steps", "pump"):
            lane["cycles"] = _number(lane.get("cycles", lane["length_bars"] * score["beats_per_bar"] if shape == "pump" else 1), f"{prefix}.cycles", 0.001, 65536)
            lane["phase"] = _number(lane.get("phase", 0), f"{prefix}.phase", 0, 1)
        if shape in ("exponential", "pump"):
            lane["exponent"] = _number(lane.get("exponent", 2), f"{prefix}.exponent", 0.05, 20)
        if shape == "pump":
            lane["duty"] = _number(lane.get("duty", 0.08), f"{prefix}.duty", 0, 0.99)
        if shape == "steps":
            values = lane.get("values")
            if not isinstance(values, list) or not 1 <= len(values) <= 4096:
                raise ScoreError(f"{prefix}.values must contain 1 to 4096 parameter values")
            for position, value in enumerate(values):
                _value(value, target, f"{prefix}.values[{position}]")
        if shape == "points":
            points = lane.get("points")
            if not isinstance(points, list) or not 2 <= len(points) <= 4096:
                raise ScoreError(f"{prefix}.points needs 2 to 4096 [position, value] pairs")
            previous = -1.0
            for point_index, point in enumerate(points):
                if not isinstance(point, list) or len(point) != 2:
                    raise ScoreError(f"{prefix}.points[{point_index}] must be [position, value]")
                position = _number(point[0], f"{prefix}.points[{point_index}][0]", 0, 1)
                _value(point[1], target, f"{prefix}.points[{point_index}][1]")
                if position <= previous:
                    raise ScoreError(f"{prefix}.points positions must increase strictly")
                previous = position
            if points[0][0] != 0 or points[-1][0] != 1:
                raise ScoreError(f"{prefix}.points must start at position 0 and end at 1")
            lane.setdefault("interpolation", "linear")
            if lane["interpolation"] not in ("linear", "smoothstep", "hold"):
                raise ScoreError(f"{prefix}.interpolation must be linear, smoothstep, or hold")
        if shape == "random-hold":
            lane["seed"] = int(_number(lane.get("seed", 0), f"{prefix}.seed", 0, 2 ** 32 - 1, integer=True))
            lane["steps_per_beat"] = _number(lane.get("steps_per_beat", 2), f"{prefix}.steps_per_beat", 0.0625, 64)
    for target_name, sections in regions.items():
        sections.sort()
        for previous, current in zip(sections, sections[1:]):
            if current[0] < previous[1]:
                raise ScoreError(f"lanes[{previous[2]}] and lanes[{current[2]}] overlap on {target_name}; use adjacent sections")
    note_regions: dict[int, list[tuple[int, int, int]]] = {}
    for index, trigger in enumerate(score["triggers"]):
        prefix = f"triggers[{index}]"
        if not isinstance(trigger, dict):
            raise ScoreError(f"{prefix} must be an object")
        _keys(trigger, {"beat", "velocity", "note", "length_beats"}, prefix)
        trigger["beat"] = _number(trigger.get("beat"), f"{prefix}.beat", 0, total_beats)
        trigger["velocity"] = int(_number(trigger.get("velocity", 100), f"{prefix}.velocity", 1, 127, integer=True))
        trigger["note"] = int(_number(trigger.get("note", 36), f"{prefix}.note", 36, 38, integer=True))
        if trigger["note"] not in (36, 38):
            raise ScoreError(f"{prefix}.note must be 36 (curve trigger) or 38 (release MIDI overrides)")
        trigger["length_beats"] = _number(trigger.get("length_beats", min(0.0625, total_beats - trigger["beat"])), f"{prefix}.length_beats", 1 / PPQN, 16)
        if trigger["beat"] + trigger["length_beats"] > total_beats + 1e-9:
            raise ScoreError(f"{prefix} note-off extends past the score end")
        start_tick = _rounded(trigger["beat"] * PPQN)
        end_tick = _rounded((trigger["beat"] + trigger["length_beats"]) * PPQN)
        if end_tick <= start_tick:
            raise ScoreError(f"{prefix} needs at least one tick between note-on and note-off")
        note_regions.setdefault(trigger["note"], []).append((start_tick, end_tick, index))
    for note, regions in note_regions.items():
        regions.sort()
        for previous, current in zip(regions, regions[1:]):
            if current[0] < previous[1]:
                raise ScoreError(f"triggers[{previous[2]}] and triggers[{current[2]}] overlap on note {note}")
    return score


def _phase(lane: dict, x: float) -> float:
    total = lane["phase"] + lane["cycles"] * x
    return 0.0 if abs(total - round(total)) < 1e-10 else total % 1.0


def _evaluate(lane: dict, x: float, beat_length: float, random_values: list[float]) -> float:
    shape = lane["shape"]
    if shape == "steps":
        phase = _phase(lane, x)
        # At an exact final cycle boundary, keep the last step. The next
        # section's anchor (or the clip looping) supplies the next first step.
        if x == 1 and math.isclose(phase, 0, abs_tol=1e-10):
            return lane["values"][-1]
        return lane["values"][min(len(lane["values"]) - 1, int(phase * len(lane["values"]) + 1e-10))]
    if shape == "points":
        points = lane["points"]
        if x >= 1:
            return points[-1][1]
        for left, right in zip(points, points[1:]):
            if x < right[0] - 1e-12:
                fraction = (x - left[0]) / (right[0] - left[0])
                if lane["interpolation"] == "hold":
                    return left[1]
                if lane["interpolation"] == "smoothstep":
                    fraction = fraction * fraction * (3 - 2 * fraction)
                return left[1] + (right[1] - left[1]) * fraction
        return points[-1][1]
    if shape == "random-hold":
        index = min(len(random_values) - 1, int(x * beat_length * lane["steps_per_beat"] + 1e-9))
        fraction = random_values[index]
    elif shape == "line":
        fraction = x
    elif shape == "smoothstep":
        fraction = x * x * (3 - 2 * x)
    elif shape == "exponential":
        fraction = x ** lane["exponent"]
    elif shape == "sine":
        fraction = (1 - math.cos(2 * math.pi * _phase(lane, x))) / 2
    elif shape == "triangle":
        fraction = 1 - abs(2 * _phase(lane, x) - 1)
    else:  # pump: hold low briefly, then rise toward the high level.
        phase = _phase(lane, x)
        if x == 1 and math.isclose(phase, 0, abs_tol=1e-10):
            phase = 1
        fraction = max(0, (phase - lane["duty"]) / (1 - lane["duty"])) ** lane["exponent"]
    return lane["from"] + (lane["to"] - lane["from"]) * fraction


def _samples(lane: dict, beats_per_bar: int, resolution: int) -> list[tuple[int, float, bool]]:
    start_beat = lane["start_bar"] * beats_per_bar
    length = lane["length_bars"] * beats_per_bar
    start_tick = _rounded(start_beat * PPQN)
    end_tick = _rounded((start_beat + length) * PPQN)
    count = math.ceil(length * resolution)
    if count > MAX_EVENTS:
        raise ScoreError("Lane exceeds the one-million-sample safety limit; reduce bars or resolution")
    # Associate MIDI ticks with exact positions so section points and rhythm
    # transitions don't drift when their time is not on the sampling grid.
    positions = {_rounded((start_beat + min(length, i / resolution)) * PPQN): min(1, i / (resolution * length)) for i in range(count + 1)}
    positions[start_tick] = 0.0
    positions[end_tick] = 1.0
    shape = lane["shape"]
    breaks: list[float] = []
    if shape == "points":
        breaks = [point[0] for point in lane["points"]]
    elif shape in ("steps", "pump", "triangle", "sine"):
        divisions = len(lane["values"]) if shape == "steps" else 2 if shape in ("triangle", "sine") else 1
        boundary_count = math.ceil(lane["cycles"] * divisions) + 2
        if boundary_count > MAX_EVENTS:
            raise ScoreError("Lane has too many rhythm boundaries")
        for step in range(boundary_count):
            x = (step / divisions - lane["phase"]) / lane["cycles"]
            if 0 <= x <= 1:
                breaks.append(x)
            if shape == "pump":
                hold_end = (step + lane["duty"] - lane["phase"]) / lane["cycles"]
                if 0 <= hold_end <= 1:
                    breaks.append(hold_end)
    elif shape == "random-hold":
        num_steps = max(1, math.ceil(length * lane["steps_per_beat"]))
        if num_steps > MAX_EVENTS:
            raise ScoreError("Lane has too many random-hold steps")
        breaks = [i / (length * lane["steps_per_beat"]) for i in range(num_steps)]
    for x in breaks:
        positions[_rounded((start_beat + length * x) * PPQN)] = x
    # Explicit start/end anchors take precedence if quantization collapsed a
    # nearby intermediate boundary to either edge of the section.
    positions[start_tick] = 0
    positions[end_tick] = 1
    random_values: list[float] = []
    if shape == "random-hold":
        rng = random.Random(lane["seed"])
        random_values = [rng.random() for _ in range(max(1, math.ceil(length * lane["steps_per_beat"])))]
    target = TARGETS[lane["target"]]
    result = []
    for tick, x in sorted(positions.items()):
        value = _evaluate(lane, x, length, random_values)
        if target.choice:
            value = _rounded(value)
        result.append((tick, value, tick in (start_tick, end_tick)))
    return result


def _vlq(value: int) -> bytes:
    if not 0 <= value <= 0x0FFFFFFF:
        raise ScoreError("MIDI delta-time or metadata length is too large")
    encoded = [value & 0x7F]
    while value > 0x7F:
        value >>= 7
        encoded.insert(0, (value & 0x7F) | 0x80)
    return bytes(encoded)


def _meta(kind: int, data: bytes) -> bytes:
    return bytes((0xFF, kind)) + _vlq(len(data)) + data


def _track(name: str, events: list[tuple[int, bytes]], end_tick: int) -> bytes:
    body = bytearray(b"\x00" + _meta(3, name.encode("utf-8")))
    previous = 0
    # Python's stable sort preserves the endpoint/startpoint ordering of
    # adjacent sections and the MSB-before-LSB order of high-resolution pairs.
    for tick, data in sorted(events, key=lambda event: event[0]):
        body.extend(_vlq(tick - previous))
        body.extend(data)
        previous = tick
    body.extend(_vlq(end_tick - previous))
    body.extend(b"\xff\x2f\x00")
    return b"MTrk" + struct.pack(">I", len(body)) + body


def render_score(score: dict, *, resolution: int = 32, high_resolution: bool = False) -> tuple[bytes, list[dict]]:
    """Return (format-1 SMF bytes, emitted automation rows for CSV previews)."""
    score = validate_score(score)
    resolution = int(_number(resolution, "resolution", 1, PPQN, integer=True))
    if not isinstance(high_resolution, bool):
        raise ScoreError("high_resolution must be a bool")
    end_tick = _rounded(score["bars"] * score["beats_per_bar"] * PPQN)
    conductor = [(0, _meta(0x58, bytes((score["beats_per_bar"], 2, 24, 8))))]
    if score.get("description"):
        conductor.append((0, _meta(1, score["description"].encode("utf-8"))))
    channel = score["channel"] - 1
    tracks = [_track(score.get("name", "Motion FX automation"), conductor, end_tick)]
    grouped: dict[str, list[dict]] = {}
    for lane in score["lanes"]:
        grouped.setdefault(lane["target"], []).append(lane)
    rows: list[dict] = []
    # CC sorting makes exported bytes independent of distinct-lane ordering.
    for name in sorted(grouped, key=lambda target_name: TARGETS[target_name].cc):
        target = TARGETS[name]
        bits = 14 if high_resolution and 20 <= target.cc <= 31 else 7
        scale = (1 << bits) - 1
        events: list[tuple[int, bytes]] = []
        previous_value = None
        for lane in sorted(grouped[name], key=lambda item: item["start_bar"]):
            for tick, value, anchor in _samples(lane, score["beats_per_bar"], resolution):
                normalized = min(1, max(0, target.normalize(value)))
                encoded = _rounded(normalized * scale)
                if encoded == previous_value and not anchor:
                    continue
                previous_value = encoded
                msb, lsb = (encoded >> 7, encoded & 0x7F) if bits == 14 else (encoded, None)
                events.append((tick, bytes((0xB0 | channel, target.cc, msb))))
                if lsb is not None:
                    events.append((tick, bytes((0xB0 | channel, target.cc + 32, lsb))))
                rows.append({"tick": tick, "beat": tick / PPQN, "bar": tick / PPQN / score["beats_per_bar"], "target": name, "value": value, "normalized": normalized, "channel": channel + 1, "bits": bits, "cc": target.cc, "msb": msb, "lsb": "" if lsb is None else lsb, "anchor": int(anchor)})
                if len(rows) > MAX_EVENTS:
                    raise ScoreError("Score exceeds the one-million-event safety limit")
        tracks.append(_track(f"Motion FX / {name} / CC {target.cc}", events, end_tick))
    if score["triggers"] or score["release_at_end"]:
        # Put releases and note-offs before new note-ons at the same tick.
        note_events = []
        for trigger in score["triggers"]:
            tick = _rounded(trigger["beat"] * PPQN)
            off_tick = _rounded((trigger["beat"] + trigger["length_beats"]) * PPQN)
            note_events.append((tick, 1 if trigger["note"] == 38 else 2, bytes((0x90 | channel, trigger["note"], trigger["velocity"]))))
            note_events.append((off_tick, 0, bytes((0x80 | channel, trigger["note"], 0))))
        if score["release_at_end"]:
            note_events.append((end_tick, 3, bytes((0xB0 | channel, 121, 0))))
        events = [(tick, data) for tick, _, data in sorted(note_events, key=lambda event: (event[0], event[1]))]
        tracks.append(_track("Motion FX / triggers and override release", events, end_tick))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), PPQN)
    return header + b"".join(tracks), sorted(rows, key=lambda row: (row["tick"], row["cc"]))


def load_preset(name: str, *, bars: float | None = None) -> dict:
    """Load an included preset; --bars scales sections and rhythmic density."""
    if name not in PRESETS:
        raise ScoreError(f"Unknown preset: {name}")
    score = json.loads((PRESET_DIR / f"{name}.json").read_text(encoding="utf-8"))
    if bars is not None:
        bars = _number(bars, "bars", 1 / PPQN, 4096)
        factor = bars / score["bars"]
        for lane in score["lanes"]:
            lane["start_bar"] = lane.get("start_bar", 0) * factor
            lane["length_bars"] = lane.get("length_bars", score["bars"]) * factor
            if "cycles" in lane:
                lane["cycles"] *= factor
        # Presets use beat-grid note 36 triggers: regenerate the grid to retain
        # one trigger per quarter note when changing arrangement length.
        if score.get("triggers"):
            step = score.get("triggers", [{}])[0].get("length_beats", 0.0625)
            total_beats = bars * score.get("beats_per_bar", 4)
            score["triggers"] = [{"beat": beat, "note": 36, "velocity": 110, "length_beats": min(step, total_beats - beat)} for beat in range(math.ceil(total_beats)) if total_beats - beat >= 1 / PPQN]
        score["bars"] = bars
    return score


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        fields = ("tick", "beat", "bar", "target", "value", "normalized", "channel", "bits", "cc", "msb", "lsb", "anchor")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--preset", choices=PRESETS, help="Included musical arrangement (default: drop-pump)")
    source.add_argument("--score", type=Path, help="JSON score in actual parameter units")
    parser.add_argument("--bars", type=float, help="Preset length in bars; with --score, override duration without scaling lanes")
    parser.add_argument("--bpm", type=float, help="Reference BPM (default: score value or 128); exported MIDI has no tempo events")
    parser.add_argument("--channel", type=int, help="MIDI channel 1..16 (default: score value or 1)")
    parser.add_argument("--resolution", type=int, default=32, help="Samples per quarter-note beat, 1..960 (default: 32)")
    parser.add_argument("--high-resolution", action="store_true", help="Use paired 14-bit CCs for targets CC20..31")
    parser.add_argument("--output", type=Path, help="Destination .mid file")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacement of existing output files")
    parser.add_argument("--preview-csv", type=Path, help="Optional CSV of emitted automation values")
    parser.add_argument("--save-score", type=Path, help="Optional copy of the expanded, validated JSON score")
    parser.add_argument("--list-targets", action="store_true", help="Print units, ranges and CC mapping, then exit")
    parser.add_argument("--validate-input", action="store_true", help="Validate and compile in memory without writing files")
    args = parser.parse_args(argv)
    try:
        if args.list_targets:
            for name, target in TARGETS.items():
                mapping = f"CC {target.cc}" + (f" / LSB {target.cc + 32}" if 20 <= target.cc <= 31 else "")
                kind = "choice" if target.choice else "log" if target.logarithmic else "linear"
                print(f"{name:14} {mapping:18} {target.low:g}..{target.high:g} {target.unit:9} {kind}")
            return 0
        if args.score:
            score = json.loads(args.score.read_text(encoding="utf-8-sig"))
            if args.bars is not None:
                if not isinstance(score, dict):
                    raise ScoreError("The score must be a JSON object")
                score["bars"] = args.bars
        else:
            score = load_preset(args.preset or "drop-pump", bars=args.bars)
        for name in ("bpm", "channel"):
            value = getattr(args, name)
            if value is not None:
                if not isinstance(score, dict):
                    raise ScoreError("The score must be a JSON object")
                score[name] = value
        score = validate_score(score)
        data, rows = render_score(score, resolution=args.resolution, high_resolution=args.high_resolution)
        if args.validate_input:
            print(f"Valid: {len(score['lanes'])} lanes, {len(score['triggers'])} triggers, {len(rows)} automation values, {len(data)} MIDI bytes")
            return 0
        if args.output is None:
            parser.error("--output is required unless using --list-targets or --validate-input")
        paths = [path.resolve() for path in (args.output, args.preview_csv, args.save_score) if path is not None]
        if len(paths) != len(set(paths)):
            raise ScoreError("Output MIDI, preview CSV and saved score must have different paths")
        if args.score and args.score.resolve() in paths:
            raise ScoreError("Output paths must differ from the input score path")
        if not args.overwrite:
            existing = [path for path in paths if path.exists()]
            if existing:
                raise ScoreError(f"Output already exists: {existing[0]}; choose a new path or use --overwrite")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
        if args.preview_csv:
            write_csv(args.preview_csv, rows)
        if args.save_score:
            args.save_score.parent.mkdir(parents=True, exist_ok=True)
            args.save_score.write_text(json.dumps(score, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"Wrote {args.output} ({len(rows)} automation values; {score['bars']:g} bars at {score['bpm']:g} BPM; channel {score['channel']})")
        return 0
    except (ScoreError, OSError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
