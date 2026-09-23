"""Shared data contracts; no generation or filesystem work at import time."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import hashlib
import math
import random
from typing import Any

PPQ = 960
BEATS_PER_BAR = 4
BAR = PPQ * BEATS_PER_BAR
ROOTS = {"C":0,"C#":1,"Db":1,"D":2,"D#":3,"Eb":3,"E":4,"F":5,"F#":6,"Gb":6,"G":7,"G#":8,"Ab":8,"A":9,"A#":10,"Bb":10,"B":11}
SCALES = {"minor":(0,2,3,5,7,8,10),"harmonic-minor":(0,2,3,5,7,8,11),"major":(0,2,4,5,7,9,11),"dorian":(0,2,3,5,7,9,10),"phrygian":(0,1,3,5,7,8,10)}
SECTION_KINDS = ("intro","build","drop","breakdown","outro","verse","chorus","prechorus","bridge","hook","break","drop2")
ROLES = ("kick","bass","lead","chords","pad","arp","screech","fx","snare","clap","closed_hat","open_hat","cymbal","percussion","skip","vocal","sub","pluck","stab","riser")

class GenerationError(ValueError):
    pass

@dataclass
class Config:
    genre: str = "hardstyle"
    style: str = "euphoric"
    key: str = "F#"
    scale: str = "minor"
    bpm: float = 150
    arrangement: str = "full"
    seed: int = 2026
    energy: float = 0.75
    complexity: float = 0.6
    variation: float = 0.45
    kick_mode: str = "pitched"
    humanize_ms: float = 3.0
    sections: list[dict[str, Any]] | None = None
    sound_set: str | None = None
    lane_roles: dict[str,str] = field(default_factory=dict)

    def validate(self, profile: dict | None = None) -> "Config":
        if not isinstance(self.genre,str) or not self.genre: raise GenerationError("Choose a genre")
        if not isinstance(self.style,str) or not self.style: raise GenerationError("Choose a style")
        if not isinstance(self.key,str) or self.key not in ROOTS: raise GenerationError("Key must be a note name such as F#, A or Bb")
        if not isinstance(self.scale,str) or self.scale not in SCALES: raise GenerationError("Scale must be one of: " + ", ".join(SCALES))
        if profile is None:
            from .registry import get_profile
            profile = get_profile(self.genre)
        arrangements = profile.get("arrangements", ("full", "compact", "drop"))
        if not isinstance(self.arrangement, str) or self.arrangement not in arrangements:
            raise GenerationError("Arrangement must be one of: " + ", ".join(arrangements))
        if self.kick_mode not in ("pitched","fixed"): raise GenerationError("Kick mode must be pitched or fixed")
        if self.sound_set is not None and (not isinstance(self.sound_set,str) or not self.sound_set.strip()): raise GenerationError("Sound set must be a MIDI file path")
        if not isinstance(self.lane_roles,dict) or any(not isinstance(k,str) or v not in ROLES for k,v in self.lane_roles.items()): raise GenerationError("Lane roles must map slot IDs to supported roles")
        if isinstance(self.seed,bool) or not isinstance(self.seed,int) or not 0<=self.seed<=4294967295: raise GenerationError("Seed must be an integer from 0 to 4294967295")
        for name,lo,hi in (("bpm",20,400),("energy",0,1),("complexity",0,1),("variation",0,1),("humanize_ms",0,15)):
            v=getattr(self,name)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not lo<=v<=hi: raise GenerationError(f"{name} must be between {lo} and {hi}")
        if self.sections is not None:
            if not isinstance(self.sections,list) or not 1<=len(self.sections)<=64: raise GenerationError("Custom sections must be a list of 1 to 64 sections")
            total=0
            for section in self.sections:
                if not isinstance(section,dict) or set(section)-{"kind","bars","name","energy"}: raise GenerationError("Each section accepts kind, bars, optional name and energy")
                if section.get("kind") not in SECTION_KINDS: raise GenerationError("Unknown section kind")
                bars=section.get("bars")
                if isinstance(bars,bool) or not isinstance(bars,int) or not 1<=bars<=128: raise GenerationError("Section bars must be an integer from 1 to 128")
                if "name" in section and (not isinstance(section["name"],str) or len(section["name"])>100): raise GenerationError("Section name must be text up to 100 characters")
                if "energy" in section:
                    value=section["energy"]
                    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1: raise GenerationError("Section energy must be between 0 and 1")
                total+=bars
            if total>512: raise GenerationError("A song may contain at most 512 bars")
        return self

    def to_dict(self) -> dict[str, Any]: return asdict(self)

    @classmethod
    def from_dict(cls,data:dict[str,Any]) -> "Config":
        if not isinstance(data,dict): raise GenerationError("Config must be a JSON object")
        allowed=set(cls.__dataclass_fields__)
        unknown=set(data)-allowed
        if unknown: raise GenerationError("Unknown config fields: "+", ".join(sorted(unknown)))
        return cls(**data).validate()

@dataclass(frozen=True)
class Note:
    start: int
    duration: int
    pitch: int
    velocity: int

    @property
    def end(self) -> int: return self.start+self.duration

@dataclass
class Track:
    id: str
    name: str
    role: str
    channel: int
    program: int | None = None
    notes: list[Note] = field(default_factory=list)
    controls: list[tuple[int,int]] = field(default_factory=list)

@dataclass(frozen=True)
class Section:
    kind: str
    name: str
    start_bar: int
    bars: int
    energy: float

    @property
    def end_bar(self)->int: return self.start_bar+self.bars

@dataclass(frozen=True)
class Chord:
    bar: int
    root: int
    pitches: tuple[int,...]
    name: str

@dataclass
class Song:
    config: Config
    sections: list[Section]
    tracks: list[Track]
    chords: list[Chord]
    profile_version: str = "1.0.0"
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def bars(self)->int: return self.sections[-1].end_bar if self.sections else 0
    @property
    def ticks(self)->int: return self.bars*BAR

def stable_rng(seed:int,*parts:object)->random.Random:
    text="|".join([str(seed),*(str(p) for p in parts)])
    return random.Random(int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:16],"big"))
