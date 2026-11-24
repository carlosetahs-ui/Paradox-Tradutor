"""Regressões de retomada, cancelamento e persistência do cache."""

import contextlib
import io
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from ck2translator import cli
from ck2translator.translator import Backend, BackendError, Cache, GoogleBackend, Translator, translation_cancel


class BatchReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.loc = self.root / "mod" / "localisation"
        self.loc.mkdir(parents=True)
        self.config = self.root / "config.toml"

    def configure(self, game="eu4", mode="overlay", count=3):
        self.config.write_text(
            f"[game]\nid='{game}'\n[model]\nbackend='google'\n[translate]\n"
            f"mode='{mode}'\nreuse_cache_from=[]\n[[mods]]\nname='Test'\npath='mod'\n",
            encoding="utf-8",
        )
        if game == "eu4":
            self.source = self.loc / "sample_l_english.yml"
            raw = "l_english:\n" + "".join(f' key{i}:0 "Castle {i}"\n' for i in range(count))
        else:
            self.source = self.loc / "sample.csv"
            raw = "".join(f"key{i};Castle {i};;;;;;;;;;;;;x\n" for i in range(count))
        self.source.write_text(raw, encoding="utf-8")
        self.output = self.root / "saida" / "test_ptbr" / "localisation" / self.source.name

    def run_backend(self, fn):
        backend = GoogleBackend(translate_fn=fn, pause=(0, 0), retry_wait=0, long_every=0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                mock.patch.object(cli, "GoogleBackend", return_value=backend):
            code = cli.main(["translate", "--config", str(self.config)])
        return code, output.getvalue()

    def test_isolated_timeout_preserves_other_lines_and_only_failed_text_is_retried(self):
        self.configure()

        def first_call(text):
            if text == "Castle 1":
                raise TimeoutError("private text")
            return text.replace("Castle", "Castelo")

        code, log = self.run_backend(first_call)
        self.assertEqual(code, 1, log)
        first_output = self.output.read_text(encoding="utf-8-sig")
        self.assertIn('"Castelo 0"', first_output)
        self.assertIn('"Castle 1"', first_output)
        self.assertIn('"Castelo 2"', first_output)
        report = (self.root / "saida" / "test_relatorio.csv").read_text(encoding="utf-8-sig")
        self.assertIn("key1;falha;Castle 1", report)
        self.assertNotIn("private text", report)

        looked_up = []
        original_translate = Translator.translate
        atomic_write = cli.atomic_write

        def record(translator, text, **options):
            looked_up.append(text)
            return original_translate(translator, text, **options)

        def preserve_output(path, data):
            self.assertNotEqual(path, self.output, "saída idêntica não deve ser regravada")
            return atomic_write(path, data)

        with mock.patch.object(Translator, "translate", new=record), \
                mock.patch.object(cli, "atomic_write", side_effect=preserve_output):
            code, log = self.run_backend(first_call)
        self.assertEqual(code, 1, log)
        self.assertEqual(looked_up, ["Castle 1"])
        self.assertEqual(self.output.read_text(encoding="utf-8-sig"), first_output)

        recovered = mock.Mock(return_value="Castelo 1")
        looked_up.clear()
        with mock.patch.object(Translator, "translate", new=record):
            code, log = self.run_backend(recovered)
        self.assertEqual(code, 0, log)
        self.assertEqual(looked_up, ["Castle 1"])
        recovered.assert_called_once_with("Castle 1")
        self.assertEqual(self.output.read_text(encoding="utf-8-sig"), first_output.replace('"Castle 1"', '"Castelo 1"'))

    def test_service_outage_defers_rest_of_batch_after_five_affected_texts(self):
        self.configure(count=10)
        unavailable = mock.Mock(side_effect=TimeoutError())
        code, log = self.run_backend(unavailable)
        self.assertEqual(code, 1, log)
        self.assertEqual(unavailable.call_count, 15)
        self.assertIn("5 texto(s) ficaram para a próxima execução", log)
        self.assertFalse(self.output.exists())

    def test_inplace_leaves_the_original_available_for_resuming(self):
        self.configure(game="ck2", mode="inplace")
        original = self.source.read_bytes()

        def fail_one(text):
            if text == "Castle 0":
                raise TimeoutError()
            return text.replace("Castle", "Castelo")

        code, log = self.run_backend(fail_one)
        self.assertEqual(code, 1, log)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((self.root / "saida" / "backup").exists())
        recovered = mock.Mock(return_value="Castelo 0")
        code, log = self.run_backend(recovered)
        self.assertEqual(code, 0, log)
        recovered.assert_called_once_with("Castle 0")
        self.assertIn(b"Castelo 2", self.source.read_bytes())

    def test_inplace_preserves_original_when_a_translation_is_rejected(self):
        self.configure(game="ck2", mode="inplace")
        original = self.source.read_bytes()

        def translate(text):
            return "O reino reino caiu." if text == "Castle 0" else text.replace("Castle", "Castelo")

        code, log = self.run_backend(translate)
        self.assertEqual(code, 1, log)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((self.root / "saida" / "backup").exists())

    def test_distinct_cache_keys_can_translate_concurrently(self):
        started = threading.Barrier(2, timeout=2)
        backend = mock.Mock(spec=Backend, model_id="test", uses_prompt=True, legacy_model_id=None)

        def chat(system, user):
            started.wait()
            return "Castelo"

        backend.chat.side_effect = chat
        with Cache() as cache, ThreadPoolExecutor(max_workers=2) as pool:
            translator = Translator(backend, cache)
            # As duas chaves cairiam na mesma faixa do bloqueio anterior.
            texts = {}
            for number in range(65):
                text = f"Castle {number}"
                key = translator._key(text, [], [])
                slot = int(key[:8], 16) % 64
                if slot in texts:
                    break
                texts[slot] = text
            futures = [pool.submit(translator.translate, source) for source in (texts[slot], text)]
            self.assertEqual([future.result(timeout=3).status for future in futures], ["ok", "ok"])
            self.assertEqual(translator.translate(text).status, "cache")
        self.assertEqual(backend.chat.call_count, 2)

    def test_fatal_error_signals_other_running_workers_before_waiting(self):
        ready = threading.Event()
        cancelled = threading.Event()

        def translate(text):
            if text == "waiting":
                ready.set()
                if translation_cancel.get().wait(2):
                    cancelled.set()
                return cli.Result("skip", text)
            self.assertTrue(ready.wait(2))
            raise BackendError("fatal")

        with self.assertRaisesRegex(BackendError, "fatal"):
            cli._translate_batch(mock.Mock(translate=translate), ["waiting", "failure"], 2)
        self.assertTrue(cancelled.is_set())

    def test_translation_is_visible_from_another_connection_before_cache_close(self):
        path = self.root / "cache.sqlite"
        with Cache(str(path)) as cache:
            cache.put("key", "Castle", "Castelo", "google")
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT dst FROM cache").fetchall(), [("Castelo",)])
