"""Bounded SMF I/O for musical notes and imported instrument-lane templates."""
from __future__ import annotations
from dataclasses import dataclass
from collections import Counter
import struct
from pathlib import Path
from .model import Song,Track,Note,GenerationError,PPQ

MAX_BYTES=32*1024*1024
MAX_EVENTS=1_000_000

@dataclass(frozen=True)
class Event:
    tick:int
    status:int
    data:bytes
    meta_type:int|None=None

@dataclass
class MidiDocument:
    format:int
    ppq:int
    tracks:list[list[Event]]

def _vlq(value:int)->bytes:
    if not 0<=value<=0x0fffffff: raise GenerationError("MIDI delta time is too large")
    result=[value&127]
    value>>=7
    while value:
        result.insert(0,(value&127)|128)
        value>>=7
    return bytes(result)

def _read_vlq(data:bytes,pos:int,end:int)->tuple[int,int]:
    value=0
    for _ in range(4):
        if pos>=end: raise GenerationError("Truncated MIDI delta/length")
        byte=data[pos];pos+=1;value=(value<<7)|(byte&127)
        if not byte&128:return value,pos
    raise GenerationError("Invalid MIDI variable-length integer")

def read_midi(path:Path)->MidiDocument:
    path=Path(path)
    if not path.is_file(): raise GenerationError(f"Sound set not found: {path}")
    if path.stat().st_size>MAX_BYTES: raise GenerationError("Sound set MIDI exceeds the 32 MB limit")
    return parse_midi(path.read_bytes())

def parse_midi(data:bytes)->MidiDocument:
    if len(data)>MAX_BYTES or len(data)<14 or data[:4]!=b"MThd": raise GenerationError("This file is not a supported standard MIDI file")
    header_size=struct.unpack_from(">I",data,4)[0]
    if header_size<6 or 8+header_size>len(data): raise GenerationError("Invalid MIDI header")
    fmt,count,ppq=struct.unpack_from(">HHH",data,8)
    if fmt not in (0,1): raise GenerationError("Format-2 MIDI is not supported; export a format-0 or format-1 sound set")
    if not 1<=count<=256 or (fmt==0 and count!=1): raise GenerationError("MIDI track count is invalid or exceeds 256")
    if ppq==0 or ppq&0x8000: raise GenerationError("SMPTE MIDI timing is not supported; export PPQ/beat-based MIDI")
    pos=8+header_size;tracks=[];total_events=0
    while len(tracks)<count:
        if pos+8>len(data): raise GenerationError("MIDI track header is missing")
        tag=data[pos:pos+4];size=struct.unpack_from(">I",data,pos+4)[0];pos+=8;end=pos+size
        if end>len(data): raise GenerationError("Truncated MIDI track")
        if tag!=b"MTrk":pos=end;continue
        events=[];tick=0;running=None;ended=False
        while pos<end:
            delta,pos=_read_vlq(data,pos,end);tick+=delta
            if pos>=end: raise GenerationError("MIDI event is missing")
            status=data[pos]
            if status&128:pos+=1
            elif running is not None:status=running
            else: raise GenerationError("MIDI running status has no preceding channel message")
            meta=None
            if 0x80<=status<=0xef:
                running=status
                length=1 if status&0xf0 in (0xc0,0xd0) else 2
                if pos+length>end or any(b&128 for b in data[pos:pos+length]): raise GenerationError("Invalid MIDI channel message")
            elif status==0xff:
                # Tolerate running status across meta events for compatibility
                # with existing files/readers. Our writer emits explicit status.
                if pos>=end:raise GenerationError("Truncated MIDI meta event")
                meta=data[pos];pos+=1;length,pos=_read_vlq(data,pos,end)
            elif status in (0xf0,0xf7):
                running=None;length,pos=_read_vlq(data,pos,end)
            else: raise GenerationError("Unsupported system event in MIDI track")
            if pos+length>end: raise GenerationError("Truncated MIDI event payload")
            payload=data[pos:pos+length];pos+=length
            events.append(Event(tick,status,payload,meta));total_events+=1
            if total_events>MAX_EVENTS:raise GenerationError("MIDI exceeds one million events")
            if meta==0x2f:
                ended=True
                if payload:raise GenerationError("Invalid end-of-track event")
                if pos!=end:raise GenerationError("Data occurs after MIDI end-of-track")
        if not ended:raise GenerationError("MIDI track is missing its end-of-track event")
        tracks.append(events)
    if pos!=len(data):raise GenerationError("Unexpected data after the declared MIDI tracks")
    return MidiDocument(fmt,ppq,tracks)

