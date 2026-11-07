"""Teste da proteção contra caracteres de outro idioma. Rode:  python -m unittest -v"""
import re
import unittest

from ck2translator.translator import Backend, Cache, Translator


class Vaza(Backend):
    model_id = "vaza"

    def __init__(self):
        self.n = 0

    def chat(self, system, user):
        self.n += 1
        t = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        return ("Em torno da圣地 " if self.n == 1 else "Em torno da terra santa ") + t


class GuardTests(unittest.TestCase):
    def test_rejeita_chines_e_tenta_de_novo(self):
        b = Vaza()
        r = Translator(b, self.enterContext(Cache())).translate("Holy [Root.GetName]")
        self.assertEqual((r.status, b.n), ("ok", 2))
        self.assertNotIn("圣", r.text)

    def test_original_com_chines_e_aceito(self):
        b = Vaza()
        r = Translator(b, self.enterContext(Cache())).translate("Say 圣地 now")
        self.assertEqual((r.status, b.n), ("ok", 1))


if __name__ == "__main__":
    unittest.main()
