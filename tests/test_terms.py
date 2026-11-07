"""Teste do comando 'terms'. Rode:  python -m unittest -v"""
import unittest

from ck2translator.cli import suggest_terms


class TermsTests(unittest.TestCase):
    def test_acha_nomes_proprios(self):
        textos = [f"The army of Karak Azul marched on day {i}, and the dwarves of Karak Azul held. The king spoke to Sigmar."
                  for i in range(8)] + [f"A king and the army rest {i}. The army is large." for i in range(8)]
        achados = dict(suggest_terms(textos, min_count=5))
        self.assertIn("Karak Azul", achados)
        self.assertIn("Sigmar", achados)
        self.assertNotIn("The", achados)
        self.assertNotIn("King", achados)

    def test_codigos_do_jogo_ignorados(self):
        achados = dict(suggest_terms([f"Hello [Root.GetName] and $NAME$ in Altdorf{'.' * 1} {i}" for i in range(6)], 5))
        self.assertIn("Altdorf", achados)
        self.assertFalse(any("Root" in k or "NAME" in k for k in achados))


if __name__ == "__main__":
    unittest.main()
