"""House and trance grooves with a shared breakdown/drop melodic identity."""
from ._phrase import PhraseComposer
from ..model import Config, Song, GenerationError


def compose(config: Config, profile: dict) -> Song:
    config.validate(profile)
    if config.style not in profile['styles']:
        raise GenerationError('Unknown EDM style: ' + config.style)
    style = profile['styles'][config.style]
    tech = config.style == 'tech-house'
    roles = ['kick', 'snare', 'clap', 'closed_hat', 'open_hat', style['bass_role'],
             'stab' if tech else 'chords', 'pad', 'lead', 'arp', 'fx', 'riser']
    if tech:
        roles.append('vocal')
    p = PhraseComposer(config, profile, roles)
    if config.style == 'deep-house':
        p.tracks['lead'].program = 4
    for section in p.sections:
        peak, build = section.kind == 'drop', section.kind == 'build'
        breakdown = section.kind == 'breakdown'
        for bar in range(section.bars):
            root, voicing = p.harmony(section, bar)
            kicks = p.drums(section, bar, active=not breakdown, peak=peak, build=build)
            if not breakdown and not build:
                beats = None
                if config.style == 'psy-trance' and bar % 4 == 3 and config.complexity > .5:
                    beats = [b + t for b in range(4) for t in (1/3, 2/3, 5/6)]
                p.bass(section, bar, root, kicks, beats)
            p.pads(section, bar, voicing)
            if peak or build:
                p.chords_at(section, bar, voicing, role='stab' if tech else 'chords')
            if peak or breakdown or build:
                p.lead(section, bar, sparse=build and bar < section.bars // 2)
            if peak or breakdown and config.style == 'uplifting-trance':
                p.arp(section, bar, voicing, force=bar == 0 or config.style == 'uplifting-trance')
            if tech and peak and bar % 2 == 0:
                p.add('vocal', section, bar, 3.5, .25, 60, 83)
            p.fx(section, bar, peak, build)
    return p.finish()
