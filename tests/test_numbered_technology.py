"""Títulos de séries tecnológicas compartilham a mesma tradução e preservam o nível."""
import re
import unittest

from ck2translator.translator import Backend, Cache, Translator


class TechnologyBackend(Backend):
    model_id = "technology-test"
    uses_prompt = False

    def __init__(self):
        self.calls = []

    def chat(self, system, user):
        text = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        self.calls.append(text)
        return text.replace("Administrative Technology", "Tecnologia Administrativa")


class NumberedTechnologyTests(unittest.TestCase):
    def test_levels_reuse_one_base_translation_and_stay_at_the_end(self):
        backend = TechnologyBackend()
        translator = Translator(backend, self.enterContext(Cache()), game="eu4")
        for level in ("I", "II", "CXCIX", "200"):
            result = translator.translate(f"  Administrative Technology {level}  ")
            self.assertEqual(result.text, f"  Tecnologia Administrativa {level}  ")
            self.assertEqual(result.status, "ok" if level == "I" else "cache")
        self.assertEqual(backend.calls, ["Administrative Technology"])

    def test_glossary_base_translates_all_three_series_without_model(self):
        terms = {"Administrative Technology": "Tecnologia Administrativa",
                 "Diplomatic Technology": "Tecnologia Diplomática",
                 "Military Technology": "Tecnologia Militar"}
        backend = TechnologyBackend()
        translator = Translator(backend, self.enterContext(Cache()), terms, game="eu4")
        for source, target in terms.items():
            self.assertEqual(translator.translate(source + " XIV").text, target + " XIV")
        self.assertEqual(backend.calls, [])

    def test_whole_title_glossary_and_keep_take_precedence(self):
        backend = TechnologyBackend()
        translator = Translator(backend, self.enterContext(Cache()),
                                {"Administrative Technology I": "Fundamentos de Administração"},
                                ["Military Technology I"], game="eu4")
        self.assertEqual(translator.translate("Administrative Technology I").text,
                         "Fundamentos de Administração")
        self.assertEqual(translator.translate("Military Technology I").status, "skip")
        self.assertEqual(backend.calls, [])

    def test_dry_run_and_failed_base_keep_the_complete_original(self):
        backend = TechnologyBackend()
        translator = Translator(backend, self.enterContext(Cache()), game="eu4")
        source = "Administrative Technology XX"
        result = translator.translate(source, dry=True)
        self.assertEqual((result.status, result.text), ("pendente", source))
        self.assertEqual(backend.calls, [])
        backend.chat = lambda system, user: ""
        result = translator.translate(source)
        self.assertEqual((result.status, result.text), ("falha", source))

    def test_terms_crossing_title_and_level_are_not_lost_to_template(self):
        backend = TechnologyBackend()
        translator = Translator(backend, self.enterContext(Cache()),
                                {"Technology III": "Tecnologia Antiga"},
                                ["Technology IV"], game="eu4")
        self.assertIn("Tecnologia Antiga", translator.translate("Military Technology III").text)
        self.assertIn("Technology IV", translator.translate("Military Technology IV").text)
        self.assertNotIn("Military Technology", backend.calls)

    def test_regular_sentences_and_descriptions_are_not_templates(self):
        backend = TechnologyBackend()
        translator = Translator(backend, self.enterContext(Cache()), game="eu4")
        for text in ("Administrative Technology II is our next step.", "The army gains 2.5",
                     "Year 1444", "Administrative Technology I Extra"):
            translator.translate(text)
        self.assertTrue(all(text in backend.calls for text in (
            "Administrative Technology II is our next step.", "The army gains 2.5",
            "Year 1444", "Administrative Technology I Extra")))


if __name__ == "__main__":
    unittest.main()