def text_value(data:bytes)->str:
    try:return data.decode("utf-8")
    except UnicodeDecodeError:return data.decode("cp1252",errors="replace")

def meta_event(kind:int,payload:bytes)->bytes:
    return bytes((0xff,kind))+_vlq(len(payload))+payload

def encode_track(events:list[tuple[int,int,bytes]],end_tick:int)->bytes:
    payload=bytearray();previous=0
    for tick,_,message in sorted(events,key=lambda item:(item[0],item[1])):
        if tick<previous or tick>end_tick:raise GenerationError("MIDI event falls outside the arrangement")
        payload.extend(_vlq(tick-previous));payload.extend(message);previous=tick
    payload.extend(_vlq(end_tick-previous));payload.extend(b"\xff\x2f\x00")
    return b"MTrk"+struct.pack(">I",len(payload))+payload

def song_midi(song:Song,tracks:list[Track]|None=None,gm_preview:bool=False)->bytes:
    tracks=song.tracks if tracks is None else tracks
    conductor_name=tracks[0].name if len(tracks)==1 else "Genre MIDI Studio / "+song.config.genre
    conductor=[(0,0,meta_event(3,conductor_name.encode("utf-8"))),
        (0,2,meta_event(0x58,bytes((4,2,24,8))))]
    for section in song.sections:
        conductor.append((section.start_bar*PPQ*4,3,meta_event(6,section.name.encode("utf-8"))))
    chunks=[encode_track(conductor,song.ticks)]
    for track in tracks:
        channel=9 if gm_preview and track.role=="kick" else track.channel-1
        # Hosts differ in which label they display: retain the original lane
        # name in both standard track-name and instrument-name metadata.
        encoded_name=track.name.encode("utf-8")
        events=[(0,0,meta_event(3,encoded_name)),(0,0,meta_event(4,encoded_name)),(0,0,meta_event(0x20,bytes((channel,))))]
        for cc,value in track.controls:events.append((0,1,bytes((0xb0|channel,cc,value))))
        if track.program is not None and (gm_preview or song.config.sound_set) and not (gm_preview and track.role=="kick"):
            events.append((0,2,bytes((0xc0|channel,track.program))))
        for note in track.notes:
            pitch=note.pitch;event_channel=channel
            if gm_preview and track.role=="kick":pitch=36;event_channel=9
            events.append((note.start,5,bytes((0x90|event_channel,pitch,note.velocity))))
            events.append((note.end,4,bytes((0x80|event_channel,pitch,0))))
        chunks.append(encode_track(events,song.ticks))
    if len(chunks)>257:raise GenerationError("Too many output MIDI tracks")
    return b"MThd"+struct.pack(">IHHH",6,1,len(chunks),PPQ)+b"".join(chunks)

def read_summary(data:bytes)->dict:
    doc=parse_midi(data)
    note_counts=[]
    for events in doc.tracks:
        note_counts.append(sum(e.status&0xf0==0x90 and len(e.data)==2 and e.data[1]>0 for e in events))
    return {"format":doc.format,"ppq":doc.ppq,"tracks":len(doc.tracks),"note_counts":note_counts,
        "end_tick":max((e.tick for t in doc.tracks for e in t),default=0)}
