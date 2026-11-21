"""Perda de texto entre cores e aspas incompletas após normalização."""
import unittest

from ck2translator import ck2loc, eu4loc
from ck2translator.translator import Backend, Cache, Translator


class FixedReply(Backend):
    uses_prompt = False
    model_id = "color-quote-regression"

    def __init__(self, text):
        self.text = text

    def chat(self, system, user):
        return self.text


class ColorQuoteRegressions(unittest.TestCase):
    def test_colored_words_cannot_disappear_behind_remaining_variable(self):
        source = "The §Yarmy of [Root.GetName]§! wins."
        translated = "O §Y[Root.GetName]§! vence."
        for game in ("ck2", "eu4"):
            with self.subTest(game=game), Cache() as cache:
                translator = Translator(FixedReply("O {1}{2}{3} vence."), cache, game=game)
                result = translator.translate(source)
                self.assertEqual((result.status, result.text), ("falha", source))
                self.assertEqual(cache.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
        for validator in (ck2loc.hard_problems, eu4loc.hard_problems):
            with self.subTest(validator=validator.__module__):
                self.assertTrue(any("desapareceu" in item for item in validator(source, translated)))

    def test_english_article_may_be_omitted_before_a_variable(self):
        for article in ("a", "an", "the", "The"):
            source = f"§Y{article} [Root.GetName]§! wins."
            translated = "§Y[Root.GetName]§! vence."
            for game in ("ck2", "eu4"):
                with self.subTest(article=article, game=game), Cache() as cache:
                    result = Translator(FixedReply("{1}{2}{3} vence."), cache, game=game).translate(source)
                    self.assertEqual((result.status, result.text), ("ok", translated))
            for validator in (ck2loc.hard_problems, eu4loc.hard_problems):
                with self.subTest(article=article, validator=validator.__module__):
                    self.assertEqual(validator(source, translated), [])

    def test_unpaired_quote_is_not_hidden_by_normalization(self):
        source = "See the king."
        for output in ("Veja «rei.", 'Veja "rei.'):
            for game in ("ck2", "eu4"):
                with self.subTest(output=output, game=game), Cache() as cache:
                    result = Translator(FixedReply(output), cache, game=game).translate(source)
                    self.assertEqual((result.status, result.text), ("falha", source))
                    self.assertEqual(cache.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)

    def test_balanced_quotes_and_concrete_single_quote_repair_are_accepted(self):
        for source, output, expected in (("See the king.", "Veja «o rei».", 'Veja "o rei".'),
                                         ("Say 'the king'.", "Diga 'o rei«.", "Diga 'o rei'.")):
            for game in ("ck2", "eu4"):
                with self.subTest(source=source, game=game), Cache() as cache:
                    translator = Translator(FixedReply(output), cache, game=game)
                    for status in ("ok", "cache"):
                        result = translator.translate(source)
                        self.assertEqual((result.status, result.text), (status, expected))


if __name__ == "__main__":
    unittest.main()
