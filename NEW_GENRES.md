# Pop, Rap and EDM release

Implemented three new profile-driven engines and thirteen styles. Generated song MIDI, GM preview MIDI, individual instrument MIDI and note automation MIDI omit Set Tempo events. Set playback tempo in the DAW; recipe BPM remains a composition reference. EDM supports up to 142 BPM.

## Style trees

- Pop: dance-pop, synth-pop, acoustic-pop, rnb-pop. Full, compact and hook presets; major/minor/dorian. Chorus peaks and stripped pad/vocal bridges.
- Rap: trap, drill, boom-bap, melodic-trap. Full, compact and hook presets; minor/harmonic-minor/dorian. Trap lanes accept 120–170 BPM; boom-bap accepts 80–100 BPM and defaults to 90. Legato 808 notes imply slides; enable glide in the instrument.
- EDM: progressive-house, tech-house, deep-house, uplifting-trance, psy-trance. Full, compact and drop presets; minor/major/dorian/harmonic-minor/phrygian. Full DJ intros/outros are 16 bars. Uplifting trance uses a 16-bar breakdown theme restated in its drops.

## Added files

- `genre_midi/genres/pop.py`, `rap.py`, `edm.py`: genre composers.
- `genre_midi/genres/_common.py`: shared harmony and note-cleanup helpers, extracted from hardstyle.
- `genre_midi/genres/_phrase.py`: deterministic motifs, routing, sections, note placement and groove helpers.
- `profiles/pop.json`, `rap.json`, `edm.json`: styles, arrangements, rhythm/harmony parameters and note-automation policies.
- `Tests/test_pop.py`, `test_rap.py`, `test_edm.py`, `genre_contracts.py`, `test_genre_integration.py`: musical, export, GUI and integration checks.
- `NEW_GENRES.md`: genre release notes.

## Changed files and contracts

- `genre_midi/model.py`: adds major, dorian and phrygian scales; verse, chorus, prechorus, bridge, hook, break and drop2 section identifiers; vocal, sub, pluck, stab and riser roles. Engines accept only the sections supported by their profiles. `Config.validate(profile=None)` validates arrangement names from the selected profile, falling back to full/compact/drop when no arrangement map is supplied. Pure engines pass the supplied profile and perform no file I/O.
- `genre_midi/registry.py`: registers all new engines and validates with the selected profile.
- `genre_midi/genres/hardstyle.py`: imports shared helpers and rejects unsupported custom section kinds cleanly. Existing musical output is preserved.
- `genre_midi/midi.py`, `note_automation.py`, `vendor/motion_fx_codec.py`: omit tempo events in all generated MIDI.
- `genre_midi/automation.py`: accepts the new section kinds and maps new melodic/sub roles to existing automation receivers. Dense linear note rendering is unchanged.
- `genre_midi/sound_sets.py`: compatible bass/sub, chords/stab and arp/pluck mapping when a requested source role is absent; preserves exact source names.
- `studio_gui.py`: shows all profile arrangement keys and new lane roles; applies style reference tempos and correct style-dependent preset lengths.
- `generate_song.py`: accepts profile arrangement keys and initializes genre/style defaults, including boom-bap.
- `genre_midi/service.py`: genre-neutral instrument guidance and tempo-free import instructions.
- `Tests/test_composer.py`: tests hardstyle's own supported scales rather than every global scale. `Tests/test_song_automation.py`: asserts absence of tempo events in the legacy codec.
- `README.md`, `EXTENDING_GENRES.md`: updated user and extension documentation.

## Validation

Baseline before changes: 71 tests passed.

Final command: `python -m pytest Tests/`

```text
133 passed, 1 warning in 78.59s (0:01:18)
```

The warning is a sandbox permission failure writing pytest's optional cache; no tests failed. See `new_genres_pytest.txt` for the complete output.

Additional checks:

- Hardstyle's complete serialized Song data matched the original exactly for euphoric, raw and classic, using the original default seed/configuration.
- Actual GUI automation canvas rectangles verified for all sixteen styles: 1,920 note rectangles and matching velocity rectangles in each two-bar view at default density.
- All thirteen new styles compose for every preset; same seed produces identical Song data and MIDI bytes.
- Exported song packs retain lengths, note density, exact names and recipe round-trips, with no tempo events in any generated MIDI.
- Verified kick/bass gaps, monophonic ownership, section clipping, supported scales, invalid-option rejection and shared trance hook identity.

MIDI provides notes and instrument cues, not finished audio. An archived input `sound_set.mid` is preserved byte-for-byte, including any metadata originally present in that source.
