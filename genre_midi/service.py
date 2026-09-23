"""Generation and atomic song-pack export shared by GUI and CLI."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import uuid
from . import __version__
from .model import Config,Song,GenerationError,PPQ,BAR,SECTION_KINDS
from .registry import available_genres,compose_song,get_profile,profile_fingerprint
from .sound_sets import inspect_sound_set,fill_sound_set
from .midi import song_midi,parse_midi
from .automation import RECEIVER_ROLES
from .note_automation import build_note_lanes,static_note_settings,render_note_lane

def validate_song(song:Song)->dict:
    song.config.validate()
    if not song.sections:raise GenerationError("Composer returned an invalid song length")
    end=0
    for section in song.sections:
        if section.kind not in SECTION_KINDS or any(type(value) is not int for value in (section.start_bar,section.bars)) or isinstance(section.energy,bool) or not isinstance(section.energy,(int,float)):raise GenerationError("Song sections need valid kinds, integer bars and numeric energy")
        if section.start_bar!=end or section.bars<1 or not math.isfinite(section.energy) or not 0<=section.energy<=1:raise GenerationError("Song sections must be contiguous, positive and finite")
        end=section.end_bar
    if not 1<=end<=512:raise GenerationError("Composer returned an invalid song length")
    if not song.tracks or len(song.tracks)>128:raise GenerationError("Song must contain 1 to 128 instrument lanes")
    ids=set();total=0;empty=[]
    for track in song.tracks:
        if track.id in ids:raise GenerationError("Composer returned duplicate track IDs")
        ids.add(track.id)
        if not isinstance(track.name,str) or not track.name:raise GenerationError("Track name is missing")
        if type(track.channel) is not int or not 1<=track.channel<=16 or (track.program is not None and (type(track.program) is not int or not 0<=track.program<=127)):raise GenerationError("Invalid MIDI routing or program")
        for cc,value in track.controls:
            if type(cc) is not int or type(value) is not int or not 0<=cc<=127 or not 0<=value<=127:raise GenerationError("Invalid MIDI controller initialization")
        last_by_pitch={}
        for note in sorted(track.notes,key=lambda n:(n.start,n.pitch)):
            if any(isinstance(value,bool) or not isinstance(value,int) for value in (note.start,note.duration,note.pitch,note.velocity)):raise GenerationError("Composer notes must use integer MIDI ticks and values")
            if not 0<=note.start<note.end<=song.ticks or not 0<=note.pitch<=127 or not 1<=note.velocity<=127:raise GenerationError("Composer note is outside MIDI bounds or song duration")
            if note.start<last_by_pitch.get(note.pitch,-1):raise GenerationError(f"Same-pitch notes overlap in lane {track.name}")
            last_by_pitch[note.pitch]=note.end
        if not track.notes:empty.append(track.name)
        total+=len(track.notes)
    if total>400000:raise GenerationError("Arrangement exceeds the 400,000-note limit; reduce its length or number of lanes")
    if not total:raise GenerationError("The selected sections and sound-set roles produce no notes; add an active section or instrument role")
    return {"notes":total,"tracks":len(song.tracks),"bars":song.bars,"empty_lanes":empty}

def create_song(config:Config)->Song:
    # Freeze a copy, so later GUI edits cannot change a pending preview/export.
    config=Config.from_dict(config.to_dict())
    info=inspect_sound_set(Path(config.sound_set)) if config.sound_set else None
    if info:
        for slot in info["slots"]:
            role=config.lane_roles.get(slot["id"],slot["role"])
            if role=="unassigned":raise GenerationError("Assign a role to the sound-set lane '"+slot["name"]+"' before generating")
    song=compose_song(config)
    if info:song=fill_sound_set(song,info)
    validate_song(song)
    return song

def load_recipe(path:Path)->Config:
    path=Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size>2*1024*1024:raise GenerationError("Recipe is missing or exceeds 2 MB")
    try:data=json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError,json.JSONDecodeError) as exc:raise GenerationError("Cannot read recipe: "+str(exc)) from exc
    if isinstance(data,dict) and "config" in data:
        if type(data.get("schema_version")) is not int or data["schema_version"]!=1:raise GenerationError("Unsupported recipe schema version")
        if str(data.get("generator_version","1")).split(".")[0]!=__version__.split(".")[0]:raise GenerationError("Recipe needs a different major generator version")
        config_data=data["config"]
    else:config_data=data
    config=Config.from_dict(config_data)
    if isinstance(data,dict) and "config" in data:
        profile=get_profile(config.genre)
        if "profile_version" in data and data["profile_version"]!=profile["version"]:raise GenerationError("Recipe needs a different genre profile version; restore the matching profile before regenerating")
        if "profile_sha256" in data and data["profile_sha256"]!=profile_fingerprint(profile):raise GenerationError("Recipe genre profile contents have changed; restore the matching profile before regenerating")
    if config.sound_set:
        source=Path(config.sound_set).expanduser()
        config.sound_set=str((source if source.is_absolute() else path.parent/source).resolve())
    return config

def _json(path:Path,value:object)->None:
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")

def _slug(name:str)->str:
    return re.sub(r"[^A-Za-z0-9_-]+","_",name).strip("_")[:70] or "lane"

def export_song(song:Song,output:Path,include_automation:bool=True,*,note_density:int=240)->dict:
    stats=validate_song(song)
    if type(note_density) is not int or note_density not in (120,240,480,960):raise GenerationError("Note density must be 120, 240, 480 or 960 notes per beat")
    output=Path(output).expanduser().resolve()
    if output.exists():raise GenerationError("Output folder already exists. Choose a new folder; existing song packs are never overwritten.")
    profile=get_profile(song.config.genre)
    current_profile_hash=profile_fingerprint(profile)
    if profile["version"]!=song.profile_version:raise GenerationError("Genre profile changed since the preview. Generate a new preview before exporting.")
    if song.metadata.get("profile_sha256",current_profile_hash)!=current_profile_hash:raise GenerationError("Genre profile contents changed since the preview. Generate a new preview before exporting.")
    source_bytes=None
    sound_info=song.metadata.get("sound_set")
    if sound_info:
        try:source_bytes=Path(sound_info["source"]).read_bytes()
        except OSError as exc:raise GenerationError("Sound-set source is no longer readable; regenerate the preview") from exc
        if hashlib.sha256(source_bytes).hexdigest()!=sound_info["sha256"]:raise GenerationError("Sound set changed since the preview. Generate a new preview before exporting.")
    note_lanes=build_note_lanes(song,profile) if include_automation else []
    output.parent.mkdir(parents=True,exist_ok=True)
    staging=output.parent/("."+output.name+".building-"+uuid.uuid4().hex)
    staging.mkdir()
    try:
        (staging/"tracks").mkdir()
        main=song_midi(song)
        parse_midi(main) # Validate the finished MIDI container before publishing.
        (staging/"song.mid").write_bytes(main)
        (staging/"preview_gm.mid").write_bytes(song_midi(song,gm_preview=True))
        track_report=[]
        for index,track in enumerate(song.tracks,1):
            filename=f"{index:02d}_{_slug(track.name)}.mid"
            (staging/"tracks"/filename).write_bytes(song_midi(song,[track]))
            track_report.append({"id":track.id,"name":track.name,"role":track.role,"channel":track.channel,
                "program":track.program,"notes":len(track.notes),"file":"tracks/"+filename})
        recipe_config=song.config.to_dict()
        if source_bytes is not None:
            (staging/"sound_set.mid").write_bytes(source_bytes)
            recipe_config["sound_set"]="sound_set.mid"
            recipe_config["lane_roles"]={t.id:t.role for t in song.tracks}
        recipe={"schema_version":1,"generator_version":__version__,"profile_version":song.profile_version,"profile_sha256":current_profile_hash,"note_density":note_density,"config":recipe_config}
        _json(staging/"recipe.json",recipe)
        _json(staging/"genre_profile.json",profile)
        _json(staging/"chords.json",[asdict(c) for c in song.chords])
        automation_report=[]
        if note_lanes:
            (staging/"automation").mkdir()
            _json(staging/"automation"/"SETUP.json",static_note_settings(song,profile))
        for lane in note_lanes:
            data,note_stats=render_note_lane(song,lane,density=note_density)
            filename=_slug(lane["source_track_id"])+"_"+_slug(lane["source_track_name"])+"_"+lane["target"]+"_NOTES.mid"
            (staging/"automation"/filename).write_bytes(data)
            role=lane["receiver"]
            automation_report.append({"receiver":role,"target":lane["target"],"name":lane["name"],
                "midi":"automation/"+filename,"format":"tiny-pitch-notes","shape":"linear",
                "targets":[lane["source_track_name"]],"source_track_id":lane["source_track_id"],
                "source_channel":lane["source_channel"],"channel":1,
                "notes_per_beat":note_density,"note_length_ticks":PPQ//note_density,"statistics":note_stats})
        if note_lanes:
            _json(staging/"automation"/"NOTE_LANES.json",automation_report)
        report={"schema_version":1,"generator_version":__version__,"genre":song.config.genre,
            "style":song.config.style,"profile_version":song.profile_version,"seed":song.config.seed,
            "key":song.config.key,"scale":song.config.scale,"bpm":song.config.bpm,"bars":song.bars,
            "duration_seconds":song.bars*4*60/song.config.bpm,"ppq":PPQ,"validation":stats,
            "sections":[asdict(s) for s in song.sections],"tracks":track_report,
            "automation":automation_report,"note_density":note_density,"warnings":song.warnings,"composition":song.metadata}
        _json(staging/"report.json",report)
        mapping="\n".join(f"- {t['name']} — {t['role']}, channel {t['channel']}, {t['notes']} notes" for t in track_report)
        routes="\n".join(f"- {r['midi']} → Note target: {r['target']}; instruments: {', '.join(r['targets'])}" for r in automation_report)
        (staging/"START_HERE.txt").write_text(
            f"GENRE MIDI STUDIO — {profile['name']} / {song.config.style}\n\n"
            f"{song.bars} bars, {song.config.bpm:g} BPM, {song.config.key} {song.config.scale}, seed {song.config.seed}.\n"
            "Import song.mid as separate instrument tracks, or import files in tracks/ individually at bar 1.\n"
            "Generated MIDI omits tempo events. Set tempo in your DAW; the recipe BPM is a composition reference.\n"
            "All files include the full timeline, leading silence and section markers. Do not realign each lane to its first note.\n"
            "Choose instruments and samples for the selected genre. MIDI does not contain samples, synth presets or audio.\n"
            "preview_gm.mid adds rough General MIDI sound hints; these are for checking notes, not finished sound design.\n\n"
            "LANES\n"+mapping+"\n\nMOTION FX ROUTING\n"+(routes or "No eligible automation receivers were exported.")+"\n"
            f"Automation is tiny back-to-back MIDI notes: {note_density} per quarter beat, {PPQ//note_density} ticks each at {PPQ} PPQ.\n"
            "Note height C0–C10 (MIDI 0–120 in FL Studio) is value 0–100%; velocity follows the same percentage on its 1–127 scale.\n"
            "The zero endpoint uses pitch 0 and velocity 1 to remain a visible note; MIDI velocity 0 means note-off.\n"
            "Every tiny note is kept even when pitches repeat. Straight ramps and sharp corners; no eased curves.\n"
            "There are no CC or pitch-bend messages in these automation files. Use them as visible piano-roll automation guides.\n"
            "For live control use Motion FX 1.1's Note target selector: choose the parameter matching that file and MIDI channel 1.\n"
            "Route each simultaneous value lane to a separate instance. A file cannot select or automate every parameter in one instance.\n"
            "For cutoff select Low-pass; for motion choose Note target Motion and set Pump depth to taste. Other effect settings stay on the plugin.\n"
            "automation/SETUP.json records omitted static settings. Start at bar 1; ordinary tiny note-offs hold the last value in Motion FX.\n"
            "For exact regeneration load recipe.json in the designer. Custom sound-set source is preserved as sound_set.mid.\n"
            "report.json contains sections, roles, note counts and warnings. Empty lanes may be intentional for the selected arrangement.\n",
            encoding="utf-8")
        hashes=[]
        for file in sorted(staging.rglob("*")):
            if file.is_file():hashes.append(hashlib.sha256(file.read_bytes()).hexdigest()+"  "+file.relative_to(staging).as_posix())
        (staging/"SHA256SUMS.txt").write_text("\n".join(hashes)+"\n",encoding="utf-8")
        os.rename(staging,output)
        return {"output_dir":str(output),"total_bars":song.bars,"duration_seconds":report["duration_seconds"],
            "tracks":track_report,"automation":automation_report,"files":[p.relative_to(output).as_posix() for p in sorted(output.rglob("*")) if p.is_file()]}
    finally:
        # Only this call's UUID staging directory can be removed on failure.
        if staging.exists() and staging.parent.resolve()==output.parent and staging.name.startswith("."+output.name+".building-"):
            shutil.rmtree(staging)
