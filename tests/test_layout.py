"""Compatibilidade dos caminhos antigos com os arquivos organizados em data/."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ck2translator.config import ConfigError, load_config
from ck2translator.glossario import main as suggest_glossary
from ck2translator.termos import collect


class LayoutCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.terms = self.root / "data" / "terms"
        self.terms.mkdir(parents=True)

    def test_default_terms_prefer_legacy_file_when_both_exist(self):
        legacy = self.root / "keep.txt"
        organized = self.terms / "keep.txt"
        legacy.write_text("Legacy\n", encoding="utf-8")
        organized.write_text("Organized\n", encoding="utf-8")
        self.assertEqual(collect(self.root, None, "keep.txt"), [legacy])

    def test_default_terms_use_data_relative_to_configuration(self):
        organized = self.terms / "keep.txt"
        organized.write_text("Organized\n", encoding="utf-8")
        self.assertEqual(collect(self.root, None, "keep.txt"), [organized])
        external = self.root / "external"
        external.mkdir()
        self.assertEqual(collect(external, None, "keep.txt"), [])

    def test_explicit_empty_terms_remain_disabled(self):
        (self.terms / "keep.txt").write_text("Organized\n", encoding="utf-8")
        self.assertEqual(collect(self.root, "", "keep.txt"), [])
        self.assertEqual(collect(self.root, [], "keep.txt"), [])

    def test_explicit_missing_terms_do_not_fall_back(self):
        (self.terms / "keep.txt").write_text("Organized\n", encoding="utf-8")
        config = self.root / "config.toml"
        config.write_text(
            "[translate]\nkeep_file='missing.txt'\n[[mods]]\nname='Example'\npath='mod'\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "missing.txt"):
            load_config(str(config))

    def test_config_loads_organized_terms_without_changing_legacy_cache_default(self):
        (self.terms / "keep.txt").write_text("Winterfell\n", encoding="utf-8")
        (self.terms / "glossary.txt").write_text("King = Rei\n", encoding="utf-8")
        (self.terms / "substituicoes.txt").write_text("partilhar = compartilhar\n", encoding="utf-8")
        config = self.root / "config.toml"
        config.write_text("[[mods]]\nname='Example'\npath='mod'\n", encoding="utf-8")
        with contextlib.chdir(self.root.parent):
            loaded = load_config(str(config))
        self.assertEqual(loaded["keep_terms"], ["Winterfell"])
        self.assertEqual(loaded["glossary_terms"], {"King": "Rei"})
        self.assertEqual(loaded["replacements"], {"partilhar": "compartilhar"})
        self.assertEqual(loaded["translate"]["cache"], self.root / "cache.sqlite")

    def make_cache(self, path, rows=1):
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as database:
            database.execute("CREATE TABLE cache (src TEXT, dst TEXT, model TEXT)")
            database.executemany(
                "INSERT INTO cache VALUES (?, ?, ?)",
                [(f"A Lord rules kingdom {number}", f"Um Senhor governa reino {number}", "argos")
                 for number in range(rows)],
            )
            database.commit()

    def run_glossary(self, directory):
        output = io.StringIO()
        with contextlib.chdir(directory), contextlib.redirect_stdout(output):
            code = suggest_glossary(["--min", "1"])
        self.assertEqual(code, 0, output.getvalue())
        return output.getvalue()

    def test_glossary_uses_organized_cache_in_current_directory_without_writing_database(self):
        directory = self.root / "Traduções # local"
        cache = directory / "data" / "cache" / "cache.sqlite"
        self.make_cache(cache)
        original = cache.read_bytes()
        message = self.run_glossary(directory)
        self.assertIn("1 traduções lidas do cache", message)
        self.assertEqual(cache.read_bytes(), original)
        self.assertFalse((directory / "cache.sqlite").exists())
        self.assertTrue((directory / "saida" / "keep_sugerido.txt").is_file())
        self.assertTrue((directory / "saida" / "glossary_sugerido.txt").is_file())

    def test_glossary_prefers_legacy_cache_when_both_exist(self):
        legacy = self.root / "cache.sqlite"
        organized = self.root / "data" / "cache" / "cache.sqlite"
        self.make_cache(legacy, rows=1)
        self.make_cache(organized, rows=2)
        message = self.run_glossary(self.root)
        self.assertIn("1 traduções lidas do cache", message)
        self.assertNotIn("2 traduções lidas do cache", message)


if __name__ == "__main__":
    unittest.main()
