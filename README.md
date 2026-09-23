# Genre MIDI Studio

The MIDI Toolkit's desktop and command-line song composer. Generate repeatable Hardstyle, Pop, Rap, and EDM arrangements, or fill a MIDI sound-set template while retaining its instrument lane names.

## Run

Use Python 3.10 or newer. The engine needs only the standard library; the desktop window also needs Tkinter, included in the standard Windows Python installer.

```text
python studio_gui.py
```

On Windows, `Start.cmd` uses a local `.venv` if present and otherwise uses `python` on PATH. Windows is the primary desktop target. The command-line engine is portable.

## Create a song

1. Choose a genre, style, key, scale, arrangement, and seed.
2. Optionally load a sound-set MIDI and assign musical roles to its lanes.
3. Generate a preview and review the section map and piano roll.
4. Export a new song folder, then import its MIDI tracks into your DAW at bar 1.

Four installed composers cover euphoric/raw/classic hardstyle; dance/synth/acoustic/R&B pop; trap/drill/boom-bap/melodic trap; and progressive/tech/deep house plus uplifting/psy trance. Available arrangements and tempo ranges vary by style. Reusing a seed and recipe makes the composition repeatable.

The export contains `song.mid`, separate instrument MIDI files, a rough General MIDI preview, `recipe.json`, profile/chord/section reports, import instructions, and checksums. Optional automation is exported separately. MIDI includes leading silence and section positions; do not move each part to its first note. Generated MIDI omits tempo events: set the tempo in your DAW. BPM in the recipe remains a composition reference.

No instruments, samples, presets, reference songs, or audio are included. Choose sounds in FL Studio or your preferred DAW. A MIDI lane named for a vocal or riser is still a note lane, not generated vocal audio. Sound-set inputs and existing export folders are protected.

## Command line

```text
python generate_song.py --list-genres
python generate_song.py --genre rap --style melodic-trap --arrangement hook --seed 7 --output exports/rap-hook
python generate_song.py --recipe exports/rap-hook/recipe.json --output exports/rap-hook-copy
python generate_song.py --inspect-sound-set your-template.mid
```

`python generate_song.py --help` lists role overrides, template selection, note density, and `--no-automation`.

## Automation dependency

The existing `genre_midi/vendor/motion_fx_codec.py` is the embedded Motion FX MIDI protocol codec used by this application's automation modules. It is retained as a required source dependency. This repository does not contain the standalone Motion FX application, VST3 plugin, installer, previews, or preset collection.

Tiny-note automation uses separate files with dense, straight-line MIDI note values. Changing its density does not reroll the musical notes. See [NOTE_AUTOMATION.md](NOTE_AUTOMATION.md) for the format and receiver setup. Normal song MIDI and `--no-automation` exports work without a plugin; live effect control requires a compatible receiver supplied separately. The embedded module is an internal dependency, not a separately packaged command-line product.

## Development and tests

```text
python -m pip install -r requirements-dev.txt
python -m pytest Tests -k "not test_gui"
```

The omitted GUI test can be run on a machine with a desktop and Tkinter. Tests synthesize their own MIDI; no reference music is required. See [EXTENDING_GENRES.md](EXTENDING_GENRES.md) for the composer contract and [NEW_GENRES.md](NEW_GENRES.md) for genre-specific notes.

## License

No application license has been selected for this source release. Public visibility alone does not grant a license to reuse, modify, or redistribute it. Existing third-party notices, where supplied, are retained and apply to their respective components.
