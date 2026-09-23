"""Genre-aware CLI, GUI, templates and tempo-free exported song packs."""
import json
from pathlib import Path
import shutil
import uuid

import pytest

from genre_midi.midi import parse_midi, song_midi
from genre_midi.note_automation import build_note_lanes, render_note_lane
from genre_midi.model import Config
from genre_midi.registry import get_profile
from genre_midi.service import create_song, export_song, load_recipe
from genre_midi.sound_sets import fill_sound_set
from generate_song import main
from Tests.genre_contracts import generate


@pytest.fixture
def output_parent():
    root = Path(__file__).resolve().parent / '_genre_test_output'
    root.mkdir(exist_ok=True)
    folder = root / uuid.uuid4().hex
    folder.mkdir()
    yield folder
    assert folder.resolve().parent == root.resolve() and len(folder.name) == 32
    shutil.rmtree(folder)


@pytest.mark.parametrize('genre,style', [('pop','rnb-pop'), ('rap','boom-bap'), ('edm','psy-trance'), ('hardstyle','euphoric')])
def test_every_generated_midi_omits_tempo_and_recipes_roundtrip(genre, style, output_parent):
    if genre == 'hardstyle':
        config = Config(sections=[{'kind':'drop', 'bars':1}])
        song = create_song(config)
    else:
        song = generate(genre, style, sections=[{'kind':{'pop':'chorus','rap':'hook','edm':'drop'}[genre], 'bars':1}])
    pack = output_parent / 'pack'
    export_song(song, pack)
    assert (pack / 'automation').is_dir()
    for file in pack.rglob('*.mid'):
        document = parse_midi(file.read_bytes())
        assert not any(e.meta_type == 0x51 for events in document.tracks for e in events), file.name
        assert max(e.tick for events in document.tracks for e in events) == song.ticks
        if 'NOTES' in file.name:
            notes = [e for events in document.tracks for e in events if e.status & 0xf0 == 0x90]
            assert len(notes) == song.bars * 4 * 240
            assert all(0 <= n.data[0] <= 120 and n.data[1] >= 1 for n in notes)
            assert all(b.tick-a.tick == 4 for a,b in zip(notes, notes[1:]))
    regenerated = create_song(load_recipe(pack / 'recipe.json'))
    assert song_midi(regenerated) == (pack / 'song.mid').read_bytes()


def test_cli_profile_defaults_and_hook_presets(output_parent):
    for genre, style, bpm in [('pop','dance-pop',100), ('rap','boom-bap',90), ('edm','psy-trance',140)]:
        folder = output_parent / genre
        args = ['--genre',genre,'--style',style,'--output',str(folder),'--no-automation',
                '--arrangement','drop' if genre=='edm' else 'hook']
        assert main(args) == 0
        report = json.loads((folder/'report.json').read_text(encoding='utf-8'))
        assert report['bpm'] == bpm
        assert report['genre'] == genre


def test_sound_set_bass_alias_and_original_name():
    song = generate('rap', 'trap')
    name = 'My Bass Instrument'
    filled = fill_sound_set(song, {'path':'fixture.mid','sha256':'fixture','warnings':[], 'slots':[
        {'id':'slot','name':name,'role':'bass','channel':2,'program':38,'pitch_hint':30,'controls':[]},
        {'id':'kick','name':'My Kick','role':'kick','channel':10,'program':None,'pitch_hint':36,'controls':[]}]})
    assert filled.tracks[0].notes == next(t.notes for t in song.tracks if t.role=='sub')
    document = parse_midi(song_midi(filled))
    assert [e.data.decode('utf-8') for e in document.tracks[1] if e.meta_type in (3,4)] == [name,name]
    lanes = build_note_lanes(filled, get_profile('rap'))
    assert lanes and all(l['source_track_name'] == name for l in lanes)


def test_gui_all_styles_arrangements_and_recipe_tempo():
    import tkinter as tk
    from studio_gui import StudioApp, LANE_ROLES
    root = tk.Tk()
    root.withdraw()
    try:
        app = StudioApp(root)
        for profile in app.profiles:
            for style, values in profile['styles'].items():
                config = Config(genre=profile['id'], style=style, scale=profile['scales'][0],
                                bpm=values.get('bpm',profile['bpm'])['default'],
                                arrangement=next(reversed(profile['arrangements'])))
                app._apply_config(config)
                assert app._config().to_dict() == config.to_dict()
                assert set(app._arrangement_names.values()) == set(profile['arrangements'])
                assert float(app.bpm_entry.cget('from')) == values.get('bpm',profile['bpm'])['min']
                song = create_song(config)
                app._show_song(song)
                root.update_idletasks()
        assert {'vocal','sub','pluck','stab','riser'} <= set(LANE_ROLES)
    finally:
        root.destroy()
