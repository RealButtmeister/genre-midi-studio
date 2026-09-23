import pytest
from Tests.genre_contracts import check_contract, check_direct_engine, check_bass_gap, generate, track, excerpt

STYLES = ['progressive-house', 'tech-house', 'deep-house', 'uplifting-trance', 'psy-trance']

@pytest.mark.parametrize('style', STYLES)
@pytest.mark.parametrize('arrangement', ['full', 'compact', 'drop'])
def test_edm_contract(style, arrangement):
    check_contract('edm', style, arrangement)

@pytest.mark.parametrize('style', STYLES)
def test_edm_validation_scales_and_kick_gap(style):
    check_direct_engine('edm', style)
    check_bass_gap(generate('edm', style, humanize_ms=15, complexity=1))

def test_trance_shared_theme_house_identity_and_142_bpm():
    trance = generate('edm', 'uplifting-trance', arrangement='full', variation=1)
    breakdown = next(s for s in trance.sections if s.kind == 'breakdown')
    drop = next(s for s in trance.sections if s.kind == 'drop')
    assert breakdown.bars >= 16
    assert excerpt(trance, 'lead', breakdown, 16) == excerpt(trance, 'lead', drop, 16)
    assert not excerpt(trance, 'kick', breakdown)
    assert trance.sections[0].bars >= 16 and trance.sections[-1].bars >= 16
    tech = generate('edm', 'tech-house', arrangement='full')
    assert track(tech, 'vocal').notes and track(tech, 'stab').notes
    assert next(s.bars for s in tech.sections if s.kind == 'breakdown') == 8
    deep = generate('edm', 'deep-house', arrangement='full')
    assert len(deep.chords[0].pitches) == 5
    assert max(s.energy for s in deep.sections) - min(s.energy for s in deep.sections) < .3
    psy = generate('edm', 'psy-trance', bpm=142, scale='phrygian')
    house = generate('edm', 'progressive-house')
    assert len(track(psy, 'bass').notes) > len(track(house, 'bass').notes)
