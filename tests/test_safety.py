"""Testes da trava de segurança. Rode:  python -m unittest -v"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ck2translator import cli as ck2tr
from ck2translator.ck2loc import LocFile, hard_problems
from ck2translator.translator import Backend, Cache, EchoBackend, Translator, normalize_punct

HEAD = "#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x\r\n"


class Estranho(Backend):
    """Devolve tradução com um '[' solto e aspas tipográficas (o que um motor ruim poderia fazer)."""
    model_id = "estranho"

    def chat(self, system, user):
        import re
        t = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        return "“Ola [nota” " + t


class SafetyTests(unittest.TestCase):
    def test_hard_problems(self):
        self.assertEqual(hard_problems("Hi [A] $B$ there", "Oi [A] $B$ ali"), [])
        self.assertTrue(hard_problems("Hi $B$", "Oi"))
        self.assertTrue(hard_problems("Hi", "Oi ["))
        self.assertTrue(hard_problems("Hi", " Oi"))
        self.assertTrue(hard_problems("Hi \\n there", "Oi \\\\n ali"))
        self.assertTrue(hard_problems("Hi", "Oi;x"))

    def test_normalize_punct(self):
        self.assertEqual(normalize_punct("a b", "“ola” — ok…"), '"ola" - ok...')
        self.assertEqual(normalize_punct("it’s", "’ola’"), "’ola’")   # o original já usava

    def _run(self, safety, backend):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        d = Path(t.name)
        (d / "m" / "localisation").mkdir(parents=True)
        orig = (HEAD + "k1;Sacrifice to Sigmar;a;b;;c;;;;;;;;;x\r\nk2;Hello $N$ there;a;b;;c;;;;;;;;;x\r\n").encode("cp1252")
        (d / "m" / "localisation" / "a.csv").write_bytes(orig)
        cfg = d / "config.toml"
        cfg.write_text(f"[model]\nbackend='echo'\n[translate]\nsafety='{safety}'\n[[mods]]\nname='M'\npath='{d / 'm'}'\n", encoding="utf-8")
        with mock.patch.object(ck2tr, "build_backend", return_value=backend):
            ck2tr.main(["translate", "--config", str(cfg)])
        out = d / "saida" / "m_ptbr" / "localisation" / "a.csv"
        return orig, (LocFile.read(out) if out.exists() else None), d

    def test_strict_reverte_linha_ruim(self):
        orig, loc, d = self._run("strict", Estranho())
        self.assertIsNone(loc)                       # todas as linhas foram revertidas: nada para gravar
        rel = (d / "saida" / "m_relatorio.csv").read_text(encoding="utf-8-sig")
        self.assertIn("revertida", rel)

    def test_normal_deixa_passar(self):
        _, loc, _ = self._run("normal", Estranho())
        self.assertIsNotNone(loc)                    # sem a trava, o '[' solto iria para o arquivo

    def test_strict_aceita_boa_traducao(self):
        orig, loc, _ = self._run("strict", EchoBackend())
        textos = {l.key: l.fields[1] for l in loc.entries()}
        self.assertEqual(textos, {"k1": "PT> Sacrifice to Sigmar", "k2": "PT> Hello $N$ there"})

    def test_paranoid_nao_traduz_linhas_com_codigos(self):
        _, loc, _ = self._run("paranoid", EchoBackend())
        textos = {l.key: l.fields[1] for l in loc.entries()}
        self.assertEqual(textos["k1"], "PT> Sacrifice to Sigmar")
        self.assertEqual(textos["k2"], "Hello $N$ there")     # tinha $N$: ficou como estava

    def test_translator_paranoid_skip(self):
        r = Translator(EchoBackend(), self.enterContext(Cache()), paranoid=True).translate("Hi [Root.GetName]")
        self.assertEqual((r.status, r.text), ("skip", "Hi [Root.GetName]"))


if __name__ == "__main__":
    unittest.main()
