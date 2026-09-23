"""Pure phrase-composition support for pop, hip-hop and dance engines."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from ..model import BAR, PPQ, ROOTS, SCALES, Chord, Config, GenerationError, Note, Section, Song, Track, stable_rng
from ._common import _scale_note, _chord_name, _voicing, _clean, _chord_tone

DRUMS = {'kick': 36, 'snare': 38, 'clap': 39, 'closed_hat': 42, 'open_hat': 46}
PROGRAMS = {'bass': 38, 'sub': 38, 'lead': 81, 'chords': 4, 'pad': 89,
            'arp': 81, 'pluck': 10, 'stab': 4, 'vocal': 53, 'fx': 97, 'riser': 97}


class PhraseComposer:
    def __init__(self, config: Config, profile: dict, roles: list[str]):
        config.validate(profile)
        if config.style not in profile['styles']:
            raise GenerationError(f"Unknown {profile['id']} style: {config.style}")
        if config.scale not in profile['scales']:
            raise GenerationError(f"This profile does not support {config.scale}")
        style = profile['styles'][config.style]
        limits = style.get('bpm', profile['bpm'])
        if not (profile['bpm']['min'] <= config.bpm <= profile['bpm']['max']
                and limits['min'] <= config.bpm <= limits['max']):
            raise GenerationError(f"{config.style} tempo must be {limits['min']}–{limits['max']} BPM")
        self.config, self.profile, self.style = config, profile, style
        self.key, self.scale = ROOTS[config.key], SCALES[config.scale]
        self.phrase_bars = style.get('phrase_bars', profile['phrase_bars'])
        self.chord_bars = style.get('chord_bars', profile['chord_bars'])
        self.progression = list(self.rng('progression').choice(style['progressions']))
        self.sections = []
        counts = defaultdict(int)
        requested = config.sections if config.sections is not None else profile['arrangements'][config.arrangement]
        start = 0
        for item in requested:
            kind = item['kind']
            if kind not in profile['section_energy']:
                raise GenerationError(f"{profile['name']} does not support section {kind}")
            bars = item['bars']
            if config.sections is None and kind == 'breakdown':
                bars = style.get('breakdown_bars', bars)
            energy = profile['section_energy'][kind]
            energy = .55 + (energy - .55) * style.get('energy_spread', 1)
            counts[kind] += 1
            self.sections.append(Section(kind, item.get('name', f'{kind.title()} {counts[kind]}'), start, bars,
                                         float(item.get('energy', energy * (.5 + .5 * config.energy)))))
            start += bars
        self.tracks = {}
        channel = 1
        for role in roles:
            name = role.replace('_', ' ').title()
            if role == 'sub' and profile['id'] == 'rap':
                name = 'Sub · legato 808 cues'
            self.tracks[role] = Track(role, name, role, 10 if role in DRUMS else channel, PROGRAMS.get(role))
            if role not in DRUMS:
                channel += 1
                if channel == 10:
                    channel += 1
        self.chords = []
        self.motif = self.make_motif()
        self.voicings = []
        previous = None
        for bar in range(self.phrase_bars):
            degree = self.degree(bar)
            previous = _voicing(self.key, self.scale, degree, previous)
            pitches = list(previous)
            for interval in range(6, style['extensions'] * 2, 2):
                pitch = self.pitch(48, degree + interval)
                while pitch <= max(pitches):
                    pitch += 12
                pitches.append(pitch)
            self.voicings.append(tuple(pitches))

    def rng(self, *parts):
        return stable_rng(self.config.seed, self.profile['id'], self.config.style, *parts)

    def degree(self, bar):
        return self.progression[(bar % self.phrase_bars // self.chord_bars) % len(self.progression)]

    def pitch(self, base, degree):
        return _scale_note(base, self.key, self.scale, degree)

    def make_motif(self):
        motif, previous = [], 4
        for bar in range(self.phrase_bars):
            rng, degree = self.rng('motif', bar), self.degree(bar)
            rhythm = self.style['lead_rhythms'][bar % len(self.style['lead_rhythms'])]
            if self.style['lead_mode'] == 'sparse':
                rhythm = rhythm[:2] if bar % 2 == 0 else [rhythm[-1]]
            elif self.config.complexity < .25:
                rhythm = rhythm[::2]
            notes = []
            for i, (beat, length) in enumerate(rhythm):
                if self.style['lead_mode'] == 'stepwise':
                    step = degree if i == 0 and bar == 0 else max(0, min(11, previous + rng.choice((-1, 1))))
                elif i == 0 or beat == 2:
                    step = _chord_tone(degree, previous, rng)
                else:
                    step = max(0, min(11, previous + rng.choice((-2, -1, 1, 2))))
                notes.append(dict(beat=beat, duration=length * self.style['lead_gate'], degree=step))
                previous = step
            motif.append(notes)
        return motif

    def add(self, role, section, local_bar, beat, length, pitch, velocity, human=False):
        if role not in self.tracks:
            return
        start = (section.start_bar + local_bar) * BAR + round(beat * PPQ)
        if human and self.config.humanize_ms:
            spread = self.config.humanize_ms * self.config.bpm / 60000 * PPQ
            start += round(self.rng('timing', role, section.start_bar, local_bar, beat).uniform(-spread, spread))
            start = max(start, (section.start_bar + local_bar) * BAR)
        end = min(section.end_bar * BAR, start + max(1, round(length * PPQ)))
        if section.start_bar * BAR <= start < end:
            self.tracks[role].notes.append(Note(start, end-start, max(0, min(127, int(pitch))),
                                                max(1, min(127, round(velocity)))))

    def harmony(self, section, bar):
        degree = self.degree(bar)
        voicing = self.voicings[bar % self.phrase_bars]
        root = 24 + (self.key + self.scale[degree]) % 12
        self.chords.append(Chord(section.start_bar + bar, root, voicing, _chord_name(self.key, self.scale, degree)
                                 + ('9' if self.style['extensions'] == 5 else '7' if self.style['extensions'] == 4 else '')))
        return root, voicing

    def pads(self, section, bar, voicing):
        if bar % self.chord_bars == 0:
            for pitch in voicing:
                self.add('pad', section, bar, 0, self.chord_bars * 4 - .08, pitch, 42 + 20 * section.energy)

    def chords_at(self, section, bar, voicing, beats=None, role='chords', strum=False):
        for beat in self.style['chord_beats'] if beats is None else beats:
            for index, pitch in enumerate(voicing):
                offset = index * .018 if strum else 0
                self.add(role, section, bar, beat + offset, self.style['chord_gate'] - offset, pitch,
                         48 + 38 * section.energy)

    def lead(self, section, bar, sparse=False, vocal=False):
        notes = self.motif[bar % self.phrase_bars]
        if sparse:
            notes = notes[:1]
        for index, note in enumerate(notes):
            degree = note['degree']
            if bar >= self.phrase_bars and index > 0 and self.rng('answer', section.start_bar, bar, index).random() < self.config.variation * .3:
                degree = max(0, min(11, degree + 1))
            pitch = self.pitch(self.style['lead_octave'], degree)
            self.add('lead', section, bar, note['beat'], note['duration'], pitch,
                     self.style['lead_velocity'] * (.65 + .35 * section.energy))
            if vocal:
                self.add('vocal', section, bar, note['beat'], note['duration'], pitch - 12, 65 + 18 * section.energy)
            if self.style['lead_mode'] == 'dotted' and note['beat'] + .75 < 4:
                self.add('pluck', section, bar, note['beat'] + .75, min(.3, note['duration']), pitch, 51)

    def arp(self, section, bar, voicing, role='arp', force=False):
        if not force and bar % self.phrase_bars != self.phrase_bars - 1 and self.rng('arp', section.start_bar, bar).random() > self.style['arp_density'] * (.4 + .6 * self.config.complexity):
            return
        step = self.style['arp_step']
        for index in range(round(4 / step)):
            self.add(role, section, bar, index * step, step * .7, voicing[index % len(voicing)] + 12, 58 + 18 * section.energy)

    def drums(self, section, bar, *, active=True, peak=False, build=False):
        if not active:
            return []
        style, progress = self.style, (bar + 1) / section.bars
        beats = style['kick_beats']
        if build:
            beats = [0, 2] if progress < .5 else []
        regions = []
        for beat in beats:
            self.add('kick', section, bar, beat, style['kick_gate'], 36, style['kick_velocity'] * (.65 + .35 * section.energy))
            regions.append((beat, beat + style['kick_gate']))
        if build:
            step = 1 if progress < .4 else .5 if progress < .75 else .25
            if bar == section.bars - 1 and self.config.complexity > .7:
                step = .125
            for i in range(round(4 / step)):
                self.add('snare', section, bar, i * step, .07, 38, 55 + 60 * progress)
        else:
            for beat in style['backbeat']:
                self.add('snare', section, bar, beat, .18 if self.config.style == 'synth-pop' else .1, 38, 70 + 35 * section.energy, human=True)
                if peak:
                    self.add('clap', section, bar, beat, .12, 39, 68 + 25 * section.energy, human=True)
        hat_step = style['hat_step']
        for i in range(round(4 / hat_step)):
            if self.config.style == 'drill' and i % 8 in (2, 5):
                continue
            beat = i * hat_step + (style['swing'] if i % 2 else 0)
            self.add('closed_hat', section, bar, beat, .07, 42, (55 if i % 2 == 0 else 40) + 15 * section.energy, human=True)
        if peak or not build and bar % 2 == 1:
            open_beats = [1.5, 3.5] if self.profile['id'] == 'rap' else [.5, 1.5, 2.5, 3.5]
            for beat in open_beats:
                self.add('open_hat', section, bar, beat, .18, 46, 55 + 20 * section.energy, human=True)
        return regions

    def bass(self, section, bar, root, kicks, beats=None):
        style = self.style
        beats = sorted(style['bass_beats'] if beats is None else beats)
        for i, original in enumerate(beats):
            beat = original
            end = min(4., beat + style['bass_gate'])
            for left, right in kicks:
                if left <= beat < right + .025:
                    beat = right + .025
                elif beat < left < end:
                    end = left - .025
            if end <= beat + .03:
                continue
            pitch = root
            if style['bass_mode'] == 'arpeggiated':
                pitch = self.pitch(24, self.degree(bar) + (0, 4, 7, 4)[i % 4])
            role = style['bass_role']
            slide = style['bass_mode'] == 'slides' and i == len(beats) - 1
            if slide:
                middle = beat + (end - beat) * .65
                self.add(role, section, bar, beat, middle - beat, pitch, style['bass_velocity'])
                self.add(role, section, bar, middle, end - middle, pitch + 12, style['bass_velocity'] * .85)
            else:
                self.add(role, section, bar, beat, end-beat, pitch, style['bass_velocity'] * (.7 + .3 * section.energy))

    def fx(self, section, bar, peak=False, build=False):
        if bar == 0:
            self.add('fx', section, bar, 0, .2, self.pitch(60, 0), 95 if peak else 55)
        if build or peak and bar == section.bars - 1:
            for beat in range(4):
                self.add('riser', section, bar, beat, .9, self.pitch(60, (bar + beat) % 14), 45 + 55 * (bar + 1) / section.bars)

    def finish(self):
        ticks = self.sections[-1].end_bar * BAR
        for track in self.tracks.values():
            _clean(track, ticks)
        role_notes = {role: {
            'kick': 'Short GM kick trigger 36; assign a drum sampler.',
            'sub': 'Monophonic low end. Rap octave transitions are legato cues; enable glide on your instrument.',
            'bass': 'Rhythmic bass with gaps around kick attacks and tails.',
            'lead': 'Shared scale-degree hook with later phrase answers.',
            'vocal': 'MIDI topline or spoken-hit placeholder; assign a voice instrument or sample.',
            'riser': 'Ascending note cues; assign an FX instrument.',
        }.get(role, f'{role.replace("_", " ").title()} notes; assign an appropriate instrument.') for role in self.tracks}
        return Song(deepcopy(self.config), self.sections, list(self.tracks.values()), self.chords,
                    str(self.profile['version']), ['MIDI contains notes, not audio; assign instruments and samples.',
                    'Tempo events are omitted; set the tempo in your DAW. GM programs are audition hints.'],
                    dict(composer=self.profile['id'] + '-phrase-engine', style=self.config.style,
                         progression_degrees=self.progression, progression_degree_numbering='zero-based diatonic',
                         chord_bars=self.chord_bars, phrase_bars=self.phrase_bars,
                         lead_motif=self.motif, role_notes=role_notes, bass_mode=self.style['bass_mode']))
