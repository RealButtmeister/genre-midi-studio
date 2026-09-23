"""Read named MIDI lanes and fill their musical roles without changing the source."""
from __future__ import annotations
from collections import Counter,defaultdict
from dataclasses import replace
from pathlib import Path
import hashlib
import re
from .model import Song,Track,Note,Config,ROLES,GenerationError
from .midi import MAX_BYTES,parse_midi,text_value

DRUM_ROLES={"kick","snare","clap","closed_hat","open_hat","cymbal","percussion"}
ALIASES={
    "kick":("kick","bass drum","bd"),
    "bass":("bass","sub","reverse bass"),
    "lead":("lead","melody","hook","main synth"),
    "chords":("chord","chords","piano","keys","guitar","organ"),
    "pad":("pad","pads","strings","choir","atmosphere"),
    "arp":("arp","arpeggio","arpeggiator","pluck","plucks"),
    "screech":("screech","screeches","acid"),
    "fx":("fx","riser","sweep","impact","effects"),
    "snare":("snare","roll"),"clap":("clap","claps"),
    "closed_hat":("closed hat","closed hats","closed hihat","closed hi hat","chh","hat","hats","hihat","hi hat"),
    "open_hat":("open hat","open hats","open hihat","open hi hat","ohh"),
    "cymbal":("cymbal","cymbals","crash","ride","splash"),
    "percussion":("perc","percussion","shaker","tom","toms","tambourine","rim","cowbell","conga","bongo","woodblock","clave")
}

def _match_role(name:str)->str|None:
    normal=re.sub(r"[_\-/]+"," ",name.lower())
    normal=re.sub(r"\s+"," ",normal)
    # Specific compound descriptions precede broad hat/lead aliases.
    for role in ("kick","open_hat","closed_hat","bass","snare","clap","cymbal","percussion","lead","chords","pad","arp","screech","fx"):
        for alias in ALIASES[role]:
            if re.search(r"(?<![a-z])"+re.escape(alias)+r"(?![a-z])",normal):return role
    return None

def infer_role(name:str,channel:int,pitch:int|None,program:int|None)->tuple[str,float]:
    # Toolkit labels put the musical role before the timbre after " - ".
    parts=re.split(r"\s[-–—:]\s",name,maxsplit=1)
    primary=parts[0]
    role=_match_role(primary) or _match_role(name)
    # The toolkit also labels hats "Hat - open 909": "open" specifies the
    # drum role, rather than an unrelated synth timbre. This must work even
    # for truly empty templates without a GM46 placeholder note.
    if (role=="closed_hat" and len(parts)>1 and re.search(r"\bopen\b",parts[1].lower())
            and not re.search(r"\b(?:closed|chh)\b",primary.lower())):
        role="open_hat"
    if role:return role,0.95
    if channel==10 and pitch is not None:
        percussion={35:"kick",36:"kick",38:"snare",40:"snare",39:"clap",42:"closed_hat",44:"closed_hat",46:"open_hat",49:"cymbal",51:"cymbal",52:"cymbal",55:"cymbal",57:"cymbal",59:"cymbal"}
        return percussion.get(pitch,"percussion"),0.8
    # Program families suggest a timbre, not a compositional role. Require an
    # explicit mapping for unnamed melodic lanes instead of guessing silently.
    return "unassigned",0.0

