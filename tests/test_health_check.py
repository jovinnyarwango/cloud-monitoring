import io
import json
import os
import sys
import types
import unittest
import urllib.error
from unittest import mock

# boto3 is only needed inside AWS; stub it for local runs.
fake = types.ModuleType("boto3")
fake.client = lambda *a, **k: mock.Mock()
fake.resource = lambda *a, **k: mock.Mock()
sys.modules.setdefault("boto3", fake)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import health_check as hc  # noqa: E402


class Resp:
    def __init__(self, body, status=200):
        self.status, self._b = status, body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(target, responder):
    with mock.patch("urllib.request.urlopen", side_effect=responder), mock.patch("time.sleep"):
        return hc.check_site(target)


class Checks(unittest.TestCase):
    def test_up(self):
        self.assertEqual(run({"url": "https://x.test"}, lambda *a, **k: Resp("hi"))[0], "UP")

    def test_ok_false_is_down(self):
        t = {"url": "https://x.test", "expect_json": {"ok": True}}
        state, detail, _ = run(t, lambda *a, **k: Resp('{"ok": false, "db": false}'))
        self.assertEqual(state, "DOWN")
        self.assertIn("db", detail)

    def test_ok_true_is_up(self):
        t = {"url": "https://x.test", "expect_json": {"ok": True}}
        self.assertEqual(run(t, lambda *a, **k: Resp('{"ok": true}'))[0], "UP")

    def test_503_body_in_detail(self):
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b'{"jobs_ok":false}'))
        state, detail, _ = run({"url": "https://x.test"}, boom)
        self.assertEqual(state, "DOWN")
        self.assertIn("503", detail)
        self.assertIn("jobs_ok", detail)

    def test_retry_recovers(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("timeout")
            return Resp("hi")
        self.assertEqual(run({"url": "https://x.test"}, flaky)[0], "UP")
        self.assertEqual(len(calls), 2)

    def test_missing_text(self):
        self.assertEqual(run({"url": "https://x.test", "expect_text": "X"}, lambda *a, **k: Resp("no"))[0], "DOWN")

    def test_targets_file_valid(self):
        t = hc.load_targets()
        self.assertTrue(all("url" in x for x in t))
        self.assertTrue(any(x["url"].endswith("/api/health") for x in t))


class Decide(unittest.TestCase):
    def test_transitions(self):
        self.assertEqual(hc.decide(None, "DOWN", 1000)[0], "alert")
        self.assertEqual(hc.decide({"state": "UP"}, "DOWN", 1000)[0], "alert")
        prev = {"state": "DOWN", "since": 1000, "last_alert": 1000}
        self.assertIsNone(hc.decide(prev, "DOWN", 1300)[0])
        self.assertEqual(hc.decide(prev, "DOWN", 1000 + 3600)[0], "reminder")
        self.assertEqual(hc.decide(prev, "UP", 1300)[0], "recovered")
        self.assertIsNone(hc.decide({"state": "UP"}, "UP", 1300)[0])
        self.assertIsNone(hc.decide(None, "UP", 1300)[0])


class Handler(unittest.TestCase):
    def test_one_alert_then_quiet_then_recovered(self):
        store = {}
        tbl = mock.Mock()
        tbl.get_item.side_effect = lambda Key: {"Item": store[Key["url"]]} if Key["url"] in store else {}
        tbl.put_item.side_effect = lambda Item: store.__setitem__(Item["url"], Item)
        sent = []
        targets = [{"url": "https://x.test"}]
        outcome = {"v": ("DOWN", "boom", 5)}
        with mock.patch.object(hc, "load_targets", return_value=targets), \
             mock.patch.object(hc, "table", return_value=tbl), \
             mock.patch.object(hc, "full_check", side_effect=lambda t: outcome["v"]), \
             mock.patch.object(hc.sns, "publish", side_effect=lambda **k: sent.append(k["Subject"])):
            hc.lambda_handler({}, None)
            hc.lambda_handler({}, None)
            outcome["v"] = ("UP", "ok", 5)
            hc.lambda_handler({}, None)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].startswith("MONITOR ALERT"))
        self.assertTrue(sent[1].startswith("MONITOR RECOVERED"))


if __name__ == "__main__":
    unittest.main()
