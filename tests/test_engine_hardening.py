"""Tratamento de respostas inválidas e limitação dos pedidos físicos ao Google."""
import io
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from ck2translator.translator import (
    BackendError, GoogleBackend, OllamaBackend, OpenAICompatBackend, _post_json,
)


class EngineHardeningTests(unittest.TestCase):
    def test_invalid_json_utf8_or_json_shape_raises_backend_error(self):
        for raw in (b"<html>unavailable</html>", b"\xff", b"[]", b"null"):
            with self.subTest(raw=raw), mock.patch("ck2translator.translator.urllib.request.urlopen") as opened:
                opened.return_value.__enter__.return_value.read.return_value = raw
                with self.assertRaises(BackendError):
                    _post_json("http://localhost/test", {}, 1)

    def test_valid_json_object_is_returned(self):
        with mock.patch("ck2translator.translator.urllib.request.urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'{"message":{"content":"Ola"}}'
            self.assertEqual(_post_json("http://localhost/test", {}, 1), {"message": {"content": "Ola"}})

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
