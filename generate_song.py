"""Compose complete song packs or fill a MIDI sound-set template. Python 3.10+."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from genre_midi import __version__
from genre_midi.registry import get_profile
from genre_midi.model import Config, GenerationError, ROOTS, SCALES, ROLES
from genre_midi.service import available_genres, inspect_sound_set, create_song, export_song, load_recipe


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version="Genre MIDI Studio " + __version__)
    p.add_argument("--list-genres", action="store_true", help="List installed genres and styles, then exit")
    p.add_argument("--inspect-sound-set", type=Path, metavar="MIDI", help="Print lane IDs and suggested roles without generating")
    p.add_argument("--recipe", type=Path, help="Load a saved recipe; explicit options below override its values")
    p.add_argument("--output", type=Path, help="New folder for the complete song pack (never overwrites an existing folder)")
    p.add_argument("--genre")
    p.add_argument("--style")
    p.add_argument("--key", choices=tuple(ROOTS))
    p.add_argument("--scale", choices=tuple(SCALES))
    p.add_argument("--bpm", type=float)
    p.add_argument("--arrangement", help="Preset from the selected genre (full, compact, drop or hook); clears custom recipe sections")
    p.add_argument("--seed", type=int)
    p.add_argument("--energy", type=float, help="0 to 1")
    p.add_argument("--complexity", type=float, help="0 to 1")
    p.add_argument("--variation", type=float, help="0 to 1")
    p.add_argument("--humanize-ms", type=float, help="Percussion timing spread, 0 to 15 ms")
    p.add_argument("--kick-mode", choices=("pitched", "fixed"))
    source = p.add_mutually_exclusive_group()
    source.add_argument("--sound-set", type=Path, help="MIDI instrument-lane template to fill")
    source.add_argument("--no-sound-set", action="store_true", help="Clear a recipe's template and use built-in lanes")
    p.add_argument("--roles", type=Path, help='JSON role map such as {"track_2": "lead"}; IDs come from --inspect-sound-set')
    p.add_argument("--role", action="append", default=[], metavar="SLOT=ROLE", help="Override one lane role; repeatable. Roles: " + ", ".join(ROLES))
    p.add_argument("--no-automation", action="store_true", help="Export musical notes without the Motion FX control tracks")
    p.add_argument("--note-density", type=int, choices=(120,240,480,960), default=None,
                   help="Tiny automation notes per quarter beat (default: 240, or saved recipe density). Straight ramps, no merged notes.")
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.list_genres:
            print(json.dumps([{k: g[k] for k in ("id", "name", "version", "styles", "bpm", "scales")}
                              for g in available_genres()], indent=2))
            return 0
        if args.inspect_sound_set:
            info = inspect_sound_set(args.inspect_sound_set)
            info["suggested_role_map"] = {s["id"]: s["role"] for s in info["slots"]}
            print(json.dumps(info, indent=2, ensure_ascii=True))
            return 0
        if not args.output:
            p.error("--output is required when generating a song")
        config = load_recipe(args.recipe) if args.recipe else Config()
        if args.genre and (not args.recipe or args.genre != config.genre):
            profile = get_profile(args.genre)
            config.genre = args.genre
            config.style = next(iter(profile["styles"]))
            config.scale = profile["scales"][0]
            config.bpm = profile["bpm"]["default"]
            config.arrangement = "full"
            config.sections = None
        if args.style and args.bpm is None and (not args.recipe or args.genre):
            style = get_profile(config.genre)["styles"].get(args.style, {})
            config.bpm = style.get("bpm", {}).get("default", config.bpm)
        for name in ("genre", "style", "key", "scale", "bpm", "arrangement", "seed",
                     "energy", "complexity", "variation", "humanize_ms", "kick_mode"):
            value = getattr(args, name)
            if value is not None:
                setattr(config, name, value)
        if args.arrangement is not None:
            config.sections = None
        if args.no_sound_set:
            config.sound_set = None
            config.lane_roles = {}
        elif args.sound_set is not None:
            config.sound_set = str(args.sound_set.expanduser().resolve())
            config.lane_roles = {}
        if args.roles:
            if args.roles.stat().st_size > 2 * 1024 * 1024:
                raise GenerationError("Role map exceeds 2 MB")
            roles = json.loads(args.roles.read_text(encoding="utf-8-sig"))
            if not isinstance(roles, dict):
                raise GenerationError("Role map must be a JSON object mapping lane IDs to roles")
            config.lane_roles.update(roles)
        for item in args.role:
            slot, separator, role = item.partition("=")
            if not separator or not slot.strip() or role.strip() not in ROLES:
                raise GenerationError("Use --role SLOT=ROLE with a supported musical role")
            config.lane_roles[slot.strip()] = role.strip()
        song = create_song(config.validate())
        density = args.note_density
        if density is None and args.recipe:
            density = json.loads(args.recipe.read_text(encoding="utf-8-sig")).get("note_density", 240)
        result = export_song(song, args.output, include_automation=not args.no_automation,
                             note_density=240 if density is None else density)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (GenerationError, OSError, json.JSONDecodeError) as exc:
        print("Genre MIDI Studio: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
