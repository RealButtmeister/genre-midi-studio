import pytest
from genre_midi.model import BAR
from Tests.genre_contracts import check_contract, check_direct_engine, check_bass_gap, generate, track, excerpt

STYLES = ['dance-pop', 'synth-pop', 'acoustic-pop', 'rnb-pop']

@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('arrangement', ['full', 'compact', 'hook'])
def test_pop_contract(style, arrangement):
    check_contract('pop', style, arrangement)

@pytest.mark.parametrize('style', STYLES)
def test_pop_validation_and_scale_boundaries(style):
    check_direct_engine('pop', style)
    check_bass_gap(generate('pop', style, humanize_ms=15))

def test_pop_style_identity_and_chorus_energy():
    dance = generate('pop', 'dance-pop', humanize_ms=0)
    acoustic = generate('pop', 'acoustic-pop', humanize_ms=0)
    rnb = generate('pop', 'rnb-pop', humanize_ms=0)
    assert len(track(dance, 'kick').notes) == 2 * len(track(acoustic, 'kick').notes)
    assert 'sub' not in {t.role for t in acoustic.tracks}
    assert len(rnb.chords[0].pitches) == 5
    assert len(track(rnb, 'closed_hat').notes) > len(track(dance, 'closed_hat').notes)
    assert all(n.start % BAR == 1920 for n in track(rnb, 'snare').notes)
    full = generate('pop', 'synth-pop', arrangement='full')
    chorus = next(s for s in full.sections if s.kind == 'chorus')
    assert chorus.energy == max(s.energy for s in full.sections)
    bridge = next(s for s in full.sections if s.kind == 'bridge')
    assert not excerpt(full, 'kick', bridge)
    assert not excerpt(full, 'lead', bridge)
    assert excerpt(full, 'vocal', bridge) and excerpt(full, 'pad', bridge)
    pre = next(s for s in full.sections if s.kind == 'prechorus')
    assert not excerpt(full, 'kick', pre, pre.bars // 2)
