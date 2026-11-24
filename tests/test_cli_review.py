"""Regressões da revisão de verificação e comandos dos dois jogos."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ck2translator import cli
from ck2translator.translator import Cache, EchoBackend, Translator


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

    def test_interruption_keeps_the_first_overlay_file_and_resume_skips_it(self):
        for game in ("ck2", "eu4"):
            with self.subTest(game=game):
                self.configure(game)
                first, second = (self.generated / "localisation" / name for name in self.files)
                original_chat = EchoBackend.chat

                def interrupt(backend, system, user):
                    if "Hello 1" in user:
                        self.assertTrue(first.is_file())
                        raise KeyboardInterrupt()
                    return original_chat(backend, system, user)

                with mock.patch.object(EchoBackend, "chat", new=interrupt):
                    self.assertEqual(self.run_cli("translate")[0], 130)
                self.assertFalse(second.exists())
                manifest = json.loads((self.generated / "translation_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(set(manifest["completed"]), {self.files[0]})

                original_translate = Translator.translate
                processed = []

                def record(translator, text, **options):
                    processed.append(text)
                    return original_translate(translator, text, **options)

                with mock.patch.object(Translator, "translate", new=record):
                    self.translate()
                self.assertEqual(processed, ["Hello 1"])
                self.assertTrue(second.is_file())
                self.assertEqual(self.run_cli("check")[0], 0)
                with mock.patch.object(Cache, "models", side_effect=AssertionError("cache consultado")), \
                        mock.patch.object(Translator, "translate", side_effect=AssertionError("texto processado")):
                    self.translate()

    def test_modified_origin_and_destination_invalidate_only_affected_files(self):
        self.configure("eu4")
        self.translate()
        first, second = (self.generated / "localisation" / name for name in self.files)
        source = self.config.parent / "original" / "localisation" / self.files[0]
        source.write_bytes(source.read_bytes().replace(b"Hello 0", b"Revised 0"))
        second.write_bytes(second.read_bytes().replace(b"PT> Hello 1", b"Edited output"))
        calls = []
        original_chat = EchoBackend.chat

        def record(backend, system, user):
            calls.append(user)
            return original_chat(backend, system, user)

        with mock.patch.object(EchoBackend, "chat", new=record):
            self.translate()
        self.assertEqual(len(calls), 1)
        self.assertIn("Revised 0", calls[0])
        self.assertIn(b"PT> Revised 0", first.read_bytes())
        self.assertIn(b"PT> Hello 1", second.read_bytes())

    def test_revision_change_rebuilds_an_existing_file_from_the_cache(self):
        self.configure("eu4")
        self.translate()
        first = self.generated / "localisation" / self.files[0]
        replacements = self.config.parent / "substituicoes.txt"
        replacements.write_text("Hello = Olá\n", encoding="utf-8")
        with mock.patch.object(EchoBackend, "chat", side_effect=AssertionError("backend chamado")):
            self.translate()
        self.assertIn("PT> Olá 0", first.read_text(encoding="utf-8-sig"))

    def test_limit_counts_unique_texts_across_files_and_never_completes_partial_output(self):
        self.configure("eu4")
        source = self.config.parent / "original" / "localisation" / self.files[1]
        with source.open("a", encoding="utf-8") as stream:
            stream.write(' shared:0 "Hello 0"\n')
        self.translate()
        self.translate("--limit", "1")
        second = self.generated / "localisation" / self.files[1]
        self.assertIn(b'"Hello 1"', second.read_bytes())
        self.assertIn(b'"PT> Hello 0"', second.read_bytes())
        manifest = json.loads((self.generated / "translation_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["completed"], {})
        self.translate()
        self.assertIn(b'"PT> Hello 1"', second.read_bytes())

    def test_failed_write_leaves_only_the_published_file_completed(self):
        self.configure("eu4")
        second = self.generated / "localisation" / self.files[1]
        atomic_write = cli.atomic_write

        def fail_second(path, data):
            if path == second:
                raise OSError("falha simulada na gravação")
            return atomic_write(path, data)

        with mock.patch.object(cli, "atomic_write", side_effect=fail_second):
            self.assertEqual(self.run_cli("translate")[0], 2)
        manifest = json.loads((self.generated / "translation_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"], [self.files[0]])
        self.assertEqual(list(manifest["completed"]), [self.files[0]])
        self.assertFalse(second.exists())
        with mock.patch.object(EchoBackend, "chat", side_effect=AssertionError("backend chamado")):
            self.translate()
        self.assertTrue(second.is_file())
        self.assertEqual(self.run_cli("check")[0], 0)


if __name__ == "__main__":
    unittest.main()
