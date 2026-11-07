"""Testes. Rode dentro da pasta do projeto:  python -m unittest -v"""
import tempfile
import unittest
from pathlib import Path

from ck2translator import cli as ck2tr
from ck2translator.ck2loc import LocFile, compare
from ck2translator.translator import (Backend, Cache, EchoBackend, Translator, fit_cp1252, mask, unmask)

HEADER = "#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x"
SAMPLE = (
    HEADER + "\r\n"
    "# comentário com ação\r\n"
    "\r\n"
    "title_empire;The §YEmpire§! of $NAME$ rules [Root.GetName].\\nLong live them!;L'Empire;Das Reich;;El Imperio;;;;;;;;;x\r\n"
    "only_code;$VAR$;$VAR$;$VAR$;;$VAR$;;;;;;;;;x\r\n"
    "pad_key;  Hello  ;Salut;Hallo;;Hola;;;;;;;;;x\r\n"
    "empty_en;;Bonjour;;;;;;;;;;;;x\r\n"
)


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class RoundTrip(unittest.TestCase):
    def test_bytes_identicos(self):
        casos = {
            "crlf_cp1252": SAMPLE.encode("cp1252"),
            "lf_sem_quebra_final": SAMPLE.replace("\r\n", "\n").rstrip("\n").encode("cp1252"),
            "eol_misto": b"a;b;c\r\nd;e;f\ng;h;i\r\n",
            "byte_invalido_cp1252": b"k;caf\x81 \xe9;x\r\n",
            "utf8_bom": b"\xef\xbb\xbf" + "k;café 中;x\n".encode("utf-8"),
            "vazio": b"",
        }
        with tempfile.TemporaryDirectory() as d:
            for nome, data in casos.items():
                p = write_bytes(Path(d) / f"{nome}.csv", data)
                self.assertEqual(LocFile.read(p).to_bytes(), data, nome)

    def test_encoding_detectado(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(LocFile.read(write_bytes(Path(d) / "a.csv", SAMPLE.encode("cp1252"))).encoding, "cp1252")
            self.assertEqual(LocFile.read(write_bytes(Path(d) / "b.csv", b"\xef\xbb\xbfk;\xc3\xa9;x")).encoding, "utf-8-sig")
            self.assertEqual(LocFile.read(write_bytes(Path(d) / "c.csv", "k;é;x".encode("utf-8"))).encoding, "utf-8")

    def test_colunas_do_cabecalho(self):
        with tempfile.TemporaryDirectory() as d:
            loc = LocFile.read(write_bytes(Path(d) / "a.csv", SAMPLE.encode("cp1252")))
            self.assertEqual(loc.columns, {"english": 1, "french": 2, "german": 3, "spanish": 5})

    def test_set_cell_so_muda_a_celula(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_bytes(Path(d) / "a.csv", SAMPLE.encode("cp1252"))
            loc = LocFile.read(p)
            ln = next(l for l in loc.entries() if l.key == "pad_key")
            loc.set_cell(ln, 1, "Olá")
            out = loc.to_bytes().decode("cp1252")
            self.assertIn("pad_key;Olá;Salut;Hallo;;Hola;;;;;;;;;x\r\n", out)
            self.assertEqual(out.count("\r\n"), SAMPLE.count("\r\n"))
            with self.assertRaises(ValueError):
                loc.set_cell(ln, 1, "a;b")
            with self.assertRaises(UnicodeEncodeError):
                loc.set_cell(ln, 1, "中")


class Protecao(unittest.TestCase):
    def test_mask_unmask(self):
        src = "The §YEmpire§! of $NAME$ rules [Root.GetName].\\n£gold£ {x}"
        masked, tokens = mask(src)
        self.assertNotIn("$", masked)
        self.assertNotIn("§", masked)
        self.assertEqual(unmask(masked, tokens), src)
        self.assertEqual(len(tokens), 7)

    def test_fit_cp1252(self):
        self.assertEqual(fit_cp1252("ação não é à toa"), "ação não é à toa")
        self.assertEqual(fit_cp1252("a → b"), "a -> b")
        fit_cp1252("中").encode("cp1252")


class DropBackend(Backend):
    model_id = "drop"

    def chat(self, system, user):
        return "tradução sem marcadores"


class FenceBackend(Backend):
    model_id = "fence"

    def chat(self, system, user):
        import re
        t = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        return '<think>hmm</think>```\n"Tradução: ' + t + '; ok"\n```'


class TradutorTests(unittest.TestCase):
    def test_falha_mantem_original(self):
        t = Translator(DropBackend(), self.enterContext(Cache()), max_attempts=2)
        r = t.translate("Hello $NAME$")
        self.assertEqual((r.status, r.text), ("falha", "Hello $NAME$"))

    def test_limpeza_e_ponto_e_virgula(self):
        r = Translator(FenceBackend(), self.enterContext(Cache())).translate("Hello [Root.GetName]")
        self.assertEqual(r.status, "ok")
        self.assertNotIn(";", r.text)
        self.assertIn("[Root.GetName]", r.text)
        self.assertNotIn("think", r.text)

    def test_cache_e_espacos(self):
        t = Translator(EchoBackend(), self.enterContext(Cache()))
        a, b = t.translate("  Hello  "), t.translate("  Hello  ")
        self.assertEqual((a.status, b.status), ("ok", "cache"))
        self.assertEqual(a.text, "  PT> Hello  ")

    def test_so_codigo_e_pulado(self):
        self.assertEqual(Translator(EchoBackend(), self.enterContext(Cache())).translate("$VAR$ §Y§!").status, "skip")


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.orig = SAMPLE.encode("cp1252")
        write_bytes(d / "meumod" / "localisation" / "a.csv", self.orig)
        write_bytes(d / "meumod" / "localisation" / "so_comentario.csv", b"# nada\r\n")
        self.cfg = d / "config.toml"
        self.cfg.write_text(
            '[model]\nbackend = "echo"\n[translate]\noutput_dir = "saida"\ncache = "c.sqlite"\n'
            f'[[mods]]\nname = "Meu Mod"\npath = \'{d / "meumod"}\'\n', encoding="utf-8")
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_overlay_completo(self):
        self.assertEqual(ck2tr.main(["translate", "--config", str(self.cfg)]), 0)
        saida = self.d / "saida"
        novo = (saida / "meu_mod_ptbr" / "localisation" / "a.csv")
        self.assertTrue(novo.exists())
        self.assertFalse((saida / "meu_mod_ptbr" / "localisation" / "so_comentario.csv").exists())
        self.assertEqual((self.d / "meumod" / "localisation" / "a.csv").read_bytes(), self.orig)  # original intacto
        mod = (saida / "meu_mod_ptbr.mod").read_text(encoding="cp1252")
        self.assertIn('dependencies = { "Meu Mod" }', mod)
        self.assertIn('path = "mod/meu_mod_ptbr"', mod)

        loc = LocFile.read(novo)
        por_chave = {l.key: l.fields for l in loc.entries()}
        self.assertTrue(por_chave["title_empire"][1].startswith("PT> The §YEmpire§! of $NAME$"))
        self.assertEqual(por_chave["title_empire"][2:4], ["L'Empire", "Das Reich"])   # outras colunas intactas
        self.assertEqual(por_chave["only_code"][1], "$VAR$")                          # só código: pulado
        self.assertEqual(por_chave["pad_key"][1], "  PT> Hello  ")
        self.assertEqual(por_chave["empty_en"][1], "")
        self.assertEqual(ck2tr.main(["verify", "--config", str(self.cfg)]), 0)

    def test_verify_detecta_adulteracao(self):
        ck2tr.main(["translate", "--config", str(self.cfg)])
        novo = self.d / "saida" / "meu_mod_ptbr" / "localisation" / "a.csv"
        novo.write_bytes(novo.read_bytes().replace(b"L'Empire", b"MUDOU"))
        self.assertEqual(ck2tr.main(["verify", "--config", str(self.cfg)]), 1)

    def test_inplace_com_backup_e_trava(self):
        self.assertEqual(ck2tr.main(["translate", "--config", str(self.cfg), "--mode", "inplace"]), 0)
        backup = self.d / "saida" / "backup" / "meu_mod" / "localisation" / "a.csv"
        self.assertEqual(backup.read_bytes(), self.orig)
        self.assertNotEqual((self.d / "meumod" / "localisation" / "a.csv").read_bytes(), self.orig)
        with self.assertRaises(SystemExit):                       # não traduz português de novo
            ck2tr.main(["translate", "--config", str(self.cfg), "--mode", "inplace"])
        self.assertEqual(ck2tr.main(["verify", "--config", str(self.cfg), "--mode", "inplace"]), 0)

    def test_skip_files(self):
        txt = self.cfg.read_text(encoding="utf-8").replace(
            "[translate]\n", '[translate]\nskip_files = ["A.CSV"]\n')
        self.cfg.write_text(txt, encoding="utf-8")
        ck2tr.main(["translate", "--config", str(self.cfg)])
        self.assertFalse((self.d / "saida" / "meu_mod_ptbr" / "localisation" / "a.csv").exists())

    def test_todas_as_colunas(self):
        txt = self.cfg.read_text(encoding="utf-8").replace(
            "[translate]\n", '[translate]\nwrite_columns = ["english","french","german","spanish"]\n')
        self.cfg.write_text(txt, encoding="utf-8")
        ck2tr.main(["translate", "--config", str(self.cfg)])
        loc = LocFile.read(self.d / "saida" / "meu_mod_ptbr" / "localisation" / "a.csv")
        f = next(l.fields for l in loc.entries() if l.key == "title_empire")
        self.assertTrue(all(f[i].startswith("PT>") for i in (1, 2, 3, 5)))
        self.assertEqual(f[4], "")
        self.assertEqual(ck2tr.main(["verify", "--config", str(self.cfg)]), 0)


if __name__ == "__main__":
    unittest.main()
