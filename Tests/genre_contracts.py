"""Shared black-box checks for installed genre engines and exported MIDI."""
from copy import deepcopy
from dataclasses import asdict
import importlib
from unittest.mock import patch

from genre_midi.model import BAR, ROOTS, SCALES, Config, GenerationError
from genre_midi.midi import parse_midi, song_midi
from genre_midi.registry import compose_song, get_profile
from genre_midi.service import validate_song


def generate(genre, style, **changes):
    profile = get_profile(genre)
    values = dict(genre=genre, style=style, bpm=profile['styles'][style].get('bpm', profile['bpm'])['default'],
                  scale=profile['scales'][0], arrangement='drop' if genre == 'edm' else 'hook')
    values.update(changes)
    return compose_song(Config(**values))


def track(song, role):
    return next(t for t in song.tracks if t.role == role)


def excerpt(song, role, section, bars=None):
    start, end = section.start_bar * BAR, (section.start_bar + (bars or section.bars)) * BAR
    return [(n.start-start, n.duration, n.pitch) for n in track(song, role).notes if start <= n.start < end]


def check_contract(genre, style, arrangement):
    first = generate(genre, style, arrangement=arrangement)
    second = generate(genre, style, arrangement=arrangement)
    assert asdict(first) == asdict(second)
    assert song_midi(first) == song_midi(second)
    assert validate_song(first)['notes'] > 0
    assert len(first.chords) == first.bars
    peak = next(s for s in first.sections if s.kind in ('chorus', 'hook', 'drop'))
    mono = {'kick', 'bass', 'sub', 'lead', 'pluck', 'vocal', 'arp', 'riser'}
    for t in first.tracks:
        assert excerpt(first, t.role, peak), (style, t.role)
        previous = {}
        for n in t.notes:
            assert 0 <= n.start < n.end <= first.ticks
            section = next(s for s in first.sections if s.start_bar * BAR <= n.start < s.end_bar * BAR)
            assert n.end <= section.end_bar * BAR
            group = 0 if t.role in mono else n.pitch
            assert n.start >= previous.get(group, 0), (style, t.role, n)
            previous[group] = n.end
            assert 0 <= n.pitch <= 127 and 1 <= n.velocity <= 127
        assert t.role in first.metadata['role_notes']
    for data in (song_midi(first), song_midi(first, gm_preview=True), song_midi(first, [first.tracks[0]])):
        doc = parse_midi(data)
        assert doc.ppq == 960
        assert not any(e.meta_type == 0x51 for events in doc.tracks for e in events)
        assert max(e.tick for events in doc.tracks for e in events) == first.ticks


def check_direct_engine(genre, style):
    profile = get_profile(genre)
    config = generate(genre, style).config
    before = deepcopy(profile), asdict(config)
    engine = importlib.import_module('genre_midi.genres.' + genre)
    # Engines operate exclusively on supplied data, including validation.
    with patch('builtins.open', side_effect=AssertionError('engine performed file I/O')), patch('pathlib.Path.open', side_effect=AssertionError('engine performed file I/O')):
        engine.compose(config, profile)
    assert (profile, asdict(config)) == before
    for changes in ({'style':'missing'}, {'scale':'missing'}, {'bpm':401}, {'arrangement':'missing'},
                    {'sections':[{'kind':'drop2', 'bars':1}]}):
        invalid = deepcopy(config)
        for key, value in changes.items():
            setattr(invalid, key, value)
        try:
            engine.compose(invalid, profile)
        except GenerationError:
            pass
        else:
            raise AssertionError(changes)
    for scale in profile['scales']:
        for key, amount in [('C', 0), ('F#', .5), ('Bb', 1)]:
            song = generate(genre, style, scale=scale, key=key, energy=amount, complexity=amount,
                            variation=amount, humanize_ms=15, sections=[{'kind':s, 'bars':1} for s in profile['section_energy']])
            validate_song(song)
            allowed = {(ROOTS[key] + v) % 12 for v in SCALES[scale]}
            for t in song.tracks:
                if t.channel != 10 and t.role != 'vocal':
                    assert all(n.pitch % 12 in allowed for n in t.notes), (style, t.role)


def check_bass_gap(song):
    kicks = track(song, 'kick').notes
    bass = next(t for t in song.tracks if t.role in ('bass', 'sub'))
    for n in bass.notes:
        assert not any(n.start < k.end and k.start < n.end for k in kicks), (song.config.style, n)
