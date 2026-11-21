"""Avisos de qualidade sem correções destrutivas ou identificação geral de idioma."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from ck2translator import cli
from ck2translator.ck2loc import EU4_TOKEN_RE
from ck2translator.quality import quality_problems, unchanged_prose


class QualityTests(unittest.TestCase):
    def test_new_repeated_words_and_short_phrases(self):
        for text in ("O estado Estado declina.", "As Primeiras Eras Primeiras Eras terminaram.",
                     "Primeiros Primeiros Eras", "muito muito muito tarde"):
            with self.subTest(text=text):
                self.assertTrue(any("repetição" in item for item in quality_problems("The realm declines.", text)))

    def test_original_emphasis_and_sentence_punctuation_are_preserved(self):
        for src, dst in (("very very dark", "muito muito escuro"),
                         ("a state", "Um estado. Estado decadente."),
                         ("a state", "Um estado {1} Estado decadente."),
                         ("First First Era", "Primeira Primeira Era")):
            with self.subTest(src=src, dst=dst):
                self.assertEqual(quality_problems(src, dst), [])
        self.assertTrue(quality_problems("very very dark", "muito muito escuro e frio frio"))

    def test_unpaired_angle_quotes(self):
        for dst in ("'Olá«", "»Olá«", "«Olá"):
            self.assertTrue(any("aspas" in item for item in quality_problems("'Hello'", dst)))
        self.assertEqual(quality_problems("'Hello'", "«Olá»"), [])
        self.assertEqual(quality_problems("«Hello", "«Olá"), [])

    def test_unchanged_english_prose_and_gain_tooltips(self):
        for text in ("The army will gain more power.", "Mehrunes Dagon gains war exhaustion.",
                     "@[From.GetTag] §Y[From.GetName]§! gains more war exhaustion."):
            with self.subTest(text=text):
                self.assertTrue(unchanged_prose(text, text, token_re=EU4_TOKEN_RE))

    def test_names_protected_terms_codes_and_translations_are_not_english_failures(self):
        for text in ("Dunmer", "High King", "The Elder Scrolls Online", "$NAME$ [Root.GetName]",
                     "O exército ganha mais poder."):
            with self.subTest(text=text):
                self.assertFalse(unchanged_prose(text, text))
        name = "The people of the great house"
        self.assertFalse(unchanged_prose(name, name, keep=[name]))
        self.assertFalse(unchanged_prose(name, name, glossary={name: "O povo da grande casa"}))
        self.assertFalse(unchanged_prose("The army will gain more power.", "O exército ganha mais poder."))


class QualityDiffTests(unittest.TestCase):
    def test_diff_includes_unchanged_prose_and_quality_warnings_for_both_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            for game in ("ck2", "eu4"):
                with self.subTest(game=game):
                    root = Path(temporary) / game
                    source = root / "mod" / "localisation"
                    target = root / "out" / "quality_ptbr" / "localisation"
                    source.mkdir(parents=True)
                    target.mkdir(parents=True)
                    originals = {"untranslated": "The army will gain more power.",
                                 "name": "The Elder Scrolls Online", "kept": "The people of the great house",
                                 "codes": "$NAME$ [Root.GetName]", "repeat": "The realm is in decline.",
                                 "quotes": "Hello there", "translated": "Hello"}
                    translations = dict(originals, repeat="O estado Estado está em declínio.",
                                        quotes="'Olá«", translated="Olá")

                    def write(directory, entries):
                        if game == "ck2":
                            text = "#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x\n"
                            text += "".join(f"{key};{value};Bonjour;Hallo;;Hola;;;;;;;;;x\n" for key, value in entries.items())
                            (directory / "a.csv").write_bytes(text.encode("cp1252"))
                        else:
                            text = "l_english:\n" + "".join(f' {key}:0 "{value}"\n' for key, value in entries.items())
                            (directory / "a_l_english.yml").write_bytes(text.encode("utf-8-sig"))

                    write(source, originals)
                    write(target, translations)
                    config = root / "config.toml"
                    config.write_text(
                        f"[game]\nid='{game}'\n[translate]\noutput_dir='out'\nkeep_file=''\n"
                        "glossary_file=''\nreplace_file=''\n[[mods]]\nname='Quality'\npath='mod'\n"
                        "[keep]\nterms=['The people of the great house']\n", encoding="utf-8",
                    )
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main(["diff", "--config", str(config)]), 0)
                    message = output.getvalue()
                    self.assertIn("chave: untranslated", message)
                    self.assertIn("prosa em inglês", message)
                    self.assertIn("repetição consecutiva", message)
                    self.assertIn("aspas angulares", message)
                    for key in ("name", "kept", "codes", "translated"):
                        self.assertNotIn(f"chave: {key}", message)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main(["diff", "--config", str(config), "--find", "name"]), 0)
                    self.assertIn("chave: name", output.getvalue())
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main(["check", "--config", str(config)]), 0)


if __name__ == "__main__":
    unittest.main()
