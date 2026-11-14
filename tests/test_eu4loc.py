"""Contratos do leitor fiel da localisation EU4 (sem dependências YAML)."""
import tempfile
import unittest
from pathlib import Path

from ck2translator.eu4loc import LocFile, compare, hard_problems, split_lines


BOM = b"\xef\xbb\xbf"
SAMPLE = (
    '# Comentário: "preservar"\r\n'
    'l_english: # idioma\r\n'
    '\r\n'
    '  empire.name:12  "  The §YEmpire§! $NAME|Y$ [Root.GetName] £adm£ @FRA\\n  "  # "comentário"\r\n'
    ' without_version: "Say \\"Hello\\"; # is text" # outside\n'
    ' raw_quotes:0 "The "Great" Empire" # keep "this"\r\n'
    ' empty:0 ""\r\n'
    'l_french:\r\n'
    ' empire.name:0 "Empire français"'
)


class Eu4LocTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def read(self, text=SAMPLE, *, bom=True, name="a_l_english.yml"):
        path = self.root / name
        path.write_bytes((BOM if bom else b"") + text.encode("utf-8"))
        return LocFile.read(path)

    def test_bom_roundtrip_is_byte_identical(self):
        loc = self.read()
        self.assertEqual(loc.to_bytes(), BOM + SAMPLE.encode("utf-8"))
        self.assertEqual(loc.encoding, "utf-8-sig")

    def test_empty_and_mixed_eols(self):
        for sample in ("", "# only comment", "l_english:\r\n k:0 \"a\"\n x: \"b\"", "l_english:\n"):
            with self.subTest(sample=sample):
                self.assertEqual(self.read(sample).to_bytes(), BOM + sample.encode("utf-8"))

    def test_source_without_bom_is_normalized_on_write(self):
        loc = self.read(bom=False)
        self.assertEqual(loc.encoding, "utf-8")
        self.assertEqual(loc.to_bytes(), BOM + SAMPLE.encode("utf-8"))
        loc.write(self.root / "out.yml")
        self.assertEqual(compare(loc, LocFile.read(self.root / "out.yml"), {"english"}, "english"), [])

    def test_invalid_utf8_is_rejected(self):
        path = self.root / "invalid.yml"
        path.write_bytes(BOM + b'l_english:\n k:0 "caf\xe9"')
        with self.assertRaises(UnicodeDecodeError):
            LocFile.read(path)

    def test_entries_have_language_and_original_values(self):
        entries = list(self.read().entries())
        self.assertEqual([line.language for line in entries], ["english"] * 4 + ["french"])
        self.assertEqual(entries[0].key, "empire.name")
        self.assertEqual(entries[1].value, 'Say \\"Hello\\"; # is text')
        self.assertEqual(entries[2].value, 'The "Great" Empire')
        self.assertEqual(entries[3].value, "")
        self.assertEqual(entries[0].suffix, '"  # "comentário"')

    def test_setting_value_only_changes_quoted_content(self):
        loc = self.read()
        line = next(loc.entries())
        value = line.value.replace("The", "O").replace("Empire", "Império")
        loc.set_value(line, value)
        expected = SAMPLE.replace("The §YEmpire", "O §YImpério")
        self.assertEqual(loc.to_bytes(), BOM + expected.encode("utf-8"))
        loc.write(self.root / "out.yml")
        self.assertEqual(compare(self.read(), LocFile.read(self.root / "out.yml"), {"english"}, "english"), [])

    def test_write_is_utf8_for_non_cp1252_text(self):
        loc = self.read('l_english:\n k:0 "Hello"')
        loc.set_value(next(loc.entries()), "Olá 世界")
        self.assertIn("Olá 世界".encode("utf-8"), loc.to_bytes())

    def test_new_semicolon_and_plain_quotes_are_safe(self):
        loc = self.read('l_english:\n k:0 "Hello" # comment')
        loc.set_value(next(loc.entries()), 'Olá; "mundo"')
        self.assertIn(b'"Ol\xc3\xa1; "mundo"" # comment', loc.to_bytes())
        self.assertEqual(hard_problems("Hello", "Olá; mundo"), [])

    def test_invalid_new_values_do_not_mutate_line(self):
        loc = self.read('l_english:\n k:0 "Hello" # comment')
        line = next(loc.entries())
        original = loc.to_bytes()
        for value in ('Ol\ná', 'foo" # escaped into comment', 'foo\\', 'bad\tvalue', '\ud800'):
            with self.subTest(value=repr(value)):
                with self.assertRaises((ValueError, UnicodeEncodeError)):
                    loc.set_value(line, value)
                self.assertEqual(loc.to_bytes(), original)
        with self.assertRaises(ValueError):
            loc.set_value(loc.lines[0], "header")

    def test_malformed_entries_report_line_and_path(self):
        for sample in ('l_english:\n k:0 "unfinished', 'l_english:\n k:0 unquoted',
                       'l_english:\n k:abc "Hello"', 'l_english:\n k:0 "Hello" trailing',
                       ' k:0 "Hello"', 'l_english:\n broken key:0 "Hello"'):
            with self.subTest(sample=sample):
                with self.assertRaisesRegex(ValueError, r"a_l_english.yml: linha \d+"):
                    self.read(sample)

    def test_literal_escape_and_flag_protection(self):
        src = 'Hello @FRA $DEV|Y$ [Root.GetName] §Y£adm£§!\\n\\t\\"\\\\'
        self.assertEqual(hard_problems(src, src.replace("Hello", "Olá")), [])
        for token in ("@FRA", "$DEV|Y$", "[Root.GetName]", "§Y", "£adm£", "\\n", "\\t", '\\"', "\\\\"):
            with self.subTest(token=token):
                self.assertTrue(hard_problems(src, src.replace(token, "")))
        self.assertTrue(hard_problems("@FRA", "@ENG"))
        self.assertTrue(hard_problems(" A ", "A"))

    def test_compare_detects_changes_outside_value_and_codes(self):
        mutations = {
            "key": ("empire.name:12", "other.name:12"),
            "version": ("empire.name:12", "empire.name:13"),
            "comment": ('# "comentário"', '# "alterado"'),
            "header": ("l_french:", "l_german:"),
            "foreign_language": ("Empire français", "Mudou"),
            "flag": ("@FRA", "@ENG"),
            "escape": ("\\n", "\\t"),
            "eol": ('# Comentário: "preservar"\r\n', '# Comentário: "preservar"\n'),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(name=name):
                original = self.read()
                changed = self.read(SAMPLE.replace(old, new), name="changed.yml")
                self.assertTrue(compare(original, changed, {"english"}, "english"))

    def test_compare_requires_bom_and_line_count(self):
        original = self.read()
        missing_bom = self.read(bom=False, name="no_bom.yml")
        self.assertTrue(compare(original, missing_bom, {"english"}, "english"))
        shorter = self.read("l_english:\n", name="short.yml")
        self.assertTrue(compare(original, shorter, {"english"}, "english"))

    def test_line_splitting_preserves_unicode_line_separator(self):
        sample = 'l_english:\n k:0 "Hello\u2028there"'
        self.assertEqual(len(split_lines(sample)), 2)
        self.assertEqual(self.read(sample).to_bytes(), BOM + sample.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
