"""Regressões de configuração, cache e geração dos mods de tradução."""
import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ck2translator import cli as ck2tr
from ck2translator.ck2loc import LocFile
from ck2translator.translator import (
    Cache, EchoBackend, GoogleBackend, RateLimiter, Translator, TranslatorsBackend, split_text,
)


class Improvements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.loc = self.root / "mod" / "localisation"
        self.loc.mkdir(parents=True)
        self.source = self.loc / "a.CSV"
        self.source.write_bytes(b"key;Hello $NAME$;Bonjour;Hallo;;Hola;;;;;;;;;x\r\n")
        self.config = self.root / "config.toml"
        self.configure()

    def configure(self, translate="", model="backend = 'echo'"):
        self.config.write_text(
            f"[model]\n{model}\n[translate]\n{translate}\n"
            "[[mods]]\nname = 'Meu Mod'\npath = 'mod'\n",
            encoding="utf-8",
        )

    def run_cli(self, command, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = ck2tr.main([command, "--config", str(self.config), *options])
        return code, output.getvalue()

    def test_config_errors_are_actionable(self):
        for settings in ("workers = 0", "workers = false", "write_columns = []",
                         "write_columns = 'english'", "skip_files = 'a.csv'",
                         "ptbr_fix = 'false'", "reuse_cache_from = [1]"):
            with self.subTest(settings=settings):
                self.configure(settings)
                code, message = self.run_cli("scan")
                self.assertEqual(code, 2)
                self.assertIn("ERRO", message)

    def test_invalid_toml_has_no_traceback(self):
        self.config.write_text("[model\n", encoding="utf-8")
        code, message = self.run_cli("scan")
        self.assertEqual(code, 2)
        self.assertIn("TOML", message)

    def test_status_needs_no_optional_backend_or_cache_file(self):
        self.configure(model="backend = 'translators'")
        with mock.patch.dict("sys.modules", {"translators": None}):
            code, message = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("faltam 1", message)
        self.assertFalse((self.root / "cache.sqlite").exists())

    def test_diff_checks_target_column_and_uppercase_csv(self):
        self.configure("write_columns = ['french']")
        self.assertEqual(self.run_cli("translate")[0], 0)
        code, message = self.run_cli("diff", "--find", "PT>")
        self.assertEqual(code, 0)
        self.assertIn("key", message)
        self.assertIn("french", message)

    def test_verify_fails_when_nothing_was_generated(self):
        self.assertEqual(self.run_cli("verify")[0], 1)

    def test_package_refuses_corrupted_translation(self):
        self.assertEqual(self.run_cli("translate")[0], 0)
        generated = self.root / "saida" / "meu_mod_ptbr" / "localisation" / "a.CSV"
        generated.write_bytes(generated.read_bytes().replace(b"$NAME$", b""))
        self.assertEqual(self.run_cli("package")[0], 1)
        self.assertFalse((self.root / "saida" / "meu_mod_ptbr.zip").exists())

    def test_missing_target_columns_do_not_change_csv_structure(self):
        self.source.write_bytes(b"key;Hello\r\n")
        self.configure("write_columns = ['spanish']")
        self.assertEqual(self.run_cli("translate")[0], 1)
        self.assertEqual(self.source.read_bytes(), b"key;Hello\r\n")
        report = self.root / "saida" / "meu_mod_relatorio.csv"
        self.assertIn("coluna", report.read_text(encoding="utf-8-sig"))

    def test_short_csv_does_not_overwrite_final_marker(self):
        self.source.write_bytes(b"key;Hello;;;;x\r\n")
        self.configure("write_columns = ['spanish']")
        self.assertEqual(self.run_cli("translate")[0], 1)
        generated = self.root / "saida" / "meu_mod_ptbr" / "localisation" / "a.CSV"
        self.assertFalse(generated.exists())

    def test_relative_descriptor_and_unicode_name(self):
        (self.root / "original.mod").write_text('name = "Tradução Épica"\n', encoding="utf-8-sig")
        self.config.write_text("[translate]\n[[mods]]\npath='mod'\nmod_file='original.mod'\n", encoding="utf-8")
        cfg = ck2tr.load_config(str(self.config))
        with mock.patch("os.getcwd", return_value=str(self.root.parent)):
            mods = ck2tr.resolve_mods(cfg, None)
        self.assertEqual(mods[0].name, "Tradução Épica")

    def test_limit_must_be_positive(self):
        with self.assertRaises(SystemExit) as result:
            self.run_cli("translate", "--limit", "0")
        self.assertEqual(result.exception.code, 2)

    def test_model_values_are_validated(self):
        for settings in ("timeout = 0", "pause_min = -1", "pause_min = 5\npause_max = 1",
                         "providers = []", "max_retries = 0", "max_chars = false"):
            with self.subTest(settings=settings):
                self.configure(model="backend = 'echo'\n" + settings)
                code, message = self.run_cli("scan")
                self.assertEqual(code, 2)
                self.assertIn("ERRO", message)

    def test_paranoid_rerun_resets_previous_overlay(self):
        self.assertEqual(self.run_cli("translate")[0], 0)
        self.configure("safety = 'paranoid'")
        self.assertEqual(self.run_cli("translate")[0], 0)
        generated = self.root / "saida" / "meu_mod_ptbr" / "localisation" / "a.CSV"
        self.assertEqual(generated.read_bytes(), self.source.read_bytes())

    def test_report_is_refreshed_after_successful_retry(self):
        backend = EchoBackend()
        with mock.patch.object(backend, "chat", return_value="Texto com [ solto"), \
                mock.patch.object(ck2tr, "build_backend", return_value=backend):
            self.assertEqual(self.run_cli("translate")[0], 1)
        report = self.root / "saida" / "meu_mod_relatorio.csv"
        self.assertIn("falha", report.read_text(encoding="utf-8-sig"))
        self.assertEqual(self.run_cli("translate")[0], 0)
        self.assertEqual(len(report.read_text(encoding="utf-8-sig").splitlines()), 1)

    def test_package_readme_uses_configured_language(self):
        self.configure("write_columns = ['french']")
        self.assertEqual(self.run_cli("translate")[0], 0)
        self.assertEqual(self.run_cli("package")[0], 0)
        with zipfile.ZipFile(self.root / "saida" / "meu_mod_ptbr.zip") as archive:
            readme = archive.read("LEIA-ME.txt").decode("utf-8")
        self.assertIn("FRANCÊS", readme)
        self.assertNotIn("INGLÊS", readme)

    def test_slug_collisions_are_rejected(self):
        self.config.write_text(self.config.read_text(encoding="utf-8")
                               + "[[mods]]\nname='Meu-Mod'\npath='mod'\n", encoding="utf-8")
        code, message = self.run_cli("scan")
        self.assertEqual(code, 2)
        self.assertIn("mesmo nome de saída", message)


class CacheAndSplitting(unittest.TestCase):
    def test_corrupted_current_backend_cache_is_retranslated(self):
        with contextlib.closing(Cache()) as cache:
            translator = Translator(EchoBackend(), cache)
            translator.translate("Hello $NAME$")
            cache.db.execute("UPDATE cache SET dst = ?", ("{99}",))
            result = translator.translate("Hello $NAME$")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.text, "PT> Hello $NAME$")

    def test_split_enforces_limit_without_punctuation(self):
        text = ("longword " * 100).strip()
        parts = split_text(text, 40)
        self.assertTrue(all(len(part) <= 40 for part in parts))
        self.assertEqual(" ".join(parts), text)

    def test_set_cell_refuses_to_add_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.csv"
            path.write_bytes(b"key;Hello\n")
            loc = LocFile.read(path)
            with self.assertRaises(ValueError):
                loc.set_cell(next(loc.entries()), 5, "Oi")

    def test_atomic_write_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.csv"
            path.write_bytes(b"key;Hello\n")
            loc = LocFile.read(path)
            loc.set_cell(next(loc.entries()), 1, "Oi")
            with mock.patch("ck2translator.files.os.replace", side_effect=PermissionError("ocupado")):
                with self.assertRaises(PermissionError):
                    loc.write(path)
            self.assertEqual(path.read_bytes(), b"key;Hello\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_split_does_not_break_protected_markers(self):
        text = "a" * 35 + "XX123XX" + "b" * 35
        parts = split_text(text, 40)
        self.assertTrue(all(len(part) <= 40 for part in parts))
        self.assertIn("XX123XX", " ".join(parts))

    def test_nested_cache_and_read_only_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache" / "translations.sqlite"
            with Cache(str(path)) as cache:
                cache.put("key", "Hello", "Oi", "test")
            before = path.read_bytes()
            with Cache(str(path), read_only=True) as cache:
                self.assertEqual(cache.get("key"), "Oi")
            self.assertEqual(path.read_bytes(), before)

    def test_google_long_pause_can_be_disabled(self):
        backend = GoogleBackend(pause=(0, 0), long_every=0, translate_fn=lambda text: text)
        self.assertEqual(backend.chat("", "Hello"), "Hello")

    def test_segment_fallback_respects_provider_limit(self):
        calls = []

        def translate(text, *args):
            calls.append(text)
            return text.replace("XX1XX", "")

        limiter = RateLimiter(0, 0, (0, 0), 0, sleep=lambda seconds: None)
        backend = TranslatorsBackend(["google"], limiter=limiter, max_chars=40,
                                     translate_fn=translate, sleep=lambda seconds: None)
        text = ("word " * 30) + "{1}"
        translated = backend.chat("", "<texto>\n" + text + "\n</texto>")
        self.assertIn("{1}", translated)
        self.assertTrue(all(len(call) <= 40 for call in calls))

