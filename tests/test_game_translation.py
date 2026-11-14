"""Perfis no tradutor compartilhado, sem serviços externos ou modelos instalados."""
import hashlib
import json
import re
import unittest

from ck2translator.ck2loc import EU4_TOKEN_RE, TOKEN_RE
from ck2translator.translator import Backend, Cache, PROMPT_VERSION, Translator, mask, unmask


class RecordingBackend(Backend):
    model_id = "ollama:test"

    def __init__(self, prefix="PT ", *, prompt=True):
        self.prefix = prefix
        self.uses_prompt = prompt
        self.calls = []

    def chat(self, system, user):
        self.calls.append((system, user))
        return self.prefix + re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)


class GameTranslationTests(unittest.TestCase):
    def test_ck2_legacy_cache_key_and_prompt_are_preserved(self):
        backend = RecordingBackend()
        translator = Translator(backend, self.enterContext(Cache()), context="Uma dinastia")
        glossary, keep = [("King", "Rei")], ["Sigmar"]
        legacy_blob = json.dumps(
            [PROMPT_VERSION, backend.model_id, "King {1}", "Uma dinastia", glossary, keep],
            ensure_ascii=False,
        )
        expected = hashlib.sha256(legacy_blob.encode("utf-8")).hexdigest()
        self.assertEqual(translator._key("King {1}", glossary, keep), expected)
        self.assertEqual(
            translator._system([], []).splitlines()[0],
            "Você é um tradutor profissional de jogos, especialista em Crusader Kings II "
            "(Europa medieval, dinastias, política). Traduza do inglês para o português do Brasil.",
        )

    def test_eu4_prompt_and_cache_have_their_own_profile(self):
        cache = self.enterContext(Cache())
        ck2_backend, eu4_backend = RecordingBackend("CK2 "), RecordingBackend("EU4 ")
        ck2 = Translator(ck2_backend, cache)
        eu4 = Translator(eu4_backend, cache, game="eu4", reuse_from="auto")
        self.assertEqual(ck2.translate("King").text, "CK2 King")
        self.assertEqual(eu4.translate("King").text, "EU4 King")
        self.assertEqual(eu4.translate("King").status, "cache")
        self.assertEqual(len(eu4_backend.calls), 1)
        self.assertIn("Europa Universalis IV", eu4_backend.calls[0][0])
        self.assertNotIn("Crusader Kings II", eu4_backend.calls[0][0])

    def test_cache_reuse_between_backends_is_scoped_to_the_game(self):
        cache = self.enterContext(Cache())
        first = RecordingBackend("FIRST ")
        first.model_id = "argos"
        first.uses_prompt = False
        Translator(first, cache).translate("King")
        second = RecordingBackend("SECOND ")
        eu4 = Translator(second, cache, game="eu4", reuse_from="auto")
        self.assertEqual(eu4.translate("King").text, "SECOND King")
        self.assertEqual(eu4.reused, 0)
        Translator(first, cache, game="eu4").translate("Queen")
        eu4 = Translator(second, cache, game="eu4", reuse_from="auto")
        self.assertEqual(eu4.translate("Queen").text, "FIRST Queen")
        self.assertEqual(eu4.reused, 1)
        self.assertEqual(len(second.calls), 1)

    def test_eu4_codes_and_escapes_never_reach_the_backend(self):
        source = r'Hello @ENG @[Root.GetTag] $VALUE|Y$ [Root.GetName] §Yworld§! £adm£\n\t\"quoted\" \\ done'
        for prompt in (True, False):
            with self.subTest(prompt=prompt):
                backend = RecordingBackend(prompt=prompt)
                translator = Translator(backend, self.enterContext(Cache()), game="eu4")
                result = translator.translate(source)
                self.assertEqual((result.status, result.text), ("ok", "PT " + source))
                masked_user = backend.calls[0][1]
                for token in EU4_TOKEN_RE.findall(source):
                    self.assertNotIn(token, masked_user)

    def test_mask_supports_profile_specific_tokens_without_changing_ck2(self):
        source = r'Hello @FRA \"king\" \\ next'
        self.assertEqual(mask(source), (source, []))
        masked, tokens = mask(source, token_re=EU4_TOKEN_RE)
        self.assertEqual(tokens, ["@FRA", '\\"', '\\"', "\\\\"])
        self.assertEqual(unmask(masked, tokens), source)
        self.assertIsNone(TOKEN_RE.search(source))

    def test_eu4_glossary_and_keep_work_alongside_flags(self):
        backend = RecordingBackend(prompt=False)
        result = Translator(
            backend, self.enterContext(Cache()), {"king": "rei"}, ["Burgundy"], game="eu4",
        ).translate("King of Burgundy @BUR")
        self.assertEqual((result.status, result.text), ("ok", "PT Rei of Burgundy @BUR"))
        self.assertNotIn("King", backend.calls[0][1])
        self.assertNotIn("Burgundy", backend.calls[0][1])

    def test_eu4_semicolons_survive_fresh_and_cached_translations(self):
        cache = self.enterContext(Cache())
        backend = RecordingBackend()
        eu4 = Translator(backend, cache, game="eu4")
        self.assertEqual(eu4.translate("Hello; world").text, "PT Hello; world")
        cached = eu4.translate("Hello; world")
        self.assertEqual((cached.status, cached.text), ("cache", "PT Hello; world"))
        self.assertEqual(Translator(backend, cache).translate("Hello; world").text, "PT Hello, world")

    def test_eu4_rejects_invented_flags_and_escapes(self):
        for prefix in ("@FRA ", '\\"'):
            with self.subTest(prefix=prefix):
                backend = RecordingBackend(prefix)
                result = Translator(
                    backend, self.enterContext(Cache()), game="eu4", max_attempts=1,
                ).translate("Hello")
                self.assertEqual((result.status, result.text), ("falha", "Hello"))
                self.assertIn("inventou códigos", result.note)

    def test_eu4_paranoid_skips_flags(self):
        backend = RecordingBackend()
        result = Translator(
            backend, self.enterContext(Cache()), game="eu4", paranoid=True,
        ).translate("Hello @ENG")
        self.assertEqual((result.status, result.text), ("skip", "Hello @ENG"))
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
