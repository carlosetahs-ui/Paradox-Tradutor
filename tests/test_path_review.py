"""Regressões de proteção dos mods originais e leitura do cache."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ck2translator.config import ConfigError, load_config, resolve_mods
from ck2translator.glossario import main as glossary_main


class PathReviewTests(unittest.TestCase):
    def test_filtered_originals_are_protected_from_overlay_writes(self):
        for game in ("ck2", "eu4"):
            for suffix in ("", "/nested"):
                with self.subTest(game=game, suffix=suffix), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    (root / "alpha" / "localisation").mkdir(parents=True)
                    beta = root / ("out/alpha_ptbr" + suffix)
                    (beta / "localisation").mkdir(parents=True)
                    config = root / "config.toml"
                    config.write_text(
                        f"[game]\nid='{game}'\n[translate]\noutput_dir='out'\n"
                        "[[mods]]\nname='Alpha'\npath='alpha'\n"
                        f"[[mods]]\nname='Beta'\npath='out/alpha_ptbr{suffix}'\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ConfigError, "sobrepõe"):
                        resolve_mods(load_config(str(config)), "Alpha")

    def test_filter_still_selects_independent_mods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("alpha", "beta"):
                (root / name / "localisation").mkdir(parents=True)
            config = root / "config.toml"
            config.write_text(
                "[[mods]]\nname='Alpha'\npath='alpha'\n"
                "[[mods]]\nname='Beta'\npath='beta'\n", encoding="utf-8",
            )
            self.assertEqual([mod.name for mod in resolve_mods(load_config(str(config)), "Alpha")], ["Alpha"])

    def test_corrupted_or_incompatible_cache_reports_error_without_writes(self):
        for kind in ("corrupt", "missing_table"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "translations.sqlite"
                if kind == "corrupt":
                    cache.write_bytes(b"this is not a SQLite database")
                else:
                    with contextlib.closing(sqlite3.connect(cache)) as database:
                        database.execute("CREATE TABLE unrelated (value TEXT)")
                        database.commit()
                original = cache.read_bytes()
                output = root / "suggestions"
                message = io.StringIO()
                with contextlib.redirect_stdout(message), contextlib.redirect_stderr(message):
                    code = glossary_main([str(cache), "--out", str(output)])
                self.assertEqual(code, 2, message.getvalue())
                self.assertIn("ERRO ao ler o cache", message.getvalue())
                self.assertEqual(cache.read_bytes(), original)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