def inspect_sound_set(path:Path)->dict:
    path=Path(path).expanduser().resolve()
    if not path.is_file():raise GenerationError(f"Sound set not found: {path}")
    if path.stat().st_size>MAX_BYTES:raise GenerationError("Sound set MIDI exceeds the 32 MB limit")
    # Parse and fingerprint one bounded snapshot. A source saved concurrently
    # must never contribute its old lane data and a different version's hash.
    with path.open("rb") as stream:source=stream.read(MAX_BYTES+1)
    if len(source)>MAX_BYTES:raise GenerationError("Sound set MIDI exceeds the 32 MB limit")
    doc=parse_midi(source);slots=[];warnings=[];used_channels=set()
    for index,events in enumerate(doc.tracks):
        names=[text_value(e.data) for e in events if e.meta_type==3]
        name=next((n for n in names if n.strip()),f"Track {index+1}")
        instrument_names=[text_value(e.data) for e in events if e.meta_type==4]
        instrument_name=next((n for n in instrument_names if n.strip()),None)
        if instrument_name and (not any(n.strip() for n in names) or re.fullmatch(r"(?:track|channel)\s*\d+",name.strip(),re.IGNORECASE)):
            name=instrument_name
        messages=[e for e in events if 0x80<=e.status<=0xef]
        channels=sorted({(e.status&15)+1 for e in messages})
        notes=[e for e in messages if e.status&0xf0==0x90 and e.data[1]>0]
        has_conductor=any(e.meta_type in (0x51,0x58,0x59) for e in events)
        if not messages and (has_conductor or (index==0 and name.lower() in ("conductor","tempo","track 1"))):continue
        if len(channels)>1:
            raise GenerationError(f"Lane '{name}' uses multiple MIDI channels. Export/split it into one instrument lane per channel first.")
        prefixes=[e.data[0]+1 for e in events if e.meta_type==0x20 and len(e.data)==1 and e.data[0]<16]
        if not channels and prefixes:
            if len(set(prefixes))>1:raise GenerationError(f"Lane '{name}' has multiple MIDI channel prefixes. Split it into separate lanes first.")
            channel=prefixes[0]
        elif not channels:
            channel=next((c for c in range(1,17) if c!=10 and c not in used_channels),1)
            warnings.append(f"{name}: no MIDI channel stored; assigned channel {channel}.")
        else:channel=channels[0]
        used_channels.add(channel)
        first_note=min((e.tick for e in notes),default=2**63)
        setup=[e for e in messages if e.tick<=first_note]
        programs=[e.data[0] for e in setup if e.status&0xf0==0xc0]
        program=programs[-1] if programs else None
        controls={e.data[0]:e.data[1] for e in setup if e.status&0xf0==0xb0 and e.data[0]<120}
        pitch_counts=Counter(e.data[0] for e in notes)
        pitch=pitch_counts.most_common(1)[0][0] if pitch_counts else None
        role,confidence=infer_role(name,channel,pitch,program)
        slots.append({"id":f"track_{index}","name":name,"channel":channel,"program":program,
            "role":role,"confidence":confidence,"pitch_hint":pitch,"controls":[list(pair) for pair in controls.items()],
            "source_note_count":len(notes)})
    if not slots:raise GenerationError("The MIDI contains no instrument lanes. Export a named MIDI track for each instrument.")
    if len(slots)>128:raise GenerationError("A sound set may contain at most 128 instrument lanes")
    counts=Counter(s["channel"] for s in slots)
    if any(count>1 and channel!=10 for channel,count in counts.items()):warnings.append("Some melodic lanes share MIDI channels. Import by track or use the separate lane MIDIs to route them independently.")
    if any(s["source_note_count"]>1 for s in slots):warnings.append("Existing notes are used only as lane pitch hints; the generated song replaces their patterns in a new file.")
    return {"path":str(path),"ppq":doc.ppq,"slots":slots,"warnings":warnings,"sha256":hashlib.sha256(source).hexdigest()}

def fill_sound_set(song:Song,info:dict)->Song:
    lookup={track.role:track for track in song.tracks}
    unknown_ids=set(song.config.lane_roles)-{slot["id"] for slot in info["slots"]}
    if unknown_ids:raise GenerationError("Sound-set mapping references missing slots: "+", ".join(sorted(unknown_ids)))
    result=[];role_counts=defaultdict(int);mapping=[];unassigned=[]
    for slot in info["slots"]:
        role=song.config.lane_roles.get(slot["id"],slot["role"])
        if role not in ROLES:unassigned.append(slot["name"]);continue
        source=lookup.get("closed_hat" if role=="percussion" else role)
        if source is None:
            equivalents = {"bass": ("sub",), "sub": ("bass",), "chords": ("stab",),
                           "stab": ("chords",), "arp": ("pluck",), "pluck": ("arp",),
                           "riser": ("fx",)}
            source = next((lookup[r] for r in equivalents.get(role, ()) if r in lookup), None)
        notes=[]
        layer=role_counts[role];role_counts[role]+=1
        if source is not None and role!="skip":
            for note in source.notes:
                pitch=note.pitch
                # GM drum keys and one-shot sampler placeholders are assignments,
                # not harmonic information. Keep the selected drum's trigger key.
                if role in DRUM_ROLES and (role!="kick" or song.config.kick_mode=="fixed" or slot["channel"]==10):
                    if slot["pitch_hint"] is not None:pitch=slot["pitch_hint"]
                    elif role=="kick":pitch=36
                elif role=="lead" and layer%2 and note.pitch<=84:pitch+=12
                elif role=="pad" and layer%2 and note.pitch>=48:pitch-=12
                notes.append(Note(note.start,note.duration,pitch,note.velocity))
        # A cymbal's two canonical pitches may collapse to a single sampler key;
        # shorten a previous hit if needed, without introducing doubled notes.
        clean=[];last_by_pitch={}
        for note in sorted(notes,key=lambda n:(n.start,n.pitch,-n.velocity)):
            previous_index=last_by_pitch.get(note.pitch)
            if previous_index is not None:
                previous=clean[previous_index]
                if previous.start==note.start:continue
                if previous.end>note.start:clean[previous_index]=Note(previous.start,note.start-previous.start,previous.pitch,previous.velocity)
            last_by_pitch[note.pitch]=len(clean);clean.append(note)
        result.append(Track(slot["id"],slot["name"],role,slot["channel"],slot["program"],clean,[tuple(c) for c in slot["controls"]]))
        mapping.append({"slot_id":slot["id"],"name":slot["name"],"role":role,"channel":slot["channel"],"program":slot["program"],"pitch_hint":slot["pitch_hint"]})
    if unassigned:raise GenerationError("Assign a role to these sound-set lanes before generating: "+", ".join(unassigned))
    if all(t.role=="skip" for t in result):raise GenerationError("Assign at least one sound-set lane a musical role")
    metadata=dict(song.metadata)
    metadata["sound_set"]={"source":info["path"],"sha256":info["sha256"],"mapping":mapping}
    return replace(song,tracks=result,metadata=metadata,warnings=song.warnings+info["warnings"])
