"""Regressões da revisão de verificação e comandos dos dois jogos."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ck2translator import cli


class CliReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def configure(self, game):
        directory = self.root / game
        localisation = directory / "original" / "localisation"
        localisation.mkdir(parents=True)
        files = ["a.csv", "b.csv"] if game == "ck2" else ["a_l_english.yml", "replace/b_l_english.yml"]
        for index, name in enumerate(files):
            path = localisation / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = (f'key{index};Hello {index};Bonjour;Hallo;;Hola;;;;;;;;;x\r\n' if game == "ck2"
                       else f'l_english:\n key{index}:0 "Hello {index}"\n')
            path.write_bytes(content.encode("cp1252" if game == "ck2" else "utf-8-sig"))
        config = directory / "config.toml"
        config.write_text(
            f"[game]\nid='{game}'\n[model]\nbackend='echo'\n"
            "[translate]\noutput_dir='out'\nptbr_fix=false\n"
            "[[mods]]\nname='Example'\npath='original'\n", encoding="utf-8",
        )
        self.config = config
        self.generated = directory / "out" / "example_ptbr"
        self.files = files

    def run_cli(self, command, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cli.main([command, "--config", str(self.config), *options])
        return code, output.getvalue()

    def translate(self, *options):
        code, message = self.run_cli("translate", *options)
        self.assertEqual(code, 0, message)

    def test_check_alias_matches_verify_for_both_games_without_a_backend(self):
        for game in ("ck2", "eu4"):
            with self.subTest(game=game):
                self.configure(game)
                self.translate()
                with mock.patch.object(cli, "build_backend", side_effect=AssertionError("backend acessado")):
                    checked = self.run_cli("check", "--mode", "overlay")
                    verified = self.run_cli("verify", "--mode", "overlay")
                self.assertEqual(checked, verified)
                self.assertEqual(checked[0], 0, checked[1])

    def test_deleted_output_is_detected_and_package_refused_for_both_games(self):
        for game in ("ck2", "eu4"):
            with self.subTest(game=game):
                self.configure(game)
                self.translate()
                missing = self.generated / "localisation" / self.files[1]
                missing.unlink()
                for command in ("verify", "check", "package"):
                    code, message = self.run_cli(command)
                    self.assertEqual(code, 1, message)
                    self.assertIn("FALTA:", message)
                    self.assertIn(missing.name, message)
                self.assertFalse(self.generated.with_suffix(".zip").exists())

    def test_partial_translation_does_not_require_unwritten_files(self):
        self.configure("eu4")
        self.translate("--limit", "1")
        self.assertFalse((self.generated / "localisation" / self.files[1]).exists())
        self.assertEqual(self.run_cli("check")[0], 0)

    def test_partial_retry_keeps_missing_file_in_manifest_until_regenerated(self):
        self.configure("eu4")
        self.translate()
        missing = self.generated / "localisation" / self.files[1]
        missing.unlink()
        self.translate("--limit", "1")
        self.assertEqual(self.run_cli("check")[0], 1)
        self.translate()
        self.assertTrue(missing.exists())
        self.assertEqual(self.run_cli("check")[0], 0)

    def test_old_overlays_without_manifest_remain_supported(self):
        self.configure("eu4")
        self.translate()
        (self.generated / "translation_manifest.json").unlink()
        self.assertEqual(self.run_cli("check")[0], 0)
        self.assertEqual(self.run_cli("package")[0], 0)

    def test_invalid_manifest_cannot_reference_paths_outside_localisation(self):
        self.configure("eu4")
        self.translate()
        manifest = self.generated / "translation_manifest.json"
        for files in (["../outside.yml"], ["C:/outside.yml"], ["replace\\a.yml"], [123]):
            with self.subTest(files=files):
                manifest.write_text(json.dumps({"version": 1, "game": "eu4", "files": files}), encoding="utf-8")
                code, message = self.run_cli("check")
                self.assertEqual(code, 2, message)
                self.assertIn("Manifesto inválido", message)
                self.assertNotIn("Traceback", message)

    def test_echo_explicitly_reports_that_it_does_not_translate(self):
        self.configure("eu4")
        code, message = self.run_cli("translate")
        self.assertEqual(code, 0, message)
        self.assertIn("não realiza tradução", message)

    def test_invalid_manifest_aborts_before_backend_cache_or_output_changes(self):
        self.configure("eu4")
        self.translate()
        manifest = self.generated / "translation_manifest.json"
        outputs = {path: path.read_bytes() for path in self.generated.rglob("*")
                   if path.is_file() and path != manifest}
        cache = self.config.parent / "cache.sqlite"
        cached = cache.read_bytes()
        source = self.config.parent / "original" / "localisation" / self.files[0]
        source.write_bytes(source.read_bytes().replace(b"Hello", b"Changed"))
        for invalid in ('{"broken":', json.dumps({"version": 1, "game": "ck2", "files": []})):
            with self.subTest(manifest=invalid):
                manifest.write_text(invalid, encoding="utf-8")
                with mock.patch.object(cli, "build_backend", side_effect=AssertionError("backend acessado")):
                    code, message = self.run_cli("translate")
                self.assertEqual(code, 2, message)
                self.assertIn("Manifesto inválido", message)
                self.assertEqual({path: path.read_bytes() for path in outputs}, outputs)
                self.assertEqual(cache.read_bytes(), cached)


if __name__ == "__main__":
    unittest.main()
