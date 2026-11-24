"""Testes do backend Argos e do reaproveitamento 'auto' do cache (sem rede). Rode:  python -m unittest -v"""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ck2translator.translator import ArgosBackend, Backend, Cache, GoogleBackend, Translator, _prepare_argos_stanza


def argos(fn):
    return ArgosBackend(translate_fn=fn)


class Local(Backend):
    model_id = "ollama:fake"

    def __init__(self, resposta=None):
        self.calls, self.resposta = 0, resposta

    def chat(self, system, user):
        self.calls += 1
        t = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        return self.resposta(t) if self.resposta else "LOCAL " + t


class ArgosTests(unittest.TestCase):
    def test_stanza_loads_packaged_resources_without_downloading(self):
        class CachedTranslation(SimpleNamespace):
            pass

        class CompositeTranslation(SimpleNamespace):
            pass

        class StanzaSentencizer(SimpleNamespace):
            pass

        package_path = Path("installed-model")
        segmenter = StanzaSentencizer(stanza_pipeline=None, stanza_lang_code="en",
                                      pkg=SimpleNamespace(package_path=package_path))
        leg = CachedTranslation(underlying=SimpleNamespace(sentencizer=segmenter))
        route = CompositeTranslation(t1=leg, t2=SimpleNamespace())
        argos_module = SimpleNamespace(CachedTranslation=CachedTranslation, CompositeTranslation=CompositeTranslation,
                                       StanzaSentencizer=StanzaSentencizer, settings=SimpleNamespace(device="cpu"))
        stanza = SimpleNamespace(Pipeline=mock.Mock())
        with mock.patch.dict("sys.modules", {"stanza": stanza}):
            _prepare_argos_stanza(route, argos_module)
            _prepare_argos_stanza(route, argos_module)
        stanza.Pipeline.assert_called_once_with(lang="en", dir=str(package_path / "stanza"), processors="tokenize",
                                               use_gpu=False, logging_level="WARNING", download_method=None)
        self.assertIs(segmenter.stanza_pipeline, stanza.Pipeline.return_value)

    def test_marcadores_preservados(self):
        r = Translator(argos(lambda t: "PT " + t), self.enterContext(Cache()), keep=["Sigmar"]).translate("Praise Sigmar, $NAME$!")
        self.assertEqual((r.status, r.text), ("ok", "PT Praise Sigmar, $NAME$!"))

    def test_plano_b_quando_o_motor_destroi_marcadores(self):
        vistos = []

        def fn(t):
            vistos.append(t)
            return re.sub(r"XX\d+XX", "", t) if "XX" in t else "PT " + t     # joga os marcadores fora

        r = Translator(argos(fn), self.enterContext(Cache())).translate("Hello $NAME$ and [Root.GetName] now")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.text, "PT Hello $NAME$ PT and [Root.GetName] PT now")
        self.assertTrue(all("$" not in v and "[" not in v for v in vistos))   # o motor nunca vê os códigos

    def test_texto_longo_e_dividido(self):
        chamadas = []
        t = ("Uma frase. " * 400).strip()
        ArgosBackend(translate_fn=lambda x: chamadas.append(len(x)) or x).chat("", f"<texto>\n{t}\n</texto>")
        self.assertGreater(len(chamadas), 1)


class ReuseAutoTests(unittest.TestCase):
    def test_todos_os_sentidos(self):
        cache = self.enterContext(Cache())
        g = Translator(GoogleBackend(translate_fn=lambda t: "G " + t, pause=(0, 0), long_pause=0), cache, keep=["Sigmar"])
        a = Translator(argos(lambda t: "A " + t), cache, keep=["Sigmar"], reuse_from="auto")
        g.translate("Praise Sigmar $X$")                       # google traduz
        r = a.translate("Praise Sigmar $X$")                   # argos aproveita
        self.assertTrue(r.text.startswith("G "))
        self.assertEqual(a.reused, 1)
        a2 = Translator(argos(lambda t: "A " + t), cache, reuse_from="auto")
        a2.translate("Only argos")                              # argos traduz
        o = Local()
        t = Translator(o, cache, keep=["Sigmar"], reuse_from="auto")
        self.assertEqual(t.translate("Only argos").text, "A Only argos")      # ollama aproveita o argos
        self.assertEqual(t.translate("Praise Sigmar $X$").text, r.text)       # e o google
        self.assertEqual(o.calls, 0)

    def test_prioridade_da_lista(self):
        cache = self.enterContext(Cache())
        Translator(GoogleBackend(translate_fn=lambda t: "G " + t, pause=(0, 0), long_pause=0), cache).translate("Hello")
        Translator(argos(lambda t: "A " + t), cache).translate("Hello")
        quem = lambda ordem: Translator(Local(), cache, reuse_from=ordem).translate("Hello").text
        self.assertEqual(quem(["argos", "google"]), "A Hello")
        self.assertEqual(quem(["google", "argos"]), "G Hello")
        self.assertEqual(quem(["ollama:inexistente"]), "LOCAL Hello")

    def test_entrada_ruim_do_cache_e_ignorada(self):
        cache = self.enterContext(Cache())
        Translator(Local(lambda t: "Em torno da圣地 " + t), cache, max_attempts=1).translate("Holy land")  # falha: não entra
        # simula entrada antiga ruim gravada antes da proteção
        t0 = Translator(Local(), cache)
        masked = "Holy land"
        cache.put(t0._key(masked, [], [], "ollama:velho"), masked, "da圣地 terra", "ollama:velho")
        novo = Local()
        r = Translator(novo, cache, reuse_from="auto").translate("Holy land")
        self.assertEqual((r.status, novo.calls), ("ok", 1))
        self.assertNotIn("圣", r.text)

    def test_sem_reuse_por_padrao_no_translator(self):
        cache = self.enterContext(Cache())
        Translator(argos(lambda t: "A " + t), cache).translate("Hello")
        o = Local()
        Translator(o, cache).translate("Hello")
        self.assertEqual(o.calls, 1)


if __name__ == "__main__":
    unittest.main()
