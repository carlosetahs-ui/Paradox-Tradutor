"""Regressões de termos entre códigos e segurança da pós-edição."""
import re
import unittest

from ck2translator import revisao
from ck2translator.ck2loc import EU4_TOKEN_RE
from ck2translator.translator import Backend, Cache, Translator, mask, unmask


class RecordingBackend(Backend):
    model_id = "review"

    def __init__(self, *, prompt=False):
        self.uses_prompt = prompt
        self.calls = []

    def chat(self, system, user):
        self.calls.append((system, user))
        return "PT " + re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)


class TranslationReviewTests(unittest.TestCase):
    def test_eu4_glossary_and_keep_match_immediately_after_color_codes(self):
        backend = RecordingBackend()
        translator = Translator(
            backend, self.enterContext(Cache()), {"empire": "império"}, ["Burgundy"], game="eu4",
        )
        source = r"The §YEmpire§! of §GBurgundy§!\nEmpire and $EMPIRE$"
        result = translator.translate(source)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, r"PT The §YImpério§! of §GBurgundy§!\nImpério and $EMPIRE$")
        self.assertNotIn("Empire", backend.calls[0][1])
        self.assertNotIn("Burgundy", backend.calls[0][1])
        self.assertNotIn("$EMPIRE$", backend.calls[0][1])
        self.assertEqual(translator.translate(source).status, "cache")

    def test_eu4_colored_glossary_only_needs_no_backend(self):
        backend = RecordingBackend()
        translator = Translator(backend, self.enterContext(Cache()), {"Empire": "Império"}, game="eu4")
        result = translator.translate("§YEmpire§!")
        self.assertEqual((result.status, result.text), ("ok", "§YImpério§!"))
        self.assertEqual(backend.calls, [])

    def test_eu4_prompt_receives_visible_terms_but_ignores_code_contents(self):
        translator = Translator(
            RecordingBackend(prompt=True), self.enterContext(Cache()),
            {"Empire": "Império", "CODE": "Código"}, ["Burgundy", "NAME"], game="eu4",
        )
        self.assertEqual(
            translator._hits("§YEmpire§! Burgundy $CODE$ [NAME]"),
            ([("Empire", "Império")], ["Burgundy"]),
        )

    def test_eu4_matching_remains_whole_word_and_protects_all_codes(self):
        source = "§YKingdom§! §YKing§! $KING$"
        masked, tokens = mask(source, glossary=[("King", "Rei")], token_re=EU4_TOKEN_RE)
        self.assertEqual(unmask(masked, tokens), "§YKingdom§! §YRei§! $KING$")
        self.assertIn("Kingdom", masked)
        self.assertNotIn("$KING$", masked)

    def test_ck2_colored_matching_and_mask_are_unchanged(self):
        translator = Translator(RecordingBackend(), self.enterContext(Cache()), {"Empire": "Império"})
        self.assertEqual(translator._hits("§YEmpire§!"), ([], []))
        self.assertEqual(mask("§YEmpire§!", glossary=[("Empire", "Império")]),
                         ("{1}Empire{2}", ["§Y", "§!"]))

    def test_invalid_user_postprocessing_keeps_validated_translation(self):
        for game in ("ck2", "eu4"):
            with self.subTest(game=game):
                backend = RecordingBackend()
                translator = Translator(
                    backend, self.enterContext(Cache()), game=game,
                    post=revisao.build(False, {"world": "{99}"}),
                )
                result = translator.translate("Hello $NAME$ world")
                self.assertEqual((result.status, result.text), ("ok", "PT Hello $NAME$ world"))

    def test_invalid_postprocessing_is_safe_on_cache_hits_and_backend_reuse(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                cache = self.enterContext(Cache())
                first = RecordingBackend()
                first.model_id = "argos"
                Translator(first, cache, game="eu4").translate("Hello $NAME$ world")
                backend = RecordingBackend()
                backend.model_id = "other" if reuse else "argos"
                translator = Translator(
                    backend, cache, game="eu4", reuse_from="auto" if reuse else [],
                    post=revisao.build(False, {"world": "{99}"}),
                )
                result = translator.translate("Hello $NAME$ world")
                self.assertEqual((result.status, result.text), ("cache", "PT Hello $NAME$ world"))
                self.assertEqual(backend.calls, [])
                self.assertEqual(translator.reused, int(reuse))

    def test_postprocessing_cannot_drop_or_invent_game_tokens(self):
        for post in (lambda text: text.replace("{1}", ""), lambda text: "$NEW$ " + text):
            with self.subTest(post=post):
                translator = Translator(
                    RecordingBackend(), self.enterContext(Cache()), game="eu4", post=post,
                )
                result = translator.translate("Hello $NAME$")
                self.assertEqual(result.text, "PT Hello $NAME$")


if __name__ == "__main__":
    unittest.main()
