"""Deterministic, phrase-based hardstyle composition from a genre data profile.

The engine produces notes, not audio. Its eight-bar hook and harmonic cycle are
shared across sections; small answer variations develop later phrases without
replacing the song's identity. No files are read or written by this module.
"""
from __future__ import annotations

import copy
import math
from collections import defaultdict
from ._common import _scale_note, _chord_tone, _voicing, _chord_name, _clean

from ..model import BAR, PPQ, ROOTS, SCALES, Chord, Config, GenerationError, Note, Section, Song, Track, stable_rng


_PITCH_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_TRACKS = (
    ("kick", "Kick · pitched tail", "kick", 1, 38),
    ("bass", "Bass · offbeat support", "bass", 2, 38),
    ("lead", "Lead · main hook", "lead", 3, 81),
    ("chords", "Chords · voiced harmony", "chords", 4, 90),
    ("pad", "Pad · atmosphere", "pad", 5, 89),
    ("arp", "Arp · rhythmic harmony", "arp", 6, 81),
    ("screech", "Screech · responses", "screech", 7, 81),
    ("fx", "FX · riser and impact cues", "fx", 8, 97),
    ("snare", "Snare · builds and fills", "snare", 10, None),
    ("clap", "Clap · backbeat", "clap", 10, None),
    ("closed_hat", "Closed Hat · pulse", "closed_hat", 10, None),
    ("open_hat", "Open Hat · offbeat", "open_hat", 10, None),
    ("cymbal", "Cymbal · crash and ride", "cymbal", 10, None),
)


def _sections(config: Config, profile: dict) -> list[Section]:
    requested = config.sections if config.sections is not None else profile["arrangements"][config.arrangement]
    result, counts, start = [], defaultdict(int), 0
    for item in requested:
        kind = item["kind"]
        if kind not in profile["section_energy"]:
            raise GenerationError(f"Hardstyle does not support section {kind}")
        counts[kind] += 1
        base = profile["section_energy"][kind] * (0.5 + 0.5 * config.energy)
        if kind == "drop" and counts[kind] > 1:
            base = min(1.0, base + 0.05 * config.variation)
        energy = float(item.get("energy", base))
        result.append(Section(kind, item.get("name", f"{kind.title()} {counts[kind]}"), start, item["bars"], energy))
        start += item["bars"]
    return result


