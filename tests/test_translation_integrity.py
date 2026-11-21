"""Regressões de corrupção dos códigos, qualidade e memória de tradução, sem rede."""
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from ck2translator import ck2loc, eu4loc
from ck2translator.translator import (
    ArgosBackend, Backend, BackendError, Cache, Translator, mask, normalize_punct,
)


class Reply(Backend):
    uses_prompt = False
    model_id = "argos"

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.inputs = []

    def chat(self, system, user):
        self.calls += 1
        text = re.search(r"<texto>\n(.*?)\n</texto>", user, re.S).group(1)
        self.inputs.append(text)
        return self.response(text) if callable(self.response) else self.response


class TranslationIntegrityTests(unittest.TestCase):
    def translator(self, response, **options):
        cache = self.enterContext(Cache())
        backend = Reply(response)
        return Translator(backend, cache, **options), backend, cache

    def assert_cache_empty(self, cache):
        self.assertEqual(cache.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)

    def test_variable_contents_and_multiplicity_never_reach_backend(self):
        source = "$MONARCH$ greets [Root.GetName] and $MONARCH$."
        translator, backend, _ = self.translator("O {1} cumprimenta {2} e {3}.")
        result = translator.translate(source)
        self.assertEqual((result.status, result.text),
                         ("ok", "O $MONARCH$ cumprimenta [Root.GetName] e $MONARCH$."))
        self.assertEqual(backend.inputs, ["{1} greets {2} and {3}."])
        self.assertEqual(ck2loc.TOKEN_RE.findall(source), ck2loc.TOKEN_RE.findall(result.text))

    def test_corrupted_raw_variable_is_rejected_without_caching(self):
        for response in ("O {1} governa {2}, $ MONARCH$.", "O {1} governa {2} e [Root.Getname].",
                         "O {1} governa {1}."):
            with self.subTest(response=response):
                translator, _, cache = self.translator(response)
                result = translator.translate("$MONARCH$ rules [Root.GetName].")
                self.assertEqual(result.status, "falha")
                self.assert_cache_empty(cache)

    def test_missing_space_after_variable_is_restored(self):
        translator, _, _ = self.translator("{1}Além")
        result = translator.translate("[Root.GetName] Also")
        self.assertEqual((result.status, result.text), ("ok", "[Root.GetName] Além"))

    def test_missing_spaces_on_both_sides_of_variable_are_restored(self):
        translator, _, _ = self.translator("Parar{1}Violaste")
        result = translator.translate("Stop [From.GetInsultNameSelfEnd] You violated")
        self.assertEqual(result.text, "Parar [From.GetInsultNameSelfEnd] Violaste")

    def test_color_delimiters_remain_adjacent_to_their_contents(self):
        translator, _, _ = self.translator("Visite {1}Scarp Keep{2} agora.")
        result = translator.translate("Visit §YScarp Keep§! now.")
        self.assertEqual((result.status, result.text), ("ok", "Visite §YScarp Keep§! agora."))

    def test_legacy_standalone_section_signs_are_also_protected(self):
        translator, backend, _ = self.translator("{1}{2} Olá {3}{4} mundo {5}{6} agora")
        source = "§T§ Hello §W§ world §l§ now"
        result = translator.translate(source)
        self.assertEqual((result.status, result.text), ("ok", "§T§ Olá §W§ mundo §l§ agora"))
        self.assertNotIn("§", backend.inputs[0])
        self.assertEqual(ck2loc.hard_problems(source, result.text), [])

    def test_reordered_colors_or_deleted_colored_text_are_rejected(self):
        for output, message in (("Visite {2}Scarp Keep{1} agora.", "ordem"),
                                ("Visite {1}{2} agora.", "desapareceu")):
            with self.subTest(output=output):
                translator, _, cache = self.translator(output)
                source = "Visit §YScarp Keep§! now."
                result = translator.translate(source)
                self.assertEqual((result.status, result.text), ("falha", source))
                self.assertIn(message, result.note)
                self.assert_cache_empty(cache)

    def test_file_validators_reject_color_corruption_with_same_token_counts(self):
        for validator in (ck2loc.hard_problems, eu4loc.hard_problems):
            with self.subTest(validator=validator.__module__):
                source = "Visit §YScarp Keep§! now."
                self.assertTrue(any("ordem" in p for p in
                                    validator(source, "Visite §!Scarp Keep§Y agora.")))
                self.assertTrue(any("desapareceu" in p for p in
                                    validator(source, "Visite §Y§! agora.")))
                self.assertEqual(validator("§YTitle§W description", "§YTítulo§W descrição"), [])

    def test_invalid_current_and_reused_cache_entries_are_retranslated(self):
        source = "Visit §YScarp Keep§! now."
        for reused in (False, True):
            for bad in ("Visite {2}Scarp Keep{1} agora.", "Visite {1}{2} agora."):
                with self.subTest(reused=reused, bad=bad):
                    translator, backend, cache = self.translator(
                        "Visite {1}Scarp Keep{2} agora.", reuse_from=["google"] if reused else [],
                    )
                    masked, _ = mask(source)
                    model = "google" if reused else backend.model_id
                    key = translator._key(masked, [], [], model)
                    cache.put(key, source, bad, model)
                    self.assertEqual(translator.translate(source, dry=True).status, "pendente")
                    self.assertEqual(backend.calls, 0)
                    result = translator.translate(source)
                    self.assertEqual((result.status, result.text, backend.calls, translator.reused),
                                     ("ok", "Visite §YScarp Keep§! agora.", 1, 0))

    def test_post_edit_cannot_reorder_color_markers(self):
        translator, backend, _ = self.translator(
            "Visite {1}Scarp Keep{2} agora.",
            post=lambda text: "Visite {2}Scarp Keep{1} agora.",
        )
        source = "Visit §YScarp Keep§! now."
        for status in ("ok", "cache"):
            result = translator.translate(source)
            self.assertEqual((result.status, result.text), (status, "Visite §YScarp Keep§! agora."))
        self.assertEqual(backend.calls, 1)

    def test_unchanged_english_prose_and_new_repetitions_are_not_cached(self):
        cases = (("The soldiers will return to their home.", lambda text: text, "sem tradução"),
                 ("The first era begins today.", "A Primeira Primeira Era começa hoje.", "repetição"))
        for source, output, message in cases:
            with self.subTest(source=source):
                translator, _, cache = self.translator(output)
                result = translator.translate(source)
                self.assertEqual((result.status, result.text), ("falha", source))
                self.assertIn(message, result.note)
                self.assert_cache_empty(cache)

    def test_repetition_used_for_emphasis_in_source_is_preserved(self):
        translator, _, _ = self.translator("Um reino muito muito distante.")
        result = translator.translate("A very very distant realm.")
        self.assertEqual((result.status, result.text), ("ok", "Um reino muito muito distante."))

    def test_angle_quotes_follow_source_quote_style_including_mismatched_pair(self):
        self.assertEqual(normalize_punct('Say "the king".', 'Diga «o rei».'), 'Diga "o rei".')
        self.assertEqual(normalize_punct("Say 'the king'.", "Diga 'o rei«."), "Diga 'o rei'.")
        self.assertEqual(normalize_punct("Say 'the king'.", "Diga «o rei»."), "Diga 'o rei'.")
        translator, _, _ = self.translator("Diga «o rei».")
        self.assertEqual(translator.translate("Say 'the king'.").text, "Diga 'o rei'.")


