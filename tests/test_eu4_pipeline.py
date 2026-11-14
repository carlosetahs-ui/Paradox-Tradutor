"""Integração do CLI EU4: ficheiros fiéis, cache e mod separado instalável."""
import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ck2translator import cli
from ck2translator.eu4loc import LocFile
from ck2translator.translator import EchoBackend


BOM = b"\xef\xbb\xbf"
CORE = (
    '# English and French; comment with "quotes"\r\n'
    'l_english: # keep language\r\n'
    ' welcome:0 "Welcome to Anbennar." # keep comment\r\n'
    ' coded:12 "The §YEmpire§! of $NAME|Y$ [Root.GetName] £adm£ @FRA\\nLong live \\"Anbennar\\"!" # codes\n'
    ' padded: "  Welcome home  "\r\n'
    ' only_code:0 "$NAME$"\r\n'
    ' empty:0 ""\r\n'
    'l_french:\r\n'
    ' welcome:0 "Bienvenue en Anbennar."'
)
NESTED = (
    'l_english:\n'
    ' visit:0 "Visit Anbennar today"\n'
    ' reward:0 "Gain @ENG gold"\n'
    ' peace:0 "Peace; prosperity"'
)


class Eu4PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.loc = self.root / "original" / "localisation"
        self.loc.mkdir(parents=True)
        self.core = self.write("00_core_l_english.yml", BOM + CORE.encode("utf-8"))
        self.nested = self.write("english/events_l_english.YML", NESTED.encode("utf-8"))
        self.write("french/other_l_french.yml", BOM + 'l_french:\n other:0 "Sans traduction"'.encode("utf-8"))
        self.write("comments_l_english.yml", BOM + b"# comment only\r\n")
        # A CK2 CSV is deliberately invalid UTF-8; EU4 must never parse it.
        self.write("ignored.csv", b"legacy;bad \xe9\n")
        self.originals = {path: path.read_bytes() for path in self.loc.rglob("*") if path.is_file()}
        self.config = self.root / "config.toml"
        self.out = self.root / "saida"
        self.generated = self.out / "meu_eu4_ptbr"
        self.configure()

    def write(self, relative, data):
        path = self.loc / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def configure(self, translate="", *, model="backend = 'echo'", terms="", game=""):
        self.config.write_text(
            "[game]\nid = 'eu4'\nsupported_version = '1.37.*'\n" + game + "\n"
            + "[model]\n" + model + "\n"
            + "[translate]\nptbr_fix = false\n" + translate + "\n"
            + "[[mods]]\nname = 'Meu EU4'\npath = 'original'\n" + terms,
            encoding="utf-8",
        )

    def run_cli(self, command, *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = cli.main([command, "--config", str(self.config), *options])
        return result, output.getvalue()

    def translate(self, *options):
        code, message = self.run_cli("translate", *options)
        self.assertEqual(code, 0, message)

    def translated(self, relative="00_core_l_english.yml"):
        return self.generated / "localisation" / relative

    def test_scan_reads_recursive_yml_and_counts_selected_language(self):
        code, message = self.run_cli("scan")
        self.assertEqual(code, 0, message)
        self.assertIn("english/events_l_english.YML", message)
        self.assertIn("7 textos", message)
        self.assertIn("utf-8-sig", message)
        self.assertNotIn("ignored.csv", message)
        self.assertFalse(self.out.exists())

    def test_complete_overlay_preserves_originals_and_layout(self):
        self.translate()
        self.assertEqual({path: path.read_bytes() for path in self.originals}, self.originals)
        expected = {"00_core_l_english.yml", "english/events_l_english.YML"}
        actual = {path.relative_to(self.generated / "localisation").as_posix()
                  for path in (self.generated / "localisation").rglob("*") if path.is_file()}
        self.assertEqual(actual, expected)
        for relative in expected:
            self.assertTrue(self.translated(relative).read_bytes().startswith(BOM))
        original = LocFile.read(self.core)
        translated = LocFile.read(self.translated())
        self.assertEqual([line.eol for line in original.lines], [line.eol for line in translated.lines])
        entries = {(line.language, line.key): line.value for line in translated.entries()}
        self.assertEqual(entries["english", "welcome"], "PT> Welcome to Anbennar.")
        self.assertEqual(entries["french", "welcome"], "Bienvenue en Anbennar.")
        self.assertEqual(entries["english", "padded"], "  PT> Welcome home  ")
        self.assertEqual(entries["english", "only_code"], "$NAME$")
        self.assertEqual(entries["english", "empty"], "")
        self.assertIn(' coded:12 "PT> The §YEmpire§! of $NAME|Y$ [Root.GetName] £adm£ @FRA\\n',
                      self.translated().read_text(encoding="utf-8-sig"))
        self.assertEqual(self.run_cli("verify")[0], 0)

    def test_descriptors_include_dependency_and_compatible_version(self):
        self.translate()
        external = (self.out / "meu_eu4_ptbr.mod").read_text(encoding="utf-8")
        internal = (self.generated / "descriptor.mod").read_text(encoding="utf-8")
        for text in (external, internal):
            self.assertIn('name = "Meu EU4 [PT-BR]"', text)
            self.assertIn('dependencies = { "Meu EU4" }', text)
            self.assertIn('supported_version = "1.37.*"', text)
            self.assertIn('tags = { "Translation" }', text)
            self.assertNotIn("remote_file_id", text)
        self.assertIn('path = "mod/meu_eu4_ptbr"', external)
        self.assertNotIn("path =", internal)

    def test_package_contains_installable_mod_and_eu4_readme(self):
        self.translate()
        code, message = self.run_cli("package")
        self.assertEqual(code, 0, message)
        with zipfile.ZipFile(self.out / "meu_eu4_ptbr.zip") as archive:
            names = set(archive.namelist())
            self.assertIn("meu_eu4_ptbr.mod", names)
            self.assertIn("meu_eu4_ptbr/descriptor.mod", names)
            nested = "meu_eu4_ptbr/localisation/english/events_l_english.YML"
            self.assertIn(nested, names)
            self.assertTrue(archive.read(nested).startswith(BOM))
            readme = archive.read("LEIA-ME.txt").decode("utf-8")
            self.assertIn("Europa Universalis IV", readme)
            self.assertIn("INGLÊS", readme)
            self.assertNotIn("Crusader Kings", readme)
            self.assertNotIn("ignored.csv", "\n".join(names))

    def test_terms_only_considers_selected_language(self):
        self.write("flags_l_english.yml", BOM + (
            'l_english:\n first:0 "See @FRA today"\n second:0 "Seek @FRA tomorrow"'
        ).encode("utf-8"))
        code, message = self.run_cli("terms", "--min-count", "2")
        self.assertEqual(code, 0, message)
        terms = (self.out / "termos_sugeridos.txt").read_text(encoding="utf-8")
        self.assertIn("Anbennar", terms)
        self.assertNotIn("Sans", terms)
        self.assertNotIn("FRA", terms)
        self.assertFalse((self.root / "cache.sqlite").exists())

    def test_status_needs_no_live_backend_and_creates_no_files(self):
        self.configure(model="backend = 'argos'")
        with mock.patch.dict("sys.modules", {"argostranslate": None, "argostranslate.translate": None}):
            code, message = self.run_cli("status")
        self.assertEqual(code, 0, message)
        self.assertIn("faltam 6", message)
        self.assertIn("pulados 1", message)
        self.assertFalse((self.root / "cache.sqlite").exists())
        self.assertFalse(self.out.exists())

    def test_cached_status_and_rerun_reuse_translation_without_writes_to_cache(self):
        self.translate()
        cache = self.root / "cache.sqlite"
        before = cache.read_bytes()
        code, message = self.run_cli("status")
        self.assertEqual(code, 0, message)
        self.assertIn("faltam 0", message)
        self.assertEqual(cache.read_bytes(), before)
        backend = EchoBackend()
        with mock.patch.object(backend, "chat", wraps=backend.chat) as chat, \
                mock.patch.object(cli, "build_backend", return_value=backend):
            self.translate()
        # The legacy preflight ping remains; every game text comes from cache.
        self.assertEqual(chat.call_count, 1)
        self.assertIn("ping", chat.call_args.args[1])

    def test_diff_finds_translations_and_warns_about_eu4_flags(self):
        self.translate()
        code, message = self.run_cli("diff", "--find", "coded")
        self.assertEqual(code, 0, message)
        self.assertIn("chave: coded", message)
        self.assertIn("ORIGINAL:", message)
        self.assertIn("PT:", message)
        path = self.translated()
        path.write_bytes(path.read_bytes().replace(b"@FRA", b"@ENG"))
        code, message = self.run_cli("diff")
        self.assertEqual(code, 0, message)
        self.assertIn("códigos do jogo", message)

    def test_skip_patterns_match_subdirectories_case_insensitively(self):
        self.configure("skip_files = ['ENGLISH/*']")
        self.translate()
        self.assertTrue(self.translated().exists())
        self.assertFalse(self.translated("english/events_l_english.YML").exists())
        self.assertEqual(self.run_cli("verify")[0], 0)

    def test_limit_leaves_remaining_values_in_original_language(self):
        self.translate("--limit", "1")
        entries = {(line.language, line.key): line.value for line in LocFile.read(self.translated()).entries()}
        self.assertEqual(entries["english", "welcome"], "PT> Welcome to Anbennar.")
        self.assertTrue(entries["english", "coded"].startswith("The §YEmpire"))
        self.assertFalse(self.translated("english/events_l_english.YML").exists())
        self.assertEqual(self.run_cli("verify")[0], 0)

    def test_paranoid_rerun_restores_original_coded_values(self):
        self.translate()
        self.configure("safety = 'paranoid'")
        self.translate()
        original = {(line.language, line.key): line.value for line in LocFile.read(self.core).entries()}
        translated = {(line.language, line.key): line.value for line in LocFile.read(self.translated()).entries()}
        self.assertEqual(translated["english", "coded"], original["english", "coded"])
        nested = {line.key: line.value for line in LocFile.read(self.translated("english/events_l_english.YML")).entries()}
        self.assertEqual(nested["reward"], "Gain @ENG gold")
        self.assertEqual(nested["visit"], "PT> Visit Anbennar today")
        self.assertEqual(self.run_cli("verify")[0], 0)

    def test_glossary_and_keep_use_existing_backend_pipeline(self):
        self.configure(terms="\n[glossary]\nPeace = 'Paz'\n[keep]\nterms = ['Anbennar']\n")
        backend = EchoBackend()
        backend.uses_prompt = False
        backend.model_id = "pipeline"
        with mock.patch.object(cli, "build_backend", return_value=backend):
            self.translate()
        text = self.translated().read_text(encoding="utf-8-sig")
        self.assertIn("Anbennar", text)
        self.assertIn("@FRA", text)
        nested = self.translated("english/events_l_english.YML").read_text(encoding="utf-8-sig")
        self.assertIn("Paz; prosperity", nested)
        self.assertEqual(self.run_cli("verify")[0], 0)

    def test_verify_and_package_refuse_structural_corruption(self):
        self.translate()
        path = self.translated()
        clean = path.read_bytes()
        cases = {
            "key": (b"coded:12", b"changed:12"),
            "version": (b"coded:12", b"coded:13"),
            "comment": (b"# keep comment", b"# altered comment"),
            "flag": (b"@FRA", b"@ENG"),
            "variable": (b"$NAME|Y$", b"$OTHER|Y$"),
            "escape": (b"\\nLong", b"\\tLong"),
            "bom": (BOM, b""),
        }
        for label, (old, new) in cases.items():
            with self.subTest(label=label):
                path.write_bytes(clean.replace(old, new, 1))
                code, message = self.run_cli("verify")
                self.assertEqual(code, 1, message)
                code, message = self.run_cli("package")
                self.assertEqual(code, 1, message)
                self.assertFalse((self.out / "meu_eu4_ptbr.zip").exists())
                self.assertNotIn("Traceback", message)
        path.write_bytes(clean)

    def test_package_requires_internal_descriptor(self):
        self.translate()
        (self.generated / "descriptor.mod").unlink()
        code, message = self.run_cli("package")
        self.assertEqual(code, 1, message)
        self.assertIn("descriptor.mod", message)
        self.assertFalse((self.out / "meu_eu4_ptbr.zip").exists())

    def test_invalid_source_has_actionable_error_without_traceback(self):
        for invalid in (BOM + b'l_english:\n broken:0 "unfinished',
                        BOM + b'l_english:\n broken:0 "caf\xe9"'):
            with self.subTest(invalid=invalid):
                self.core.write_bytes(invalid)
                for command in ("scan", "status", "terms", "translate"):
                    code, message = self.run_cli(command)
                    self.assertEqual(code, 2, message)
                    self.assertIn("Localisation inválida", message)
                    self.assertIn(self.core.name, message)
                    self.assertNotIn("Traceback", message)

    def test_corrupted_output_reports_failure_without_traceback(self):
        self.translate()
        for invalid in (BOM + b'l_english:\n broken:0 "unfinished',
                        BOM + b'l_english:\n broken:0 "caf\xe9"'):
            with self.subTest(invalid=invalid):
                self.translated().write_bytes(invalid)
                for command in ("verify", "package"):
                    code, message = self.run_cli(command)
                    self.assertEqual(code, 1, message)
                    self.assertIn("Localisation inválida", message)
                    self.assertNotIn("Traceback", message)

    def test_french_slot_is_supported_without_changing_headers_or_filenames(self):
        self.configure("source_column = 'french'\nwrite_columns = ['french']")
        self.translate()
        entries = {(line.language, line.key): line.value for line in LocFile.read(self.translated()).entries()}
        self.assertEqual(entries["english", "welcome"], "Welcome to Anbennar.")
        self.assertEqual(entries["french", "welcome"], "PT> Bienvenue en Anbennar.")
        self.assertTrue(self.translated("french/other_l_french.yml").exists())
        self.assertFalse(self.translated("english/events_l_english.YML").exists())
        self.assertEqual(self.run_cli("verify")[0], 0)
        code, message = self.run_cli("package")
        self.assertEqual(code, 0, message)
        with zipfile.ZipFile(self.out / "meu_eu4_ptbr.zip") as archive:
            self.assertIn("FRANCÊS", archive.read("LEIA-ME.txt").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
