import unittest
from ck2translator.translator import RateLimiter, TranslatorsBackend, BackendError


class FakeClock:
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += max(s, 0)


class TestRateLimiter(unittest.TestCase):
    def test_per_minute_cap(self):
        c = FakeClock()
        lim = RateLimiter(per_minute=5, per_hour=0, pause=(0, 0), long_every=0, sleep=c.sleep, now=c.now)
        for _ in range(6):
            lim.wait()
        self.assertGreaterEqual(c.t, 60)  # 6th request waits for the minute window


class TestTranslatorsBackend(unittest.TestCase):
    def mk(self, fn):
        lim = RateLimiter(0, 0, (0, 0), 0, sleep=lambda s: None)
        return TranslatorsBackend(["google", "bing"], limiter=lim, translate_fn=fn, sleep=lambda s: None)

    def test_rotates_when_blocked(self):
        calls = []
        def fn(text, p, s, d):
            calls.append(p)
            if p == "google":
                raise RuntimeError("429 Too Many Requests")
            return "Olá {1}"
        b = self.mk(fn)
        out = b.chat("", "<texto>\nHello {1}\n</texto>")
        self.assertEqual(out, "Olá {1}")
        self.assertEqual(calls, ["google", "bing"])
        b2 = b.chat("", "<texto>\nHi\n</texto>")
        self.assertEqual(calls[-1], "bing")  # google still on cooldown

    def test_all_fail_raises(self):
        def fn(*a): raise RuntimeError("403 blocked")
        b = self.mk(fn)
        b.cooldown = 0
        with self.assertRaises(BackendError):
            b.chat("", "<texto>\nHi\n</texto>")


if __name__ == "__main__":
    unittest.main()


class TestCodeProtection(unittest.TestCase):
    def mk(self, fn):
        lim = RateLimiter(0, 0, (0, 0), 0, sleep=lambda s: None)
        return TranslatorsBackend(["google", "bing"], limiter=lim, translate_fn=fn, sleep=lambda s: None)

    def test_repairs_mangled_markers(self):
        b = self.mk(lambda t, *a: "O exército xx 1 Xx marcha")
        self.assertEqual(b.chat("", "<texto>\nThe XX army\n</texto>".replace("XX", "{1}")), "O exército {1} marcha")

    def test_lost_marker_falls_back_to_segments(self):
        def fn(t, p, s, d):
            if "XX" in t:
                return "Olá, mundo"          # marker lost
            return {"Hello": "Olá", "world": "mundo"}.get(t, t)
        b = self.mk(fn)
        self.assertEqual(b.chat("", "<texto>\nHello {1} world\n</texto>"), "Olá {1} mundo")
        self.assertEqual(b.stats["trecho_a_trecho"], 1)

    def test_duplicated_marker_rejected(self):
        b = self.mk(lambda t, *a: "XX1XX e XX1XX" if "XX" in t else "oi")
        self.assertEqual(b.chat("", "<texto>\nhi {1}\n</texto>"), "oi {1}")
