"""Perfis, compatibilidade das configurações antigas e descritores separados."""
import tempfile
import unittest
from pathlib import Path

from ck2translator.config import ConfigError, Mod, load_config, resolve_mods
from ck2translator.profiles import PROFILES, get_profile, write_descriptors


class Profiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mod_path = self.root / "original"
        (self.mod_path / "localisation").mkdir(parents=True)
        self.config = self.root / "config.toml"

    def configure(self, game="", translate="", mods="name='Meu Mod'\npath='original'\n"):
        self.config.write_text(
            game + "\n[model]\nbackend='echo'\n[translate]\n" + translate + "\n[[mods]]\n" + mods,
            encoding="utf-8",
        )
        return load_config(str(self.config))

    def test_old_config_defaults_to_ck2(self):
        cfg = self.configure()
        self.assertEqual(cfg["game"], {"id": "ck2"})
        self.assertEqual(resolve_mods(cfg, None)[0].game, "ck2")
        profile = get_profile(cfg)
        self.assertEqual(profile.suffix, ".csv")
        self.assertFalse(profile.recursive)
        self.assertEqual(get_profile(), profile)

    def test_eu4_profile_and_defaults(self):
        cfg = self.configure("[game]\nid='eu4'")
        self.assertEqual(resolve_mods(cfg, None)[0].game, "eu4")
        self.assertEqual(get_profile(cfg).suffix, ".yml")
        self.assertTrue(get_profile(cfg).recursive)
        self.assertEqual(cfg["translate"]["write_columns"], ["english"])
        self.assertEqual(cfg["translate"]["source_column"], "english")
        self.assertEqual(cfg["translate"]["mode"], "overlay")
        self.assertEqual(set(PROFILES), {"ck2", "eu4"})

    def test_unknown_and_malformed_game_rejected(self):
        for game in ("[game]\nid='eu5'", "[game]\nid=1", "[game]\nid=[]", "game='eu4'"):
            with self.subTest(game=game), self.assertRaises(ConfigError):
                self.configure(game)

    def test_eu4_requires_same_native_language_slot(self):
        for setting in ("source_column='portuguese'", "write_columns=['portuguese']",
                        "write_columns=['french']", "write_columns=['english','french']"):
            with self.subTest(setting=setting), self.assertRaises(ConfigError):
                self.configure("[game]\nid='eu4'", setting)
        cfg = self.configure("[game]\nid='eu4'", "source_column='french'\nwrite_columns=['french']")
        self.assertEqual(cfg["translate"]["source_column"], "french")

    def test_eu4_inplace_rejected_in_configuration_and_cli_override(self):
        with self.assertRaisesRegex(ConfigError, "overlay"):
            self.configure("[game]\nid='eu4'", "mode='inplace'")
        cfg = self.configure("[game]\nid='eu4'")
        cfg["translate"]["mode"] = "inplace"
        with self.assertRaisesRegex(ConfigError, "overlay"):
            resolve_mods(cfg, None)

    def test_supported_version_validation(self):
        for supported in ("123", "false", "''", "'   '", '"1.37\\n"'):
            with self.subTest(supported=supported), self.assertRaisesRegex(ConfigError, "supported_version"):
                self.configure("[game]\nid='eu4'\nsupported_version=" + supported)

    def test_eu4_prefers_internal_descriptor_for_exact_dependency_name(self):
        (self.mod_path / "descriptor.mod").write_text('name="Império Épico"\n', encoding="utf-8-sig")
        (self.mod_path / "a.mod").write_text('name="Outro"\n', encoding="utf-8")
        cfg = self.configure("[game]\nid='eu4'", mods="path='original'")
        self.assertEqual(resolve_mods(cfg, None)[0].name, "Império Épico")
        cfg = self.configure("[game]\nid='eu4'")
        self.assertEqual(resolve_mods(cfg, None)[0].name, "Meu Mod")

    def test_overlay_cannot_overlap_source(self):
        for output in ("original", "original/nested"):
            with self.subTest(output=output):
                cfg = self.configure("[game]\nid='eu4'", f"output_dir='{output}'")
                with self.assertRaisesRegex(ConfigError, "sobrepõe"):
                    resolve_mods(cfg, None)

    def test_ck2_descriptor_bytes_remain_compatible(self):
        mod = Mod('Mod "Épico"', self.mod_path)
        out = self.root / "output"
        write_descriptors(out, mod)
        expected = ('name = "Mod \'Épico\' [PT-BR]"\n'
                    'path = "mod/mod_epico_ptbr"\n'
                    'dependencies = { "Mod \'Épico\'" }\n')
        self.assertEqual((out / "mod_epico_ptbr.mod").read_bytes(), expected.encode("cp1252"))
        self.assertFalse((out / "mod_epico_ptbr" / "descriptor.mod").exists())

    def test_eu4_descriptors_include_dependency_and_user_supported_version(self):
        cfg = self.configure("[game]\nid='eu4'\nsupported_version='1.37.*'")
        mod = Mod('世界 Épico', self.mod_path, "eu4")
        out = self.root / "output"
        write_descriptors(out, mod, cfg)
        internal = (out / "epico_ptbr" / "descriptor.mod").read_text(encoding="utf-8")
        external = (out / "epico_ptbr.mod").read_text(encoding="utf-8")
        self.assertIn('name = "世界 Épico [PT-BR]"', internal)
        self.assertIn('dependencies = { "世界 Épico" }', internal)
        self.assertIn('tags = { "Translation" }', internal)
        self.assertIn('supported_version = "1.37.*"', internal)
        self.assertNotIn("path =", internal)
        self.assertEqual(external, internal + 'path = "mod/epico_ptbr"\n')
        self.assertNotIn("remote_file_id", external)
        self.assertNotIn("replace_path", external)

    def test_eu4_descriptor_escapes_name_and_does_not_invent_version(self):
        mod = Mod('Mod "A" \\ B', self.mod_path, "eu4")
        out = self.root / "output"
        write_descriptors(out, mod)
        text = (out / "mod_a_b_ptbr" / "descriptor.mod").read_text(encoding="utf-8")
        self.assertIn('dependencies = { "Mod \\"A\\" \\\\ B" }', text)
        self.assertNotIn("supported_version", text)


if __name__ == "__main__":
    unittest.main()
