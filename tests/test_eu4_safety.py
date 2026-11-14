"""Regressões de metadados EU4 encontradas durante a revisão da integração."""
import tempfile
import unittest
from pathlib import Path

from ck2translator.config import _read_mod_name, load_config, resolve_mods
from ck2translator.profiles import write_descriptors


class Eu4DescriptorSafetyTests(unittest.TestCase):
    def test_descriptor_name_with_quotes_and_backslashes_keeps_exact_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mod_path = root / "m"
            (mod_path / "localisation").mkdir(parents=True)
            name = 'The "Great" Mod \\ Expansão'
            escaped = name.replace("\\", "\\\\").replace('"', '\\"')
            (mod_path / "descriptor.mod").write_text(f'name="{escaped}"\n', encoding="utf-8")
            config_path = root / "config.toml"
            config_path.write_text(
                '[game]\nid="eu4"\n[model]\nbackend="echo"\n[[mods]]\npath="m"\n',
                encoding="utf-8",
            )
            cfg = load_config(str(config_path))
            mod = resolve_mods(cfg, None)[0]
            self.assertEqual(mod.name, name)
            write_descriptors(cfg["translate"]["output_dir"], mod, cfg)
            output = cfg["translate"]["output_dir"]
            descriptors = [output / f"{mod.slug}_ptbr.mod", output / f"{mod.slug}_ptbr" / "descriptor.mod"]
            for path in descriptors:
                with self.subTest(descriptor=path.name):
                    self.assertEqual(_read_mod_name(path), name + " [PT-BR]")
                    self.assertIn(f'dependencies = {{ "{escaped}" }}', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
