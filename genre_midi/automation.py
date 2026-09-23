"""Compile separate Motion FX control scores from the musical arrangement.

Each receiver uses MIDI channel 1 on its own route.  The saved/default Motion FX
curve must be the factory descending ``(1 - phase) ** 3`` pump curve: curve
points, bypass and MIDI receive channel have no mapped CCs.  Mode 1 and cycle 3
make each actual kick retrigger a one-beat envelope; no host-clock pump is used.

Genre policies supply physical parameter values.  The reusable compiler owns
section continuity, exact kick timing, role guardrails and explicit end states.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from .model import BAR, PPQ, BEATS_PER_BAR, SECTION_KINDS, GenerationError, Song
from .vendor.motion_fx_codec import TARGETS, validate_score

ROLES = ("lead", "bass", "pad")
RECEIVER_ROLES = {"lead": frozenset(("lead", "chords", "arp", "screech", "pluck", "stab", "vocal")),
                  "bass": frozenset(("bass", "sub")), "pad": frozenset(("pad",))}

# These are deliberate initial/final values, not guesses about prior plug-in state.
NEUTRAL = {
    "pump": 0, "cutoff": 20000, "resonance": 0.2, "drive": 0,
    "width": 1, "pan": 0, "delayMix": 0, "feedback": 0.2,
    "reverbMix": 0, "gate": 0, "mix": 1, "output": -3,
    "highpass": 20, "motionCutoff": 0, "motionPan": 0,
    "motionWidth": 0, "attack": 2, "release": 180, "threshold": -24,
    "smooth": 10, "cycle": 3, "mode": 1, "filterType": 3,
    "delayTime": 2, "reverbSize": 0.65,
}

# Profiles may tighten these guardrails; they cannot silently turn bass into
# a stereo effect or remove its low end. Motion source and envelope cycle stay fixed.
ROLE_LIMITS = {
    "lead": {"pump": [0, 0.38], "drive": [0, 5], "output": [-12, -3],
             "width": [0.8, 1.5], "pan": [-0.16, 0.16], "cutoff": [900, 20000],
             "highpass": [20, 450], "reverbMix": [0, 0.30],
             "delayMix": [0, 0.24], "feedback": [0, 0.45]},
    "bass": {"pump": [0, 0.45], "drive": [0, 7], "output": [-12, -3],
             "width": [0, 1], "pan": [0, 0], "motionPan": [0, 0],
             "motionWidth": [0, 0], "motionCutoff": [0, 0],
             "highpass": [20, 90], "reverbMix": [0, 0.035],
             "delayMix": [0, 0.02], "feedback": [0, 0.22]},
    "pad": {"pump": [0, 0.28], "drive": [0, 2], "output": [-14, -4],
            "width": [1, 1.7], "pan": [-0.22, 0.22], "highpass": [20, 500],
            "reverbMix": [0, 0.44], "delayMix": [0, 0.30],
            "feedback": [0, 0.5]},
}


def _hardstyle_policy(style: str) -> dict:
    """Only this policy contains hardstyle production choices; the compiler is shared."""
    amounts = {
        "intro": {
            "lead": {"cutoff": 6500, "highpass": 140, "width": 1.08, "output": -6, "reverbMix": .10},
            "bass": {"cutoff": 6500, "pump": .24, "drive": 1.2, "output": -5},
            "pad": {"cutoff": 6500, "highpass": 180, "width": 1.3, "reverbMix": .24, "output": -8},
        },
        "build": {
            "lead": {"cutoff": 17500, "highpass": 320, "pump": .15, "drive": 2.2, "width": 1.25,
                     "delayMix": .18, "feedback": .37, "reverbMix": .27, "output": -6},
            "bass": {"cutoff": 10000, "highpass": 65, "pump": .20, "drive": 2.5, "output": -6},
            "pad": {"cutoff": 14000, "highpass": 350, "width": 1.6, "reverbMix": .40,
                    "delayMix": .18, "feedback": .42, "output": -8},
        },
        "drop": {
            "lead": {"cutoff": 19000, "highpass": 105, "pump": .30, "drive": 2.2, "width": 1.3,
                     "delayMix": .07, "feedback": .26, "reverbMix": .09, "output": -4.5},
            "bass": {"cutoff": 18000, "highpass": 20, "pump": .38, "drive": 3.0, "output": -5},
            "pad": {"cutoff": 10500, "highpass": 230, "pump": .23, "width": 1.4,
                    "reverbMix": .19, "delayMix": .07, "output": -8},
        },
        "breakdown": {
            "lead": {"cutoff": 5800, "highpass": 145, "width": 1.18, "reverbMix": .24,
                     "delayMix": .15, "feedback": .36, "output": -7},
            "bass": {"cutoff": 3500, "output": -7},
            "pad": {"cutoff": 7600, "highpass": 190, "width": 1.65, "reverbMix": .42,
                    "delayMix": .22, "feedback": .44, "reverbSize": .85, "output": -7},
        },
        "outro": {
            "lead": {"cutoff": 4200, "highpass": 180, "width": 1.05, "reverbMix": .12, "output": -8},
            "bass": {"cutoff": 5000, "pump": .26, "output": -6},
            "pad": {"cutoff": 4200, "highpass": 210, "width": 1.25, "reverbMix": .20, "output": -9},
        },
    }
    # Related styles retain coherent arrangement gestures with different timbre/space.
    colors = {
        "euphoric": {"space": 1.0, "drive": 1.0, "width": 1.0},
        "raw": {"space": .56, "drive": 1.65, "width": .6},
        "classic": {"space": .78, "drive": .75, "width": .75},
    }.get(style, {"space": 1.0, "drive": 1.0, "width": 1.0})
    for section in amounts.values():
        for values in section.values():
            for target in ("reverbMix", "delayMix"):
                if target in values:
                    values[target] *= colors["space"]
            if "drive" in values:
                values["drive"] *= colors["drive"]
            if "width" in values:
                values["width"] = 1 + (values["width"] - 1) * colors["width"]
            values["filterType"] = 0  # low-pass; independent high-pass remains separate
    return {"section_amounts": amounts, "role_limits": {}, "transition_beats": 2.0,
            "predrop_gap_beats": 1.0, "break_pan": {"lead": .07, "bass": 0, "pad": .17}}


POLICY_FACTORIES = {"hardstyle": _hardstyle_policy}


def _physical_number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise GenerationError(f"{label} must be a finite number from {low:g} to {high:g}")
    return float(value)


def _limits(role: str, policy: dict) -> dict[str, tuple[float, float]]:
    limits = {name: (target.low, target.high) for name, target in TARGETS.items()}
    limits.update({name: tuple(values) for name, values in ROLE_LIMITS[role].items()})
    limits.update({"mode": (1, 1), "cycle": (3, 3), "motionPan": (0, 0),
                   "motionWidth": (0, 0), "motionCutoff": (0, 0)})
    for target, interval in policy.get("role_limits", {}).get(role, {}).items():
        low, high = limits[target]
        low, high = max(low, interval[0]), min(high, interval[1])
        if low > high:
            raise GenerationError(f"automation.role_limits.{role}.{target} conflicts with receiver guardrails")
        limits[target] = low, high
    return limits


def _merge_profile(policy: dict, override: Any) -> dict:
    if not isinstance(override, dict):
        raise GenerationError("Profile automation must be an object")
    unknown = set(override) - {"section_amounts", "role_limits", "transition_beats", "predrop_gap_beats", "break_pan"}
    if unknown:
        raise GenerationError("Unknown automation fields: " + ", ".join(sorted(unknown)))
    policy = deepcopy(policy)
    for field in ("transition_beats", "predrop_gap_beats"):
        if field in override:
            policy[field] = _physical_number(override[field], f"automation.{field}", 0.0625, 16)
    for field in ("role_limits", "section_amounts"):
        content = override.get(field, {})
        if not isinstance(content, dict):
            raise GenerationError(f"automation.{field} must be an object")
        groups = content.items() if field == "section_amounts" else [(None, content)]
        for section, roles in groups:
            if section is not None and section not in SECTION_KINDS:
                raise GenerationError(f"Unknown automation section: {section}")
            if not isinstance(roles, dict):
                raise GenerationError(f"automation.{field} entries must be role objects")
            for role, values in roles.items():
                if role not in ROLES or not isinstance(values, dict):
                    raise GenerationError(f"automation.{field} must use lead, bass or pad parameter objects")
                for target, value in values.items():
                    if target not in TARGETS:
                        raise GenerationError(f"Unknown Motion FX automation target: {target}")
                    spec = TARGETS[target]
                    if field == "role_limits":
                        if not isinstance(value, list) or len(value) != 2:
                            raise GenerationError(f"automation.role_limits.{role}.{target} requires [minimum, maximum]")
                        numbers = [_physical_number(v, target, spec.low, spec.high) for v in value]
                        if numbers[0] > numbers[1]:
                            raise GenerationError(f"automation.role_limits.{role}.{target} minimum exceeds maximum")
                        destination = policy.setdefault(field, {}).setdefault(role, {})
                    else:
                        numbers = _physical_number(value, target, spec.low, spec.high)
                        destination = policy.setdefault(field, {}).setdefault(section, {}).setdefault(role, {})
                    if spec.choice and any(v != int(v) for v in (numbers if isinstance(numbers, list) else [numbers])):
                        raise GenerationError(f"Automation choice {target} requires integer values")
                    destination[target] = numbers
    if "break_pan" in override:
        values = override["break_pan"]
        if not isinstance(values, dict) or set(values) - set(ROLES):
            raise GenerationError("automation.break_pan requires lead, bass or pad amounts")
        for role, value in values.items():
            policy.setdefault("break_pan", {})[role] = _physical_number(value, "break_pan", 0, .22)
    return policy


def _kick_starts(song: Song) -> list[tuple[int, int]]:
    """Layered or unison kicks cause one envelope, preserving the strongest hit."""
    hits: dict[int, int] = {}
    for track in song.tracks:
        if track.role != "kick":
            continue
        for note in track.notes:
            if 0 <= note.start < song.ticks:
                hits[note.start] = max(hits.get(note.start, 0), note.velocity)
    return sorted(hits.items())


def compile_role_score(song: Song, role: str, policy: dict) -> dict:
    """Compile one receiver using numeric policy data, without genre conditionals.

    Adjacent lanes retain their previous end values except explicit drop resets,
    choice switches and disabling pump immediately when a section has no kick.
    All 25 controls finish at a dry, centered state with modest output headroom.
    """
    if role not in ROLES:
        raise GenerationError("Automation receiver must be lead, bass or pad")
    if not song.sections or song.sections[0].start_bar != 0:
        raise GenerationError("Automation requires a song beginning at bar zero")
    for previous, current in zip(song.sections, song.sections[1:]):
        if current.start_bar != previous.end_bar:
            raise GenerationError("Automation requires contiguous song sections")
    limits = _limits(role, policy)

    def bounded(target: str, value: float) -> float:
        low, high = limits[target]
        return min(high, max(low, value))

    neutral = {target: bounded(target, value) for target, value in NEUTRAL.items()}
    kicks = _kick_starts(song)
    triggers = []
    for index, (tick, velocity) in enumerate(kicks):
        next_tick = kicks[index + 1][0] if index + 1 < len(kicks) else song.ticks
        duration = min(PPQ // 16, next_tick - tick, song.ticks - tick)
        if duration > 0:
            triggers.append({"beat": tick / PPQ, "length_beats": duration / PPQ,
                             "note": 36, "velocity": velocity})
    lanes = []
    current = dict(neutral)
    for index, section in enumerate(song.sections):
        amount = .35 + .65 * (song.config.energy * .5 + section.energy * .5)
        target = dict(neutral)
        target.update(policy.get("section_amounts", {}).get(section.kind, {}).get(role, {}))
        for name in ("pump", "drive", "reverbMix", "delayMix", "pan"):
            target[name] *= amount
        target["width"] = 1 + (target["width"] - 1) * amount
        has_kick = any(section.start_bar * BAR <= tick < section.end_bar * BAR for tick, _ in kicks)
        if not has_kick:
            target["pump"] = 0
        if role == "bass" and section.kind == "drop":
            target["highpass"] = 20
        target = {name: bounded(name, value) for name, value in target.items()}
        # Hard lower-end guardrail also applies when a profile requests a conflicting floor.
        if role == "bass" and section.kind == "drop":
            target["highpass"] = 20
        start = dict(target) if index == 0 or section.kind == "drop" else dict(current)
        if not has_kick:
            start["pump"] = 0
        for name, spec in TARGETS.items():
            if spec.choice:
                start[name] = target[name]
        before_drop = index + 1 < len(song.sections) and song.sections[index + 1].kind == "drop"
        predrop = section.kind == "build" and before_drop
        length_beats = section.bars * BEATS_PER_BAR
        transition = min(.35, policy.get("transition_beats", 2.0) / length_beats)
        gap = min(.25, policy.get("predrop_gap_beats", 1.0) / length_beats)
        finish = dict(target)
        if predrop:
            finish.update({"delayMix": 0, "reverbMix": 0, "drive": 0, "pump": 0,
                           "pan": 0, "output": -8 if role == "pad" else -7})
            if role == "lead":
                finish["cutoff"] = bounded("cutoff", 1700)
        for name, spec in TARGETS.items():
            if section.kind == "build":
                peak_position = 1 - gap * 2 if predrop else .9
                points = [[0, start[name]], [peak_position, target[name]]]
                if predrop:
                    points.append([1 - gap, bounded(name, finish[name])])
                points.append([1, bounded(name, finish[name])])
            else:
                points = [[0, start[name]], [transition, target[name]], [1, target[name]]]
            # Slow explicit panning works in quiet breakdowns without fake MIDI kicks.
            pan_amount = bounded("pan", policy.get("break_pan", {}).get(role, 0) * amount)
            if section.kind == "breakdown" and name == "pan" and pan_amount:
                points = [[0, start[name]], [.25, pan_amount], [.75, -pan_amount], [1, 0]]
            if index == len(song.sections) - 1:
                # Hold the musical gesture to the final tick, then neutralize every
                # mapped control. No CC121: it would restore unknown saved values.
                points[-1][0] = 1 - 1 / (section.bars * BAR)
                points.append([1, neutral[name]])
            lanes.append({"name": f"{section.name}: {name}", "target": name,
                          "shape": "points", "start_bar": section.start_bar,
                          "length_bars": section.bars, "points": points,
                          "interpolation": "hold" if spec.choice else "smoothstep"})
            current[name] = points[-1][1]
    return validate_score({
        "name": f"{song.config.genre.title()} / {role.title()} / Motion FX",
        "description": f"Route only to Motion FX on {role}; eligible tracks: "
                       + ", ".join(track.name for track in song.tracks if track.role in RECEIVER_ROLES[role])
                       + ". MIDI channel 1, bypass off, factory descending pump curve. "
                       "Mode 1; note 36 follows exact kick onsets; three receivers require separate MIDI routes. "
                       "All 25 mapped controls have explicit start/end states. CC cannot set curve points.",
        "bpm": song.config.bpm, "bars": song.bars, "beats_per_bar": BEATS_PER_BAR,
        "channel": 1, "lanes": lanes, "triggers": triggers, "release_at_end": False,
    })


def build_automation(song: Song, profile: dict) -> dict[str, dict]:
    """Return lead/bass/pad scores for independent Motion FX instances.

    Profiles optionally define ``automation.section_amounts[kind][role][target]``
    in physical units, and ``automation.role_limits[role][target] = [min, max]``.
    Add another numeric policy factory or supply these fields for future genres.
    """
    factory = POLICY_FACTORIES.get(song.config.genre)
    policy = factory(song.config.style) if factory else {"section_amounts": {}, "role_limits": {}}
    policy = _merge_profile(policy, profile.get("automation", {}))
    present = {track.role for track in song.tracks}
    return {role: compile_role_score(song, role, policy) for role in ROLES
            if present & RECEIVER_ROLES[role]}