class BackendAndConcurrencyTests(unittest.TestCase):
    def test_argos_errors_are_reported_as_backend_errors(self):
        failure = TimeoutError("model stopped")

        def fail(text):
            raise failure

        with self.assertRaises(BackendError) as caught:
            ArgosBackend(translate_fn=fail).chat("", "The army returns home.")
        self.assertIs(caught.exception.__cause__, failure)

    def test_argos_rejects_empty_or_nontextual_output(self):
        for output in (None, [], 42, "", "   "):
            with self.subTest(output=output), self.assertRaises(BackendError):
                ArgosBackend(translate_fn=lambda text: output).chat("", "The army returns home.")

    def test_simultaneous_identical_sources_share_one_translation(self):
        entered = threading.Event()
        duplicate = threading.Event()
        release = threading.Event()
        start = threading.Barrier(7)
        backend_lock = threading.Lock()
        calls = []

        def translate(text):
            with backend_lock:
                calls.append(text)
                if len(calls) > 1:
                    duplicate.set()
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return "O lago sagrado fica ao norte."

        backend = Reply(translate)
        cache = self.enterContext(Cache())
        # Translators diferentes também devem compartilhar o trabalho pelo cache.
        translators = [Translator(backend, cache) for _ in range(6)]

        def worker(translator):
            start.wait(3)
            return translator.translate("The sacred lake is in the north.")

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(worker, translator) for translator in translators]
            try:
                start.wait(3)
                self.assertTrue(entered.wait(2))
                self.assertFalse(duplicate.wait(0.05), "Uma segunda tradução começou antes do cache.")
            finally:
                release.set()
            results = [future.result(timeout=3) for future in futures]
        self.assertEqual(backend.calls, 1)
        self.assertEqual([result.text for result in results], ["O lago sagrado fica ao norte."] * 6)
        self.assertEqual(sorted(result.status for result in results), ["cache"] * 5 + ["ok"])


if __name__ == "__main__":
    unittest.main()
