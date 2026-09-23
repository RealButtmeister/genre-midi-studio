"""Pop arrangement: shared topline, chorus peaks and stripped bridges."""
from ._phrase import PhraseComposer
from ..model import BAR, Config, Song, GenerationError


def compose(config: Config, profile: dict) -> Song:
    config.validate(profile)
    if config.style not in profile['styles']:
        raise GenerationError('Unknown pop style: ' + config.style)
    style = profile['styles'][config.style]
    p = PhraseComposer(config, profile, ['kick', 'snare', 'clap', 'closed_hat', 'open_hat',
                                       style['bass_role'], 'chords', 'pad', 'lead', 'pluck', 'vocal', 'fx', 'riser'])
    if config.style == 'acoustic-pop':
        p.tracks['chords'].program = 24
        p.tracks['lead'].program = 24
        p.tracks['bass'].program = 32
    for section in p.sections:
        peak, build = section.kind == 'chorus', section.kind == 'prechorus'
        bridge = section.kind == 'bridge'
        for bar in range(section.bars):
            root, voicing = p.harmony(section, bar)
            p.pads(section, bar, voicing)
            active = not bridge and (not build or bar >= section.bars // 2)
            kicks = p.drums(section, bar, active=active, peak=peak, build=build)
            if active:
                p.bass(section, bar, root, kicks)
                p.chords_at(section, bar, voicing, strum=config.style == 'acoustic-pop')
            p.lead(section, bar, sparse=section.kind in ('intro', 'outro', 'verse'), vocal=True)
            if bridge:
                # Bridge strips to pad and vocal rather than continuing the hook stack.
                start = (section.start_bar + bar) * BAR
                p.tracks['lead'].notes = [n for n in p.tracks['lead'].notes if n.start < start]
            if peak:
                p.arp(section, bar, voicing, role='pluck', force=bar == 0)
            p.fx(section, bar, peak, build)
    return p.finish()
