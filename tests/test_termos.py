"""Testes de keep.txt / glossary.txt e da aplicação deles. Rode:  python -m unittest -v"""
import re
import tempfile
import unittest
from pathlib import Path

from ck2translator.termos import read_glossary, read_keep
from ck2translator.translator import Backend, Cache, Translator, mask, unmask


class PT(Backend):
    """Backend sem prompt: 'traduz' trocando algumas palavras e preserva os marcadores {n}."""
    model_id = "translators"
    uses_prompt = False
    DIC = {"the": "o", "rules": "governa", "of": "de", "and": "e", "army": "exército"}

    def chat(self, system, user):
        t = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        return " ".join(self.DIC.get(w.lower(), w) for w in t.split())


def tmp(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "x.txt"
    p.write_text(text, encoding="utf-8-sig")
    return p


class Arquivos(unittest.TestCase):
    def test_keep(self):
        p = tmp("# nomes\nWinterfell\n\n  Night's Watch   # grupo\n'Dorne'\n")
        self.assertEqual(read_keep(p), ["Winterfell", "Night's Watch", "Dorne"])

    def test_glossario_formatos(self):
        p = tmp("House = Casa\nLord -> Senhor\nKing: Rei\n\"Iron Throne\" = \"Trono de Ferro\"  # nota\n")
        self.assertEqual(read_glossary(p), {"House": "Casa", "Lord": "Senhor", "King": "Rei",
                                            "Iron Throne": "Trono de Ferro"})

    def test_glossario_invalido(self):
        for ruim in ("sem separador\n", "A = \n", "A = b;c\n"):
            with self.assertRaises(ValueError):
                read_glossary(tmp(ruim))

    def test_chave_com_dois_pontos_e_apostrofo(self):
        self.assertEqual(read_glossary(tmp("R'hllor = R'hllor\n")), {"R'hllor": "R'hllor"})


class Aplicacao(unittest.TestCase):
    def test_mask_glossario_e_maiuscula(self):
        m, tk = mask("The King and king", None, [("king", "rei")])
        self.assertEqual(m, "The {1} and {2}")
        self.assertEqual(tk, ["Rei", "rei"])
        self.assertEqual(unmask(m, tk), "The Rei and rei")

    def test_mask_nao_pega_parte_de_palavra(self):
        m, _ = mask("Kingdom and Kings", None, [("King", "Rei")])
        self.assertEqual(m, "Kingdom and Kings")

    def test_termo_mais_longo_vence(self):
        m, tk = mask("Iron Throne", None, [("Iron", "Ferro"), ("Iron Throne", "Trono de Ferro")])
        self.assertEqual((m, tk), ("{1}", ["Trono de Ferro"]))

    def test_translators_aplica_glossario_e_keep(self):
        t = Translator(PT(), self.enterContext(Cache()), {"Lord": "Senhor"}, ["Winterfell"])
        r = t.translate("The Lord rules Winterfell and [Root.GetName]")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.text, "o Senhor governa Winterfell e [Root.GetName]")

    def test_so_glossario_nao_e_pulado(self):
        r = Translator(PT(), self.enterContext(Cache()), {"House": "Casa"}, []).translate("House")
        self.assertEqual((r.status, r.text), ("ok", "Casa"))

    def test_sem_termos_continua_pulando_codigo_puro(self):
        r = Translator(PT(), self.enterContext(Cache()), {"House": "Casa"}, []).translate("[Root.GetName]")
        self.assertEqual(r.status, "skip")

    def test_mudar_glossario_refaz_so_o_afetado(self):
        c = self.enterContext(Cache())
        a = Translator(PT(), c, {}, []); a.translate("The army"); a.translate("The King")
        b = Translator(PT(), c, {"King": "Rei"}, [])
        self.assertEqual(b.translate("The army").status, "cache")
        self.assertEqual(b.translate("The King").status, "ok")
        self.assertEqual(b.translate("The King").text, "o Rei")


class Ollama(Backend):
    model_id = "ollama:x"

    def chat(self, system, user):
        self.system = system
        return re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)


class Prompt(unittest.TestCase):
    def test_ollama_recebe_glossario_e_keep_no_prompt(self):
        b = Ollama()
        Translator(b, self.enterContext(Cache()), {"Lord": "Senhor"}, ["Winterfell"]).translate("Lord of Winterfell")
        self.assertIn('"Lord" -> "Senhor"', b.system)
        self.assertIn('manter exatamente como está: "Winterfell"', b.system)


class Config(unittest.TestCase):
    def _cfg(self, toml, **arquivos):
        from ck2translator import cli as ck2tr
        d = Path(tempfile.mkdtemp())
        for nome, txt in arquivos.items():
            (d / nome.replace("_", ".")).write_text(txt, encoding="utf-8")
        (d / "config.toml").write_text(toml, encoding="utf-8")
        return ck2tr.load_config(str(d / "config.toml"))

    BASE = '[translate]\n{extra}\n[[mods]]\nname="m"\npath="."\n{tail}'

    def test_arquivos_padrao_e_mesclagem(self):
        cfg = self._cfg(self.BASE.format(extra="", tail='[keep]\nterms=["A"]\n[glossary]\nX="x"\n'),
                        keep_txt="B\nKing\n", glossary_txt="King = Rei\n")
        self.assertEqual(cfg["keep_terms"], ["A", "B"])          # King saiu do keep: está no glossário
        self.assertEqual(cfg["glossary_terms"], {"X": "x", "King": "Rei"})

    def test_nomes_personalizados_e_desligar(self):
        cfg = self._cfg(self.BASE.format(extra='keep_file = "meus.txt"\nglossary_file = ""', tail=""),
                        meus_txt="Z\n", glossary_txt="a = b\n")
        self.assertEqual((cfg["keep_terms"], cfg["glossary_terms"]), (["Z"], {}))

    def test_sem_arquivos(self):
        cfg = self._cfg(self.BASE.format(extra="", tail=""))
        self.assertEqual((cfg["keep_terms"], cfg["glossary_terms"]), ([], {}))


if __name__ == "__main__":
    unittest.main()
