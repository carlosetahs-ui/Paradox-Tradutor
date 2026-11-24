"""Tratamento de respostas inválidas e limitação dos pedidos físicos ao Google."""
import io
import json
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from ck2translator.translator import (
    BackendError, Cache, GeminiBackend, GoogleBackend, OllamaBackend, OpenAICompatBackend, RateLimiter,
    TextBackendError, Translator, _post_json,
)


class EngineHardeningTests(unittest.TestCase):
    def test_invalid_json_utf8_or_json_shape_raises_backend_error(self):
        for raw in (b"<html>unavailable</html>", b"\xff", b"[]", b"null"):
            with self.subTest(raw=raw), mock.patch("ck2translator.translator.urllib.request.urlopen") as opened:
                opened.return_value = io.BytesIO(raw)
                with self.assertRaises(BackendError):
                    _post_json("http://localhost/test", {}, 1)

    def test_valid_json_object_is_returned(self):
        with mock.patch("ck2translator.translator.urllib.request.urlopen") as opened:
            opened.return_value = io.BytesIO(b'{"message":{"content":"Ola"}}')
            self.assertEqual(_post_json("http://localhost/test", {}, 1), {"message": {"content": "Ola"}})

    def test_model_requests_retry_503_and_respect_retry_after(self):
        failure = urllib.error.HTTPError("https://example.test", 503, "Unavailable",
                                         {"Retry-After": "7"}, io.BytesIO())
        response = io.BytesIO(b'{"message":{"content":"Ola"}}')
        with mock.patch("ck2translator.translator.urllib.request.urlopen", side_effect=[failure, response]) as request, \
                mock.patch("ck2translator.translator.time.sleep") as sleep:
            self.assertEqual(_post_json("https://example.test", {}, 3), {"message": {"content": "Ola"}})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(7)

    def test_response_with_continuous_small_chunks_obeys_reading_deadline(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read1.return_value = b"x"
        with mock.patch("ck2translator.translator.urllib.request.urlopen", return_value=response), \
                mock.patch("ck2translator.translator.time.monotonic", side_effect=[0, 0.5, 1.5]):
            with self.assertRaisesRegex(TextBackendError, "TimeoutError"):
                _post_json("https://example.test", {}, 1, retries=0)
        response.__enter__.return_value.read1.assert_called_once()

    def test_remote_plain_http_is_refused_and_credentials_do_not_follow_redirects(self):
        for backend in (OllamaBackend, OpenAICompatBackend):
            with self.subTest(backend=backend), self.assertRaisesRegex(BackendError, "HTTPS"):
                backend("model", host="http://server.example.test")
        with mock.patch("ck2translator.translator.urllib.request.urlopen") as opened:
            error = urllib.error.HTTPError("https://example.test", 401, "", {}, io.BytesIO(b"secret"))
            opened.side_effect = error
            with self.assertRaisesRegex(BackendError, "401") as caught:
                OpenAICompatBackend("model", host="https://example.test/v1", api_key="secret").chat("", "hello")
            opened.assert_called_once()
            request = opened.call_args.args[0]
            self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer secret")
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("secret", str(caught.exception))

    def test_google_transport_uses_timeout_and_does_not_log_the_query_on_failure(self):
        dependencies = {
            "deep_translator": SimpleNamespace(GoogleTranslator=lambda **_: SimpleNamespace(source="en", target="pt")),
            "bs4": SimpleNamespace(BeautifulSoup=mock.Mock()),
        }
        with mock.patch.dict("sys.modules", dependencies), \
                mock.patch("ck2translator.translator.urllib.request.build_opener") as builder, \
                mock.patch("ck2translator.translator.time.sleep"):
            backend = GoogleBackend(timeout=7, pause=(0, 0), retry_wait=0)
            opener = builder.return_value
            opener.open.side_effect = TimeoutError("private source text")
            with self.assertRaises(TextBackendError) as caught:
                backend.chat("", "private source text")
        self.assertEqual(opener.open.call_count, 3)
        self.assertTrue(all(call.kwargs["timeout"] == 7 for call in opener.open.call_args_list))
        self.assertNotIn("private", str(caught.exception))

    def test_local_backends_reject_nontextual_or_empty_content(self):
        for content in (None, [], 42, "", "  "):
            for kind in ("ollama", "openai"):
                with self.subTest(content=content, kind=kind):
                    if kind == "ollama":
                        backend = OllamaBackend("test")
                        response = {"message": {"content": content}}
                    else:
                        backend = OpenAICompatBackend("test")
                        response = {"choices": [{"message": {"content": content}}]}
                    with mock.patch("ck2translator.translator._post_json", return_value=response):
                        with self.assertRaises(BackendError):
                            backend.chat("", "Hello")

    def test_local_backends_reject_truncated_text_without_caching(self):
        cases = [
            (OllamaBackend("test"), {"done_reason": "length", "message": {"content": "Olá"}}),
            (OpenAICompatBackend("test"), {"choices": [
                {"finish_reason": "length", "message": {"content": "Olá"}},
            ]}),
        ]
        for backend, response in cases:
            with self.subTest(backend=backend.model_id), Cache() as cache, \
                    mock.patch("ck2translator.translator._post_json", return_value=response) as post:
                translator = Translator(backend, cache)
                result = translator.translate("Hello there")
                self.assertEqual((result.status, result.text), ("falha", "Hello there"))
                self.assertEqual(translator.translate("Hello there", dry=True).status, "pendente")
                post.assert_called_once()

    def test_gemini_uses_header_and_official_generate_content_shape(self):
        limiter = mock.Mock(spec=RateLimiter)
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "Raciocínio interno", "thought": True}, {"text": "Olá"}, {"text": " mundo"}
        ]}}]}
        with mock.patch("ck2translator.translator._post_json", return_value=response) as post:
            backend = GeminiBackend("models/gemini-2.5-flash", "secret", 0.15, 12, limiter)
            self.assertEqual(backend.chat("instruções", "Hello"), "Olá mundo")
        url, payload, timeout, headers = post.call_args.args
        self.assertIs(post.call_args.kwargs["limiter"], limiter)
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/"
                              "gemini-2.5-flash:generateContent")
        self.assertEqual(timeout, 12)
        self.assertEqual(headers, {"x-goog-api-key": "secret"})
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "instruções")
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "Hello")
        self.assertEqual(payload["generationConfig"]["temperature"], 0.15)

    def test_gemini_reads_environment_key_without_leaking_it(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "environment-secret"}, clear=True):
            backend = GeminiBackend("gemini-test")
        self.assertEqual(backend.headers, {"x-goog-api-key": "environment-secret"})
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(BackendError, "GEMINI_API_KEY"):
            GeminiBackend("gemini-test")

    def test_gemini_rejects_blocked_or_malformed_responses(self):
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
        with mock.patch("ck2translator.translator._post_json", return_value=blocked):
            with self.assertRaisesRegex(TextBackendError, "SAFETY"):
                GeminiBackend("gemini-test", "secret").chat("system", "user")
        partial = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "Olá"}]}}]}
        with mock.patch("ck2translator.translator._post_json", return_value=partial):
            with self.assertRaisesRegex(TextBackendError, "MAX_TOKENS"):
                GeminiBackend("gemini-test", "secret").chat("system", "user")

    def test_gemini_retries_use_the_same_rate_limiter(self):
        limiter = mock.Mock(spec=RateLimiter)
        unavailable = urllib.error.HTTPError("https://example.test", 503, "", {}, io.BytesIO())
        response = io.BytesIO(b'{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"Ola"}]}}]}')
        with mock.patch("ck2translator.translator.urllib.request.urlopen", side_effect=[unavailable, response]) as request, \
                mock.patch("ck2translator.translator.time.sleep"):
            self.assertEqual(GeminiBackend("gemini-test", "secret", limiter=limiter).chat("", "Hello"), "Ola")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(limiter.wait.call_count, 2)

    def test_gemini_groups_parallel_translations_in_one_request(self):
        translated = json.dumps({"translations": [
            {"id": 0, "text": "Um"}, {"id": 1, "text": "Dois"}, {"id": 2, "text": "Três"},
        ]})
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": translated}]}}]}
        backend = GeminiBackend("gemini-test", "secret", batch_size=3)
        with mock.patch("ck2translator.translator._post_json", return_value=response) as post, \
                ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(backend.chat, "instruções comuns", f"Text {number}")
                       for number in range(3)]
            self.assertEqual([future.result(timeout=2) for future in futures], ["Um", "Dois", "Três"])
        post.assert_called_once()
        payload = post.call_args.args[1]
        jobs = json.loads(payload["contents"][0]["parts"][0]["text"])["jobs"]
        self.assertEqual([job["entrada"] for job in jobs], ["Text 0", "Text 1", "Text 2"])
        self.assertEqual(backend.stats, {"pedidos_gemini": 1, "textos_enviados": 3})

    def test_gemini_rejects_invalid_batch_without_mixing_lines(self):
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": '{"translations":[{"id":0,"text":"Um"}]}'}
        ]}}]}
        backend = GeminiBackend("gemini-test", "secret", batch_size=2)
        with mock.patch("ck2translator.translator._post_json", return_value=response), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(backend.chat, "system", f"Text {number}") for number in range(2)]
            for future in futures:
                with self.assertRaisesRegex(TextBackendError, "lote"):
                    future.result(timeout=2)

    def test_gemini_retries_blocked_batch_as_independent_requests(self):
        blocked = {"promptFeedback": {"blockReason": "SAFETY"}}

        def respond(url, payload, timeout, headers, **kwargs):
            sent = payload["contents"][0]["parts"][0]["text"]
            if sent.startswith('{"jobs"') or sent == "Blocked text":
                return blocked
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Olá"}]}}]}

        backend = GeminiBackend("gemini-test", "secret", batch_size=2)
        with mock.patch("ck2translator.translator._post_json", side_effect=respond) as post, \
                ThreadPoolExecutor(max_workers=2) as pool:
            good = pool.submit(backend.chat, "system", "Hello")
            bad = pool.submit(backend.chat, "system", "Blocked text")
            self.assertEqual(good.result(timeout=2), "Olá")
            with self.assertRaisesRegex(TextBackendError, "SAFETY"):
                bad.result(timeout=2)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(backend.stats, {"pedidos_gemini": 3, "textos_enviados": 4})

    def test_gemini_batch_retries_only_the_invalid_translation(self):
        calls = []

        def respond(url, payload, timeout, headers, **kwargs):
            calls.append(payload)
            sent = payload["contents"][0]["parts"][0]["text"]
            if sent.startswith('{"jobs"'):
                jobs = json.loads(sent)["jobs"]
                rows = []
                for job in jobs:
                    text = "Olá" if "Hello" in job["entrada"] else "Adeus"  # perde {1} de propósito
                    rows.append({"id": job["id"], "text": text})
                output = json.dumps({"translations": rows}, ensure_ascii=False)
            else:
                output = "Adeus {1}"
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": output}]}}]}

        backend = GeminiBackend("gemini-test", "secret", batch_size=2)
        with Cache() as cache, mock.patch("ck2translator.translator._post_json", side_effect=respond), \
                ThreadPoolExecutor(max_workers=2) as pool:
            translator = Translator(backend, cache)
            futures = [pool.submit(translator.translate, text) for text in ("Hello", "Goodbye $NAME$")]
            results = [future.result(timeout=2) for future in futures]
        self.assertEqual([(result.status, result.text) for result in results],
                         [("ok", "Olá"), ("ok", "Adeus $NAME$")])
        self.assertEqual(len(calls), 2)

    def test_google_counts_and_pauses_for_each_chunk(self):
        calls = []
        backend = GoogleBackend(
            translate_fn=lambda text: calls.append(text) or text,
            pause=(1, 1), long_every=2, long_pause=10,
        )
        with mock.patch("ck2translator.translator.time.sleep") as sleep:
            backend.chat("", "word " * 1800)
        self.assertGreater(len(calls), 1)
        self.assertEqual(backend.count, len(calls))
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [10 if count % 2 == 0 else 1 for count in range(1, len(calls) + 1)])

    def test_google_retries_are_also_counted_and_throttled(self):
        backend = GoogleBackend(
            translate_fn=mock.Mock(side_effect=[RuntimeError("temporary"), "Ola"]),
            pause=(1, 1), long_every=0, retry_wait=4,
        )
        with mock.patch("ck2translator.translator.time.sleep") as sleep, mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(backend.chat("", "Hello"), "Ola")
        self.assertEqual(backend.count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 4, 1])

    def test_google_requests_from_workers_never_overlap(self):
        entered = threading.Event()
        release = threading.Event()
        second_called = threading.Event()

        def translate(text):
            if text == "first":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timed out")
            else:
                second_called.set()
            return text

        backend = GoogleBackend(translate_fn=translate, pause=(0, 0), long_every=0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(backend.chat, "", "first")
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(backend.chat, "", "second")
                self.assertFalse(second_called.wait(0.05), "Pedidos simultâneos ao Google")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3), "first")
            self.assertEqual(second.result(timeout=3), "second")
        self.assertEqual(backend.count, 2)


if __name__ == "__main__":
    unittest.main()
