"""Public CLI workflows, including recipes and sound-set role overrides."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generate_song import main
from genre_midi import __version__
from genre_midi.model import Config
from genre_midi.registry import get_profile, profile_fingerprint


def _sound_set() -> bytes:
    """Independent small MIDI fixture with one named and one ambiguous lane."""
    def chunk(payload):
        return b"MTrk" + struct.pack(">I", len(payload)) + payload
    conductor = chunk(b"\x00\xff\x03\x09Conductor\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x2f\x00")
    lanes = []
    for name, channel, pitch in ((b"Lead - primary", 0, 64), (b"Layer 2", 1, 67)):
        payload = b"\x00\xff\x03" + bytes((len(name),)) + name
        payload += bytes((0, 0xc0 | channel, 81, 0, 0x90 | channel, pitch, 100))
        payload += bytes((120, 0x80 | channel, pitch, 0)) + b"\x00\xff\x2f\x00"
        lanes.append(chunk(payload))
    return b"MThd" + struct.pack(">IHHH", 6, 1, 3, 960) + conductor + b"".join(lanes)


class CliTests(unittest.TestCase):
    def setUp(self):
        # Ordinary mkdir inherits usable workspace ACLs under restricted Windows
        # tokens; Python 3.14's owner-only TemporaryDirectory does not.
        self.temporary_parent = (ROOT / "Tests" / "_cli_test_output").resolve()
        self.temporary_parent.mkdir(exist_ok=True)
        self.folder = self.temporary_parent / uuid.uuid4().hex
        self.folder.mkdir()

    def tearDown(self):
        resolved = self.folder.resolve()
        if resolved.parent != self.temporary_parent or len(resolved.name) != 32:
            raise RuntimeError("Refusing to clean an unexpected test path")
        shutil.rmtree(resolved)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(arg) for arg in args])
        return code, stdout.getvalue(), stderr.getvalue()

    def write_sound_set(self):
        path = self.folder / "sound set.mid"
        path.write_bytes(_sound_set())
        return path

    def test_list_genres_returns_registered_styles_without_generating(self):
        code, stdout, stderr = self.invoke("--list-genres")
        self.assertEqual((code, stderr), (0, ""))
        profiles = json.loads(stdout)
        hardstyle = next(profile for profile in profiles if profile["id"] == "hardstyle")
        self.assertEqual(set(hardstyle["styles"]), {"euphoric", "raw", "classic"})
        self.assertLessEqual(hardstyle["bpm"]["min"], hardstyle["bpm"]["default"])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_inspection_preserves_unknown_role_and_never_changes_source(self):
        source = self.write_sound_set()
        code, stdout, stderr = self.invoke("--inspect-sound-set", source)
        self.assertEqual((code, stderr), (0, ""))
        inspected = json.loads(stdout)
        self.assertEqual([slot["name"] for slot in inspected["slots"]], ["Lead - primary", "Layer 2"])
        self.assertEqual(inspected["suggested_role_map"], {"track_1": "lead", "track_2": "unassigned"})
        self.assertEqual([slot["channel"] for slot in inspected["slots"]], [1, 2])
        self.assertEqual(source.read_bytes(), _sound_set())
        self.assertEqual(list(self.folder.iterdir()), [source])

    def test_full_recipe_with_cli_overrides_exports_notes_only(self):
        source = self.write_sound_set()
        profile = get_profile("hardstyle")
        config = Config(seed=11, style="euphoric", arrangement="compact", sound_set=source.name,
                        lane_roles={"track_2": "pad"}, sections=[{"kind": "drop", "bars": 8}])
        recipe = self.folder / "recipe.json"
        recipe.write_text(json.dumps({"schema_version": 1, "generator_version": __version__,
                                      "profile_version": profile["version"], "profile_sha256": profile_fingerprint(profile),
                                      "config": config.to_dict()}), encoding="utf-8")
        roles = self.folder / "roles.json"
        roles.write_text(json.dumps({"track_2": "bass"}), encoding="utf-8")
        output = self.folder / "New Full Song"
        code, stdout, stderr = self.invoke("--recipe", recipe, "--output", output, "--arrangement", "full",
                                          "--seed", "303", "--style", "raw", "--bpm", "160", "--roles", roles,
                                          "--role", "track_2=lead", "--no-automation")
        self.assertEqual((code, stderr), (0, ""))
        result = json.loads(stdout)
        self.assertEqual(result["total_bars"], 128)
        self.assertEqual(len(result["tracks"]), 2)
        self.assertEqual(result["automation"], [])
        self.assertFalse((output / "automation").exists())
        self.assertTrue((output / "song.mid").is_file())
        exported = json.loads((output / "recipe.json").read_text(encoding="utf-8"))
        values = exported["config"]
        self.assertEqual((values["arrangement"], values["seed"], values["style"], values["bpm"]), ("full", 303, "raw", 160))
        self.assertIsNone(values["sections"])
        self.assertEqual(values["lane_roles"]["track_2"], "lead")
        self.assertEqual(exported["profile_sha256"], profile_fingerprint(profile))
        self.assertEqual(source.read_bytes(), _sound_set())

    def test_invalid_role_and_existing_output_return_friendly_exit_two(self):
        source = self.write_sound_set()
        failed = self.folder / "bad-role"
        code, stdout, stderr = self.invoke("--output", failed, "--role", "track_2=unknown-role")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("Genre MIDI Studio: Use --role", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertFalse(failed.exists())

        code, stdout, stderr = self.invoke("--output", failed, "--sound-set", source)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("Assign a role", stderr)
        self.assertIn("Layer 2", stderr)
        self.assertFalse(failed.exists())

        existing = self.folder / "existing"
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        code, stdout, stderr = self.invoke("--output", existing, "--arrangement", "drop", "--no-automation")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("Genre MIDI Studio:", stderr)
        self.assertIn("already exists", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(existing.iterdir()), [sentinel])

    def test_import_does_not_run_cli_or_create_output_files(self):
        script = "import sys; sys.path.insert(0, sys.argv[1]); import generate_song"
        result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT)], cwd=self.folder,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(list(self.folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
