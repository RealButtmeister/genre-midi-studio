"""Pure shared harmony and note ownership helpers."""
from collections import defaultdict
from ..model import Note, Track

_PITCH_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
def _scale_note(base: int, key: int, scale: tuple[int, ...], degree: int) -> int:
    octave, step = divmod(degree, len(scale))
    return base + key + octave * 12 + scale[step]


def _chord_tone(degree: int, previous: int, rng, *, ceiling: int = 11) -> int:
    candidates = sorted({degree + interval + octave for interval in (0, 2, 4)
                         for octave in (-7, 0, 7) if 0 <= degree + interval + octave <= ceiling})
    distance = min(abs(value - previous) for value in candidates)
    close = [value for value in candidates if abs(value - previous) <= distance + 1]
    return rng.choice(close)


def _voicing(key: int, scale: tuple[int, ...], degree: int, previous: tuple[int, ...] | None) -> tuple[int, ...]:
    triad = [_scale_note(48, key, scale, degree + interval) for interval in (0, 2, 4)]
    candidates = []
    for inversion in range(3):
        shape = triad[inversion:] + [pitch + 12 for pitch in triad[:inversion]]
        for shift in (-24, -12, 0, 12):
            candidate = tuple(pitch + shift for pitch in shape)
            if 48 <= candidate[0] and candidate[-1] <= 76:
                movement = sum(abs(a - b) for a, b in zip(candidate, previous)) if previous else 0
                center = abs(sum(candidate) / 3 - 62)
                candidates.append((movement + center * 0.3, candidate))
    return min(candidates)[1]


def _chord_name(key: int, scale: tuple[int, ...], degree: int) -> str:
    pitches = [_scale_note(0, key, scale, degree + interval) for interval in (0, 2, 4)]
    third, fifth = pitches[1] - pitches[0], pitches[2] - pitches[0]
    quality = "dim" if fifth == 6 else "aug" if fifth == 8 else "m" if third == 3 else ""
    return _PITCH_NAMES[pitches[0] % 12] + quality


def _clean(track: Track, song_ticks: int) -> None:
    """Resolve note ownership before SMF serialization, including exact retriggers."""
    monophonic = track.role in {"kick", "bass", "sub", "lead", "screech", "vocal", "pluck", "riser"}
    groups = defaultdict(list)
    for note in track.notes:
        if 0 <= note.start < song_ticks and note.duration > 0:
            groups[0 if monophonic else note.pitch].append(note)
    cleaned = []
    for notes in groups.values():
        # A coincident duplicate is one attack; retain the stronger candidate.
        starts = {}
        for note in notes:
            prior = starts.get(note.start)
            if prior is None or note.velocity > prior.velocity:
                starts[note.start] = note
        ordered = sorted(starts.values(), key=lambda note: (note.start, note.pitch))
        for index, note in enumerate(ordered):
            end = min(song_ticks, note.end, ordered[index + 1].start if index + 1 < len(ordered) else song_ticks)
            if end > note.start:
                cleaned.append(Note(note.start, end - note.start, note.pitch, note.velocity))
    track.notes = sorted(cleaned, key=lambda note: (note.start, note.pitch))


