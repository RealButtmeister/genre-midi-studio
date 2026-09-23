# Adding genres without changing the song/export pipeline

The GUI and CLI consume a common `Config -> Song -> song pack` pipeline. A genre owns its musical decisions. MIDI writing, sound-set lane mapping, recipes, reports and controller rendering are shared. Hardstyle, Pop, Rap and EDM are registered engines.

## Engine contract

1. Add `genre_midi/genres/<genre>.py` with `compose(config: Config, profile: dict) -> Song`.
2. Register that callable through `register_engine("engine_id", compose)` in the registry's built-in initialization. Registration is explicit Python code. Profile JSON cannot import or execute arbitrary modules.
3. Add `profiles/<genre>.json` with `schema_version: 1`, unique `id`, display `name`, `version`, registered `engine`, tempo limits/default, `scales`, `meter: [4,4]` and a nonempty `styles` object. Put genre-specific rules and patterns here.
4. Return contiguous `Section` entries and meaningful canonical-role `Track` entries. Tracks have unique IDs, nonempty names, channels 1–16 and optional zero-based GM programs. Notes use integer ticks at **960 PPQ**, MIDI pitches 0–127 and velocities 1–127. Clip notes to the arrangement and prevent overlapping notes of the same pitch in a lane.
5. Set `Song.profile_version` to the profile version. Supply chord annotations and useful composition metadata. Use `stable_rng(seed, genre, role, phrase, ...)` for independent repeatable random streams, so one new percussion part does not reroll the entire melody.

Call `validate_song` on generated material and exercise the shared export function. A newly registered engine and valid profile appear in the GUI without a new genre-specific window. Profiles loaded by the app must all be valid; invalid or unregistered entries produce actionable errors.

## Musical roles and templates

Current roles are `kick`, `bass`, `lead`, `chords`, `pad`, `arp`, `screech`, `fx`, `snare`, `clap`, `closed_hat`, `open_hat`, `cymbal`, `percussion`, `skip`, `vocal`, `sub`, `pluck`, `stab` and `riser`. Imported MIDI lanes map to those roles. Bass/sub, arp/pluck and chords/stab mapping use a counterpart if the requested lane is absent. The generic `percussion` template role borrows the engine's closed-hat rhythm.

Multiple imported lanes for the same role use the shared layer rules. Genre-specific articulations, instrument ranges and independent counterpoint require new explicit role or slot controls, not guesses from program numbers.

The shared model permits natural minor, harmonic minor, major, dorian and phrygian, and uses 4/4. Profiles restrict their own scales. New scales require interval sets in `SCALES`. Other meters require extending timeline math, validators, the GUI and automation together. `Config.validate(profile=None)` accepts the selected profile's arrangement keys, falling back to full/compact/drop when that field is absent. Omit the argument for installed-profile lookup; pure composers pass their supplied profile to avoid file I/O. GUI and CLI accept profile arrangement keys, including hook. Custom sections are available in recipes; engines reject section kinds absent from their energy map.

## Automation policies

`build_note_lanes(song, profile)` in `note_automation.py` expands the internal lead/bass/pad policy into per-instrument tiny-note lanes. Pitch and velocity follow the same straight normalized ramps. Original instrument names, default 240-note density and unmerged repeated notes are required; see AGENTS.md and NOTE_AUTOMATION.md. The internal `build_automation(song, profile)` still supplies physical parameter anchors. Hardstyle-specific section behavior is isolated in `genre_midi/automation.py`. Other genres start from neutral section policies and can provide `automation.role_limits` and `automation.section_amounts`; add a dedicated policy when the music needs different transitions.

Preserve the Motion FX pitch-value mapping, physical parameter ranges, deterministic score generation and receiver separation. MIDI trigger timings must use the composed kick notes, after sound-set mapping. Profiles without a kick lane must not invent kick triggers. Changes to the effect's note-value contract require coordinated updates to Motion FX, the note renderer, previews and tests.

The vendored `genre_midi/vendor/motion_fx_codec.py` is the codec from the companion Motion FX toolkit. It is copied here to keep this app self-contained; no code is imported from the older `autogen.py` or from user templates.

## Acceptance checks for a new genre

- Distinct, documented rhythm, harmony, phrase development and arrangement behavior across styles and multiple seeds.
- Stable same-seed MIDI, with melody or rhythm identity reused across relevant sections.
- Valid notes and voice limits at minimum/maximum controls, short arrangements and 512-bar boundaries.
- Useful results both with built-in lanes and sparse/custom sound sets; missing roles handled explicitly.
- Exact full beat timeline across song, per-lane and control files, no hanging notes, and **no Set Tempo (0x51) events**. Playback tempo belongs to the DAW; BPM remains in the recipe and report only.
- Automation within receiver-specific limits, with deliberate transitions and initialized controls.
- Independent MIDI-parser checks and listening in a real DAW with representative instruments before claiming host or sound-design validation.

Bump profile versions when musical rules change. Exported recipes record generator/profile identity and the copied template. Keep older app/profile versions when long-term byte-identical regeneration is needed.
