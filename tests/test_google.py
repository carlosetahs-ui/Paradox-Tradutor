"""Testes do backend Google (sem rede). Rode:  python -m unittest -v"""
import unittest

from ck2translator.translator import BackendError, Cache, GoogleBackend, Translator


def backend(fn):
    return GoogleBackend(translate_fn=fn, pause=(0, 0), long_pause=0, retry_wait=0)


class GoogleTests(unittest.TestCase):
    def test_codigos_e_termos_protegidos(self):
        vistos = []

        def fn(t):
            vistos.append(t)
            return "Louvem " + t.replace("Praise", "").strip()

        r = Translator(backend(fn), self.enterContext(Cache()), keep=["Sigmar"]).translate("Praise Sigmar, $NAME$ [Root.GetName]!")
        self.assertEqual(r.status, "ok")
        self.assertNotIn("Sigmar", vistos[0])       # o Google nunca vê o termo nem os códigos
        self.assertNotIn("$NAME$", vistos[0])
        self.assertEqual(r.text, "Louvem Sigmar, $NAME$ [Root.GetName]!")

    def test_google_perde_codigo_vira_falha(self):
        r = Translator(backend(lambda t: "sem codigos"), self.enterContext(Cache())).translate("Hello $NAME$")
        self.assertEqual((r.status, r.text), ("falha", "Hello $NAME$"))

    def test_marcador_com_espaco_e_minuscula(self):
        r = Translator(backend(lambda t: t.replace("XX1XX", "xx 1 xx") + " ola"), self.enterContext(Cache())).translate("Hi $A$")
        self.assertEqual(r.text, "Hi $A$ ola")

    def test_bloqueio_para_tudo(self):
        def fn(t):
            raise RuntimeError("429 too many requests")
        with self.assertRaises(BackendError):
            Translator(backend(fn), self.enterContext(Cache())).translate("Hello")


if __name__ == "__main__":
    unittest.main()


from ck2translator.translator import Backend


class FakeLocal(Backend):
    model_id = "ollama:fake"

    def __init__(self):
        self.calls = 0

    def chat(self, system, user):
        import re
        self.calls += 1
        return "LOCAL " + re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)


class ReuseTests(unittest.TestCase):
    def test_ollama_reaproveita_google(self):
        cache = self.enterContext(Cache())
        g = Translator(backend(lambda t: "Louvem " + t), cache, keep=["Sigmar"], context="ctx")
        a = g.translate("Praise Sigmar, $NAME$!")
        local = FakeLocal()
        t = Translator(local, cache, keep=["Sigmar"], context="ctx", reuse_from=["google"])
        b = t.translate("Praise Sigmar, $NAME$!")
        self.assertEqual((b.status, b.text, local.calls, t.reused), ("cache", a.text, 0, 1))
        self.assertEqual(t.translate("Other text").status, "ok")          # o que falta vai ao modelo
        self.assertEqual(local.calls, 1)

    def test_google_reaproveita_ollama(self):
        cache = self.enterContext(Cache())
        a = Translator(FakeLocal(), cache).translate("Hello [Root.GetName]")
        chamadas = []
        g = Translator(backend(lambda t: chamadas.append(t) or t), cache, reuse_from=["ollama:fake"])
        b = g.translate("Hello [Root.GetName]")
        self.assertEqual((b.text, chamadas), (a.text, []))

    def test_sem_reuse_nao_mistura(self):
        cache = self.enterContext(Cache())
        Translator(backend(lambda t: "G " + t), cache).translate("Hello")
        local = FakeLocal()
        Translator(local, cache).translate("Hello")
        self.assertEqual(local.calls, 1)
