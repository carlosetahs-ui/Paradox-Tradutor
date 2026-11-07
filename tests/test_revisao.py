"""Testes da pós-edição (pt-PT -> pt-BR, substituicoes.txt), de status, package e avisos. Rode:  python -m unittest -v"""
import contextlib
import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from ck2translator import cli as ck2tr, revisao
from ck2translator.translator import Backend, Cache, Translator


class PT(Backend):
    model_id = "translators"
    uses_prompt = False

    def __init__(self, resposta):
        self.resposta, self.n = resposta, 0

    def chat(self, system, user):
        self.n += 1
        return self.resposta


class Regras(unittest.TestCase):
    def test_vocabulario_e_capitalizacao(self):
        self.assertEqual(revisao.ptbr("Partilha o ecrã. ACTUALMENTE: Equipa."), "Partilha a tela. ATUALMENTE: Equipe.")

    def test_nao_mexe_em_parte_de_palavra(self):
        self.assertEqual(revisao.ptbr("factoria e equipamento"), "factoria e equipamento")

    def test_gerundio(self):
        self.assertEqual(revisao.ptbr("Ele está a fazer e estão a ver. Está a mar alto."),
                         "Ele está fazendo e estão vendo. Está a mar alto.")

    def test_pronome_antes_do_verbo(self):
        self.assertEqual(revisao.ptbr("Os fiéis impressionam-me."), "Os fiéis me impressionam.")
        self.assertEqual(revisao.ptbr("O pai deu-me o trono."), "O pai me deu o trono.")
        for igual in ("Impressionam-me muito.", "Diga-me tudo.", "Apanhem-nos, rapazes!", "Se juntei-me ao grupo"):
            self.assertEqual(revisao.ptbr(igual), igual)      # começo de frase, imperativo, 1ª pessoa: não mexe

    def test_nao_estraga_nomes_com_nos_me_te(self):
        for t in ("Nos Estados Unidos.", "Te Deum e Me Too", "Os Nos Altos"):
            self.assertEqual(revisao.ptbr(t), t)

    def test_substituicoes_do_usuario_vencem(self):
        post = revisao.build(True, {"Rastafarian": "rastafári", "equipa": "turma"})
        self.assertEqual(post("A Rastafarian equipa"), "A Rastafári turma")

    def test_desligado(self):
        self.assertIsNone(revisao.build(False, {}))
        self.assertEqual(revisao.build(False, {"a": "b"})("a casa"), "b casa")


class NoTradutor(unittest.TestCase):
    def test_codigos_keep_e_glossario_ficam_intactos(self):
        # O serviço devolve pt-PT; a pós-edição roda sobre "{n}", então nada protegido é tocado.
        b = PT("O {1} está a partilhar {2} com {3} e [facto].")
        t = Translator(b, self.enterContext(Cache()), {"Equipa": "Equipa"}, ["Facto"], post=revisao.build(True, {}))
        r = t.translate("The $NAME$ shares Equipa with Facto")
        self.assertEqual(r.status, "falha")      # '[facto]' inventado: rejeitado como sempre

        b = PT("O {1} está a partilhar {2} com {3}.")
        t = Translator(b, self.enterContext(Cache()), {"Equipa": "Equipa"}, ["Facto"], post=revisao.build(True, {}))
        r = t.translate("The $NAME$ shares Equipa with Facto")
        self.assertEqual(r.text, "O $NAME$ está compartilhando Equipa com Facto.")

    def test_pos_edicao_vale_tambem_no_cache_sem_retraduzir(self):
        c, b = self.enterContext(Cache()), PT("Está a partilhar o ecrã")
        Translator(b, c).translate("Sharing the screen")              # grava o texto bruto
        r = Translator(b, c, post=revisao.build(True, {})).translate("Sharing the screen")
        self.assertEqual((r.status, r.text, b.n), ("cache", "Está compartilhando a tela", 1))

    def test_dry_nao_chama_o_backend(self):
        b = PT("x")
        r = Translator(b, self.enterContext(Cache())).translate("Hello", dry=True)
        self.assertEqual((r.status, b.n), ("pendente", 0))


HEADER = "#CODE;ENGLISH;FRENCH;GERMAN;;SPANISH;;;;;;;;;x\r\n"


class Comandos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        for mod in ("a", "b"):
            (self.d / mod / "localisation").mkdir(parents=True)
            (self.d / mod / "localisation" / "same.csv").write_bytes(
                (HEADER + f"k_{mod};Hello world {mod};;;;;;;;;;;;;x\r\n").encode("cp1252"))
        self.cfg = self.d / "config.toml"
        self.cfg.write_text('[model]\nbackend="echo"\n[translate]\nmodee="overlay"\n'
                            '[[mods]]\nname="A"\npath="a"\n[[mods]]\nname="B"\npath="b"\n', encoding="utf-8")

    def run_cmd(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ck2tr.main(list(args))
        return rc, buf.getvalue()

    def test_aviso_de_chave_com_erro_de_digitacao(self):
        _, out = self.run_cmd("scan", "--config", str(self.cfg))
        self.assertIn("'modee'", out)
        self.assertIn("Você quis dizer 'mode'", out)

    def test_scan_avisa_arquivo_com_mesmo_nome(self):
        _, out = self.run_cmd("scan", "--config", str(self.cfg))
        self.assertIn("MESMO NOME", out)
        self.assertIn("same.csv: A, B", out)

    def test_status_e_package(self):
        _, out = self.run_cmd("status", "--config", str(self.cfg))
        self.assertIn("faltam 1", out)
        self.run_cmd("translate", "--config", str(self.cfg))
        _, out = self.run_cmd("status", "--config", str(self.cfg))
        self.assertIn("faltam 0", out)
        self.assertIn("Tudo já está no cache", out)
        _, out = self.run_cmd("package", "--config", str(self.cfg), "--mod", "A")
        with zipfile.ZipFile(self.d / "saida" / "a_ptbr.zip") as z:
            nomes = z.namelist()
            self.assertIn("a_ptbr.mod", nomes)
            self.assertIn("a_ptbr/localisation/same.csv", nomes)
            self.assertIn("LEIA-ME.txt", nomes)
            self.assertIn("dependencies", z.read("a_ptbr.mod").decode("cp1252"))

    def test_substituicoes_txt_e_ptbr_fix_via_config(self):
        (self.d / "substituicoes.txt").write_text("Hello = Olá\n", encoding="utf-8")
        cfg = ck2tr.load_config(str(self.cfg))
        self.assertEqual(cfg["replacements"], {"Hello": "Olá"})
        self.assertTrue(cfg["translate"]["ptbr_fix"])


if __name__ == "__main__":
    unittest.main()
