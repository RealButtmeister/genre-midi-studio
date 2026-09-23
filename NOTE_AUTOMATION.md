# Straight-line MIDI note automation

Version 1.1 exports automation as tiny MIDI notes. The note heights and velocities follow the same straight-line shape. There are no CC or pitch-bend events in these automation files.

## The value mapping

| Value | Note in FL Studio | MIDI pitch | Velocity |
|---|---|---|---|
| 0% | C0 | 0 | 1 |
| 25% | F#2 | 30 | 32 |
| 50% | C5 | 60 | 64 |
| 75% | F#7 | 90 | 95 |
| 100% | C10 | 120 | 127 |

Both dimensions come from the same value. Velocity uses its own 127-step range. At the bottom it must be 1, because velocity 0 means note-off and would make the zero-value notes disappear. Octave labels above follow [FL Studio's note numbering](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/pianoroll_scripting_api.htm); other DAWs may label the same MIDI pitches differently.

## Tiny notes, straight shapes

The default is **240 notes per quarter beat**: four MIDI ticks each at 960 PPQ, about 1.67 ms at 150 BPM. Choose 120, 240, 480 or 960 notes per beat. At 960, every note is one tick long.

Equal pitches remain separate tiny notes. Notes are never merged into long blocks, and longer songs do not silently get coarser timing. Exact off-grid kick hits can split a tiny note into even shorter pieces. Every note-off occurs before the next note-on at the shared boundary.

Ramps interpolate linearly in automation percentage. This also keeps filter ramps visually straight despite the filter's logarithmic frequency range. Corners and intended kick resets stay sharp. The Motion lane uses straight descending ramps triggered by the actual kick hits. There are no eased, exponential, sine or S-shaped automation segments.

The piano roll still has discrete pitch rows; more notes increase horizontal density. Motion FX can smooth the resulting parameter changes during playback with its Smoothing control.

## Original channel names

Each automation file identifies its original instrument, such as `I3 Lead - supersaw | Cutoff`. MIDI track-name metadata includes that name and target; instrument-name metadata contains the exact original name. Separate files retain original source IDs, so duplicate channel names remain distinguishable.

The song and individual musical MIDIs store the original name in both standard track-name and instrument-name fields. Templates that store names only in the instrument-name field are also supported.

## Preview and export

Generate a preview, switch **Preview** to **Tiny-note automation**, select an instrument/target and inspect two bars. The upper panel draws the actual tiny MIDI notes; the lower panel draws their actual velocities. Change the start bar to inspect another part of the song. Changing note density updates automation without changing the musical notes.

`automation/*_NOTES.mid` contains one instrument's one value lane. `NOTE_LANES.json` records names, source channels, note density and counts. `SETUP.json` records initial effect settings. Only changing continuous controls are rendered; fixed settings and discrete selectors remain settings.

Import every lane at bar 1 and preserve leading silence. You can use these MIDI files directly as visible guides when drawing automation in your DAW.

## Motion FX 1.2 receiver

For live note control, select the matching **Note target** in Motion FX and send the file to that instance on MIDI channel 1. For example, a Cutoff file needs Note target **Cutoff** and Filter Type **Low-pass**. A Motion file needs Note target **Motion**; set the Pump depth for the desired amount of ducking.

Choose **Note value: Pitch height** to read pitch 0-120 as 0-100%, or **Velocity** to read the matching velocity lane (1 = 0%, 64 = 50%, 127 = 100%). The **Velocity sidechain** factory preset selects Motion and Velocity with 3 ms smoothing. Tiny note-offs hold the last value. Notes such as 36 and 38 are ordinary values while a Note target is selected. Note target **Off** preserves the earlier trigger behavior for existing projects.

One instance reads one selected value target. Use separate instances for simultaneous independent targets; sending several value lanes into one selected target would make them compete. Other effect settings remain under your control. Start at bar 1 to initialize values.

## Scripts

```powershell
python generate_song.py --recipe examples/Euphoric_Full_Song_v1_1/recipe.json --note-density 240 --output New_Straight_Note_Song
```

Saved recipes retain note density. Musical composition and automation rendering are separate: the same musical recipe produces the same musical notes when you change the automation density.
