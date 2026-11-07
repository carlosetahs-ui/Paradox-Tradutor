"""Teste do comando 'diff'. Rode:  python -m unittest -v"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from ck2translator import cli as ck2tr


class DiffTests(unittest.TestCase):
    def test_avisos(self):
        w = ck2tr.struct_warnings("Hello [Root.GetName] $X$ now", "Ola [Root.GetName] now")
        self.assertTrue(any("'$'" in x for x in w))
        self.assertTrue(any("códigos" in x for x in w))
        self.assertEqual(ck2tr.struct_warnings("Hi [A] there", "Oi [A] ali"), [])
        self.assertTrue(any("ORDEM" in x for x in ck2tr.struct_warnings("[A] and [B]", "[B] e [A]")))

    def test_comando(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            head = "#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x\r\n"
            (d / "m" / "localisation").mkdir(parents=True)
            (d / "saida" / "m_ptbr" / "localisation").mkdir(parents=True)
            (d / "m" / "localisation" / "a.csv").write_bytes((head + "k1;Sacrifice [Root.GetName] to Sigmar;a;b;;c;;;;;;;;;x\r\nk2;Fine;a;b;;c;;;;;;;;;x\r\n").encode("cp1252"))
            (d / "saida" / "m_ptbr" / "localisation" / "a.csv").write_bytes((head + "k1;Sacrificar para Sigmar;a;b;;c;;;;;;;;;x\r\nk2;Bom;a;b;;c;;;;;;;;;x\r\n").encode("cp1252"))
            cfg = d / "config.toml"
            cfg.write_text(f"[model]\nbackend='echo'\n[translate]\n[[mods]]\nname='M'\npath='{d / 'm'}'\n", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ck2tr.main(["diff", "--config", str(cfg)])
            out = buf.getvalue()
            self.assertIn("k1", out)
            self.assertIn("códigos do jogo diferentes", out)
            self.assertNotIn("k2", out)                       # sem aviso: não aparece
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ck2tr.main(["diff", "--config", str(cfg), "--find", "bom"])
            self.assertIn("k2", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
