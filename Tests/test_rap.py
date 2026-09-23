import pytest
from genre_midi.model import BAR, PPQ, GenerationError
from Tests.genre_contracts import check_contract, check_direct_engine, check_bass_gap, generate, track

STYLES = ['trap', 'drill', 'boom-bap', 'melodic-trap']

@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('arrangement', ['full', 'compact', 'hook'])
def test_rap_contract(style, arrangement):
    check_contract('rap', style, arrangement)

@pytest.mark.parametrize('style', STYLES)
def test_rap_validation_scales_and_kick_gap(style):
    check_direct_engine('rap', style)
    check_bass_gap(generate('rap', style, humanize_ms=15, complexity=1))

def test_half_time_rolls_slides_and_boom_bap_tempo():
    trap = generate('rap', 'trap', humanize_ms=0, complexity=1)
    assert {n.start % BAR for n in track(trap, 'snare').notes} == {2 * PPQ}
    hats = track(trap, 'closed_hat').notes
    assert any(b.start - a.start == PPQ // 6 for a, b in zip(hats, hats[1:]))
    # A full eight-bar hook includes both triplet and 32nd turnarounds.
    longer = generate('rap', 'trap', sections=[{'kind':'hook','bars':8}], humanize_ms=0, complexity=1)
    hats = track(longer, 'closed_hat').notes
    assert any(b.start - a.start == PPQ // 8 for a, b in zip(hats, hats[1:]))
    sub = track(trap, 'sub').notes
    assert any(a.end == b.start and b.pitch - a.pitch == 12 for a, b in zip(sub, sub[1:]))
    boom = generate('rap', 'boom-bap', bpm=90, humanize_ms=0)
    assert 'sub' not in {t.role for t in boom.tracks}
    assert len(boom.chords[0].pitches) == 4
    assert {n.start % BAR for n in track(boom, 'snare').notes} == {PPQ, 3 * PPQ}
    with pytest.raises(GenerationError):
        generate('rap', 'boom-bap', bpm=140)
    with pytest.raises(GenerationError):
        generate('rap', 'trap', bpm=90)
