"""Explicit composer registration plus versioned, data-only genre profiles."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
from typing import Callable
from .model import Config,Song,GenerationError

PROFILE_DIR=Path(__file__).resolve().parent.parent/"profiles"
_ENGINES:dict[str,Callable[[Config,dict],Song]]={}

def register_engine(name:str,composer:Callable[[Config,dict],Song])->None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]*",name) or not callable(composer):raise GenerationError("Invalid genre engine registration")
    if name in _ENGINES:raise GenerationError(f"Genre engine is already registered: {name}")
    _ENGINES[name]=composer

def _builtins()->None:
    if "hardstyle" not in _ENGINES:
        from .genres.hardstyle import compose
        register_engine("hardstyle",compose)
    from .genres import pop, rap, edm
    for name, module in (("pop", pop), ("rap", rap), ("edm", edm)):
        if name not in _ENGINES:
            register_engine(name, module.compose)

def validate_profile(profile:dict)->dict:
    if not isinstance(profile,dict) or type(profile.get("schema_version")) is not int or profile["schema_version"]!=1:raise GenerationError("Unsupported genre profile schema version")
    for field in ("id","name","version","engine"):
        if not isinstance(profile.get(field),str) or not profile[field]:raise GenerationError(f"Genre profile needs {field}")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*",profile["id"]):raise GenerationError("Invalid genre ID")
    if not isinstance(profile.get("styles"),dict) or not profile["styles"]:raise GenerationError("Genre profile has no styles")
    if profile.get("meter",[4,4])!=[4,4]:raise GenerationError("This engine version supports 4/4 genre profiles")
    bpm=profile.get("bpm",{})
    try:
        if not 20<=bpm["min"]<=bpm["default"]<=bpm["max"]<=400:raise ValueError()
    except (KeyError,TypeError,ValueError):raise GenerationError("Genre tempo range is invalid") from None
    if not isinstance(profile.get("scales"),list) or not profile["scales"]:raise GenerationError("Genre profile has no scales")
    return profile


def profile_fingerprint(profile:dict)->str:
    """Hash profile contents independently of JSON whitespace or key order."""
    try:
        encoded=json.dumps(profile,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")
    except (TypeError,ValueError) as exc:
        raise GenerationError("Genre profile must contain finite JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()

def available_genres()->list[dict]:
    _builtins();profiles=[];ids=set()
    for path in sorted(PROFILE_DIR.glob("*.json")):
        try:profile=validate_profile(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError,json.JSONDecodeError) as exc:raise GenerationError(f"Cannot read genre profile {path.name}: {exc}") from exc
        if profile["id"] in ids:raise GenerationError("Duplicate genre profile ID: "+profile["id"])
        if profile["engine"] not in _ENGINES:raise GenerationError("Genre profile uses an unregistered engine: "+profile["engine"])
        ids.add(profile["id"]);profiles.append(profile)
    return profiles

def get_profile(genre:str)->dict:
    for profile in available_genres():
        if profile["id"]==genre:return profile
    raise GenerationError("Unknown genre: "+genre)

def compose_song(config:Config)->Song:
    profile=get_profile(config.genre);config.validate(profile)
    if config.style not in profile["styles"]:raise GenerationError("Unknown style for "+profile["name"]+": "+config.style)
    if config.scale not in profile["scales"]:raise GenerationError("This genre profile does not support the chosen scale")
    if not profile["bpm"]["min"]<=config.bpm<=profile["bpm"]["max"]:raise GenerationError(f"{profile['name']} tempo must be {profile['bpm']['min']}–{profile['bpm']['max']} BPM")
    song=_ENGINES[profile["engine"]](config,profile)
    song.metadata["profile_sha256"]=profile_fingerprint(profile)
    return song
