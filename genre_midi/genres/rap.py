"""Hip-hop drums, short melodic loops and kick-separated legato 808 cues."""
from ._phrase import PhraseComposer
from ..model import Config, Song, GenerationError


def compose(config: Config, profile: dict) -> Song:
    config.validate(profile)
    if config.style not in profile['styles']:
        raise GenerationError('Unknown rap style: ' + config.style)
    style = profile['styles'][config.style]
    p = PhraseComposer(config, profile, ['kick', 'snare', 'clap', 'closed_hat', 'open_hat',
                                       style['bass_role'], 'chords', 'pad', 'lead', 'fx'])
    p.tracks['lead'].program = 10 if config.style in ('trap', 'drill') else 4
    if config.style == 'boom-bap':
        p.tracks['bass'].program = 32
    for section in p.sections:
        peak = section.kind == 'hook'
        for bar in range(section.bars):
            root, voicing = p.harmony(section, bar)
            active = section.kind not in ('bridge',) and (section.kind not in ('intro', 'outro') or bar % 2 == 0)
            kicks = p.drums(section, bar, active=active, peak=peak)
            if active:
                p.bass(section, bar, root, kicks)
            p.pads(section, bar, voicing)
            if bar % p.chord_bars == 0 or peak and config.style == 'melodic-trap':
                p.chords_at(section, bar, voicing, beats=[0] if config.style != 'melodic-trap' else None)
            p.lead(section, bar, sparse=section.kind in ('intro', 'outro', 'bridge'))
            if active and config.style != 'boom-bap' and bar % 4 == 3 and config.complexity >= .25:
                # Alternating triplet and 32nd bursts end the four-bar drum phrase.
                step = 1/6 if bar % 8 == 3 else .125
                for i in range(round(1 / step)):
                    p.add('closed_hat', section, bar, 3 + i * step, step * .65, 42, 48 + i * 3)
            p.fx(section, bar, peak)
            if config.style == 'boom-bap' and bar % 4 == 3:
                p.add('fx', section, bar, 3.75, .04, 72, 30)
    return p.finish()