def _make_motif(config: Config, profile: dict, style: dict, progression: list[int]) -> list[list[dict]]:
    """Make one scale-degree hook; do not reroll it independently for each drop."""
    result, previous = [], 4
    chord_bars, phrase_bars = profile["chord_bars"], profile["phrase_bars"]
    for bar in range(phrase_bars):
        rng = stable_rng(config.seed, profile["id"], config.style, "lead-motif", bar)
        degree = progression[(bar // chord_bars) % len(progression)]
        rhythm_index = (bar % 4 + rng.randrange(2)) % len(style["lead_rhythms"])
        rhythm = style["lead_rhythms"][rhythm_index]
        if config.complexity < 0.25:
            rhythm = [[0, 1.75], [2, 1.75]]
        elif config.complexity < 0.5:
            rhythm = [item for index, item in enumerate(rhythm) if index % 2 == 0 or item[0] >= 3]
        notes = []
        for index, (beat, length) in enumerate(rhythm):
            strong = index == 0 or beat == 2 or index == len(rhythm) - 1
            if bar == 0 and index == 0:
                step = degree
            elif bar == phrase_bars - 1 and index == len(rhythm) - 1:
                step = degree + (7 if degree <= 3 and previous > 4 else 0)
            elif strong:
                step = _chord_tone(degree, previous, rng)
            else:
                step = max(0, min(11, previous + rng.choice((-2, -1, 1, 1, 2))))
            notes.append({"beat": float(beat), "duration": float(length) * style["lead_gate"],
                          "degree": step, "accent": 1.0 if strong else 0.86})
            previous = step
        result.append(notes)
    return result


def compose(config: Config, profile: dict) -> Song:
    """Compose a complete song using only supplied configuration and profile data."""
    config.validate(profile)
    if config.style not in profile["styles"]:
        raise GenerationError(f"Unknown hardstyle style: {config.style}")
    if config.scale not in profile["scales"]:
        raise GenerationError(f"This profile does not support {config.scale}")
    if not profile["bpm"]["min"] <= config.bpm <= profile["bpm"]["max"]:
        raise GenerationError(f"Hardstyle tempo must be between {profile['bpm']['min']} and {profile['bpm']['max']} BPM")
    style = profile["styles"][config.style]
    key, scale = ROOTS[config.key], SCALES[config.scale]
    sections = _sections(config, profile)
    tracks = [Track(*descriptor) for descriptor in _TRACKS]
    by_id = {track.id: track for track in tracks}
    if config.kick_mode == "fixed":
        by_id["kick"].name = "Kick · fixed sampler note 36"
    if style["bass_mode"] == "reverse-bass":
        by_id["bass"].name = "Bass · reverse-bass offbeats"
    progression = list(stable_rng(config.seed, profile["id"], config.style, "progression").choice(style["progressions"]))
    motif = _make_motif(config, profile, style, progression)
    phrase_bars, chord_bars = profile["phrase_bars"], profile["chord_bars"]
    phrase_voicings, previous = [], None
    for local_bar in range(phrase_bars):
        degree = progression[(local_bar // chord_bars) % len(progression)]
        previous = _voicing(key, scale, degree, previous)
        phrase_voicings.append(previous)

    chords, drop_number = [], 0
    humanize_ticks = config.humanize_ms * config.bpm / 60000 * PPQ

    def add(track_id: str, bar: int, beat: float, duration: float, pitch: int,
            velocity: float, section: Section, *, human: bool = False, salt: object = 0) -> None:
        start = bar * BAR + round(beat * PPQ)
        limit = section.end_bar * BAR - (PPQ if section.kind == "build" else 0)
        if human and humanize_ticks:
            jitter = stable_rng(config.seed, profile["id"], "timing", track_id, bar, beat, salt)
            start += round(jitter.uniform(-humanize_ticks, humanize_ticks))
            start = max(bar * BAR, start)
        end = min(limit, start + max(1, round(duration * PPQ)))
        if section.start_bar * BAR <= start < end:
            by_id[track_id].notes.append(Note(start, end - start, max(0, min(127, int(pitch))),
                                             max(1, min(127, round(velocity)))))

    for section_index, section in enumerate(sections):
        if section.kind == "drop":
            drop_number += 1
        for local_bar in range(section.bars):
            bar = section.start_bar + local_bar
            phrase_bar = local_bar % phrase_bars
            degree = progression[(phrase_bar // chord_bars) % len(progression)]
            voicing = phrase_voicings[phrase_bar]
            root = 24 + (key + scale[degree]) % 12
            chords.append(Chord(bar, root, voicing, _chord_name(key, scale, degree)))
            progress = (local_bar + 0.5) / section.bars
            phrase_end = phrase_bar == phrase_bars - 1
            last_bar = local_bar == section.bars - 1
            energy = section.energy
            drum_velocity = 0.66 + 0.34 * energy

            # Kick tails are rhythmically separated before placing bass support.
            kick_beats = []
            if section.kind == "drop":
                kick_beats = [0.0, 1.0, 2.0, 3.0]
                fill_rng = stable_rng(config.seed, profile["id"], config.style, "kick-fill", section_index, local_bar)
                if phrase_end and config.complexity >= 0.3 and fill_rng.random() < style["fill_probability"]:
                    if config.style == "raw":
                        kick_beats = [0, 1, 2, 2.5, 2.75, 3.5] if config.complexity > 0.65 else [0, 1, 2, 2.5, 3.5]
                    elif config.complexity >= 0.5:
                        kick_beats = [0, 1, 2, 3, 3.5]
                if config.style == "raw" and phrase_end and config.variation > 0.7:
                    kick_beats = [beat for beat in kick_beats if beat < 3]
            elif section.kind == "intro":
                kick_beats = [0, 2] if progress < 0.5 else [0, 1, 2, 3]
            elif section.kind == "outro":
                kick_beats = [0, 1, 2, 3] if progress < 0.5 else [0, 2]
            elif section.kind == "build" and progress < 0.5:
                kick_beats = [0, 2]
            kick_regions = []
            for index, beat in enumerate(sorted(set(kick_beats))):
                following = kick_beats[index + 1] if index + 1 < len(kick_beats) else 4
                length = min(style["kick_gate"], max(0.03, following - beat - 0.02))
                velocity = style["kick_velocity"] * drum_velocity * (0.92 if beat % 1 else 1)
                add("kick", bar, beat, length, root if config.kick_mode == "pitched" else 36,
                    velocity, section)
                kick_regions.append((float(beat), float(beat + length)))

            bass_active = section.kind == "drop" or section.kind in {"intro", "outro"} and (progress > 0.25 if section.kind == "intro" else progress < 0.75)
            if bass_active:
                beats = [beat + style["bass_offset"] for beat in range(4)]
                if style["bass_mode"] == "sparse-support":
                    beats = beats[::2] if local_bar % 2 == 0 else beats[1::2]
                for beat in beats:
                    end = min(4.0, beat + style["bass_gate"])
                    # Respect pitched tails as well as attacks, including roll retriggers.
                    for kick_start, kick_end in kick_regions:
                        if kick_start <= beat < kick_end + 0.02:
                            beat = kick_end + 0.02
                        elif beat < kick_start < end:
                            end = kick_start - 0.02
                    if end - beat >= 0.08:
                        add("bass", bar, beat, end - beat, root, style["bass_velocity"] * (0.7 + 0.3 * energy), section)

            # The hook's first eight bars are identical at each drop. Later
            # answer phrases can move selected passing notes by a scale step.
            lead_active = section.kind == "drop" or section.kind == "build" and progress > 0.25
            if lead_active:
                for note_index, note in enumerate(motif[phrase_bar]):
                    step = note["degree"]
                    variant = stable_rng(config.seed, profile["id"], config.style, "lead-answer", section_index, local_bar, note_index)
                    if section.kind == "drop" and local_bar >= phrase_bars and phrase_bar % 4 == 3 and note["accent"] < 1 and variant.random() < config.variation:
                        step = max(0, min(11, step + variant.choice((-1, 1))))
                    if section.kind == "build" and progress < 0.65 and note["beat"] > 2:
                        continue
                    pitch = _scale_note(style["lead_octave"], key, scale, step)
                    add("lead", bar, note["beat"], note["duration"], pitch,
                        style["lead_velocity"] * (0.65 + 0.35 * energy) * note["accent"], section)
            elif section.kind == "breakdown":
                # A sparse, slower statement of the very same melodic contour.
                selected = [motif[phrase_bar][0], motif[phrase_bar][-1]] if local_bar % 2 == 0 else [motif[phrase_bar][0]]
                for index, note in enumerate(selected):
                    beat = index * 2
                    add("lead", bar, beat, 1.8 if len(selected) == 2 else 3.5,
                        _scale_note(style["lead_octave"], key, scale, note["degree"]), 66 + 15 * energy, section)
            elif section.kind == "intro" and progress > 0.5 and local_bar % 2 == 0:
                note = motif[phrase_bar][0]
                add("lead", bar, 2, 0.85, _scale_note(style["lead_octave"], key, scale, note["degree"]), 64, section)

            if section.kind in {"drop", "build"}:
                attacks = [0, 2] if config.style != "raw" else [0, 2.75]
                if section.kind == "build" and progress < 0.4:
                    attacks = [0]
                for attack in attacks:
                    for pitch in voicing:
                        add("chords", bar, attack, style["chord_gate"] * (2 if config.style == "euphoric" else 1),
                            pitch, 62 + 29 * energy, section)
            elif local_bar % chord_bars == 0:
                for pitch in voicing:
                    add("chords", bar, 0, min(chord_bars * 4 - 0.12, (section.bars - local_bar) * 4), pitch,
                        48 + 20 * energy, section)

            if local_bar % chord_bars == 0:
                # Pads thin out during raw drops; harmony still remains legible.
                pad_enabled = section.kind != "drop" or config.style != "raw" or local_bar % phrase_bars == 0
                if pad_enabled:
                    duration = min(chord_bars * 4 - 0.15, (section.bars - local_bar) * 4)
                    for pitch in voicing:
                        add("pad", bar, 0, duration, pitch + 12, 45 + 24 * energy, section)

            arp_rng = stable_rng(config.seed, profile["id"], config.style, "arp", section_index, local_bar)
            arp_active = section.kind in {"drop", "build"} and arp_rng.random() < style["arp_density"] * (0.35 + 0.65 * config.complexity)
            # One intentional turn-around guarantees a usable texture part in
            # short drop sketches; the remaining phrases retain their space.
            if section.kind == "drop" and local_bar == min(phrase_bars - 2, section.bars - 1):
                arp_active = True
            if arp_active:
                step = 0.25 if config.complexity > 0.8 and phrase_end else 0.5
                pitches = [voicing[0] + 12, voicing[1] + 12, voicing[2] + 12, voicing[1] + 12]
                for index in range(1, round(4 / step)):
                    add("arp", bar, index * step, step * 0.68, pitches[(index + local_bar) % 4], 57 + 23 * energy, section)

            screech_rng = stable_rng(config.seed, profile["id"], config.style, "screech", section_index, local_bar)
            screech_active = screech_rng.random() < style["screech_density"] * (0.65 + 0.35 * config.complexity)
            if section.kind == "drop" and (screech_active or local_bar == min(phrase_bars - 1, section.bars - 1)):
                attacks = [1.0, 2.75] if config.style == "raw" else [3.25]
                for index, beat in enumerate(attacks):
                    pitch = _scale_note(60, key, scale, degree + (4 if index else 7))
                    add("screech", bar, beat, 0.32 if config.style == "raw" else 0.48, pitch, 89 + 23 * energy, section)

            # Percussion keeps distinct musical functions on separate lanes.
            if section.kind == "build":
                roll_step = 1.0 if progress < 0.35 else 0.5 if progress < 0.75 else 0.25
                if last_bar and config.complexity > 0.75:
                    roll_step = 0.125
                for index in range(math.ceil(4 / roll_step)):
                    beat = index * roll_step
                    if last_bar and beat >= 3:
                        continue
                    accent = 1.0 if index % max(1, round(1 / roll_step)) == 0 else 0.86
                    add("snare", bar, beat, min(0.1, roll_step * 0.7), 38,
                        (56 + 63 * progress) * accent, section)
            elif section.kind == "drop":
                for beat in (1, 3):
                    add("snare", bar, beat, 0.11, 38, 74 * drum_velocity, section)
                if phrase_end and config.complexity > 0.55:
                    for beat in (3.25, 3.5, 3.75):
                        add("snare", bar, beat, 0.08, 38, 73 + 12 * (beat - 3), section)

            percussion_active = section.kind != "breakdown"
            if percussion_active:
                clap_enabled = section.kind == "drop" or section.kind in {"intro", "outro"} and progress < 0.8 or section.kind == "build" and progress < 0.5
                if clap_enabled:
                    for beat in (1, 3):
                        add("clap", bar, beat, 0.12, 39, 91 * drum_velocity, section, human=True)
                open_enabled = section.kind == "drop" or section.kind == "intro" and progress > 0.5 or section.kind == "outro" and progress < 0.5
                open_beats = (0.5, 1.5, 2.5, 3.5) if open_enabled else ()
                for beat in open_beats:
                    add("open_hat", bar, beat, 0.27, 46, 77 * drum_velocity, section, human=True)
                hat_step = 0.25 if config.complexity > 0.75 and phrase_end else style["hat_step"]
                if section.kind == "intro" and progress < 0.25 or section.kind == "outro" and progress > 0.75:
                    hat_step = 1.0
                for index in range(round(4 / hat_step)):
                    beat = index * hat_step
                    if beat in open_beats:
                        continue
                    velocity_rng = stable_rng(config.seed, profile["id"], "hat-velocity", section_index, local_bar, index)
                    velocity = (65 if beat % 1 == 0 else 48) * drum_velocity + velocity_rng.uniform(-6, 6)
                    add("closed_hat", bar, beat, min(0.12, hat_step * 0.7), 42, velocity, section, human=True)

            if local_bar == 0 and section.kind in {"intro", "drop", "breakdown"}:
                add("cymbal", bar, 0, 1.0, 49, 69 + 37 * energy, section)
            elif section.kind == "drop" and phrase_bar == 0:
                add("cymbal", bar, 0, 0.8, 49, 89, section)
            if section.kind == "drop" and (drop_number > 1 or config.complexity > 0.8) and progress > 0.5:
                for beat in (0.5, 1.5, 2.5, 3.5):
                    add("cymbal", bar, beat, 0.18, 51, 58 + 22 * energy, section, human=True, salt="ride")
            if section.kind == "build" and local_bar == max(0, section.bars - 2):
                add("fx", bar, 0, min(8, (section.bars - local_bar) * 4 - 1),
                    _scale_note(72, key, scale, 0), 82 + 26 * energy, section)
            elif section.kind == "drop" and local_bar == 0:
                add("fx", bar, 0, 0.25, _scale_note(48, key, scale, 0), 117, section)
            elif section.kind in {"intro", "outro", "breakdown"} and local_bar == 0:
                add("fx", bar, 0, 0.5, _scale_note(60, key, scale, 0), 62, section)

    song_ticks = sections[-1].end_bar * BAR
    for track in tracks:
        _clean(track, song_ticks)
    role_notes = {
        "kick": "Pitched kick follows chord roots; fixed mode sends sampler note 36. Use a hardstyle kick instrument or tuned sampler.",
        "bass": "Offbeat support leaves gaps around kick attacks and tails; classic style requests a reverse-bass instrument.",
        "lead": "Reusable eight-bar hook in the selected scale, with later phrase answers.",
        "chords": "Voice-led triads; the chord chart provides the harmonic plan.",
        "pad": "Held harmony and space behind the lead.",
        "arp": "Chord-tone rhythmic detail; deliberate rests depend on complexity and style.",
        "screech": "Short synth response notes, more frequent in raw style.",
        "fx": "Riser/impact note cues requiring an FX instrument or sampler; these notes do not synthesize audio.",
        "snare": "GM note 38; build rolls accelerate and leave the final beat silent.",
        "clap": "GM note 39.", "closed_hat": "GM note 42.", "open_hat": "GM note 46.",
        "cymbal": "GM notes 49 (crash) and 51 (ride).",
    }
    return Song(copy.deepcopy(config), sections, tracks, chords, str(profile["version"]),
                ["MIDI contains notes and automation, not hardstyle audio. Assign suitable instruments or samples to each lane.",
                 "General MIDI program numbers are audition hints; a GM player will not reproduce a finished hardstyle sound."],
                {"composer": "hardstyle-phrase-engine", "style": config.style,
                 "progression_degrees": progression, "progression_degree_numbering": "zero-based diatonic",
                 "chord_bars": chord_bars, "phrase_bars": phrase_bars,
                 "lead_motif": motif, "role_notes": role_notes,
                 "predrop_gap_beats": 1, "bass_mode": style["bass_mode"],
                 "source_material": "Algorithmic scale-degree motifs; no song transcription or copied melody."})
