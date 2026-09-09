import io
import random
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from sokuho_archive.fetcher import Fetcher, FetchError


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def http_error(code, headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://x.invalid", code, "err", message, io.BytesIO(b""))


FEED = b'<?xml version="1.0" encoding="UTF-8" ?>\n<flashNews flag="0" pubDate="x" />\n'


def fetcher(**kwargs):
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("rng", random.Random(0))
    return Fetcher(**kwargs)


class HappyPath(unittest.TestCase):
    def test_returns_body_and_validators(self):
        response = FakeResponse(FEED, 200, {"ETag": '"abc"', "Last-Modified": "Tue, 08 Sep 2026 16:02:19 GMT"})
        with mock.patch("urllib.request.urlopen", return_value=response):
            result = fetcher().fetch()
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, FEED)
        self.assertEqual(result.etag, '"abc"')
        self.assertEqual(result.attempts, 1)
        self.assertFalse(result.not_modified)

    def test_sends_conditional_headers(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured.update(request.headers)
            return FakeResponse(FEED, 200)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetcher().fetch(etag='"abc"', last_modified="Tue, 08 Sep 2026 16:02:19 GMT")
        self.assertEqual(captured.get("If-none-match"), '"abc"')
        self.assertEqual(captured.get("If-modified-since"), "Tue, 08 Sep 2026 16:02:19 GMT")

    def test_identifies_itself_and_refuses_compression(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured.update(request.headers)
            return FakeResponse(FEED, 200)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetcher().fetch()
        self.assertIn("sokuho-archive", captured.get("User-agent", ""))
        self.assertEqual(captured.get("Accept-encoding"), "identity")


class NotModified(unittest.TestCase):
    def test_304_is_a_result_not_an_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(304, {"ETag": '"abc"'})):
            result = fetcher().fetch(etag='"abc"')
        self.assertTrue(result.not_modified)
        self.assertIsNone(result.body)
        self.assertEqual(result.etag, '"abc"')

    def test_304_keeps_previous_validators_when_absent(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(304)):
            result = fetcher().fetch(etag='"kept"', last_modified="lm")
        self.assertEqual(result.etag, '"kept"')
        self.assertEqual(result.last_modified, "lm")


class Retries(unittest.TestCase):
    def test_transient_waf_403_is_retried_then_succeeds(self):
        # Exactly the behaviour observed live: an interposed WAF 403s at random.
        attempts = [http_error(403), http_error(403), FakeResponse(FEED, 200)]

        def fake_urlopen(request, timeout=None):
            outcome = attempts.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = fetcher().fetch()
        self.assertEqual(result.status, 200)
        self.assertEqual(result.attempts, 3)

    def test_network_errors_are_retried(self):
        attempts = [urllib.error.URLError("boom"), FakeResponse(FEED, 200)]

        def fake_urlopen(request, timeout=None):
            outcome = attempts.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(fetcher().fetch().attempts, 2)

    def test_permanent_error_falls_through_to_the_mirror(self):
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if len(seen) == 1:
                raise http_error(404)
            return FakeResponse(FEED, 200)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = fetcher().fetch()
        self.assertEqual(len(seen), 2, "a 404 should not be retried on the same URL")
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual(result.url, seen[1])

    def test_gives_up_with_the_last_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(503)):
            with self.assertRaises(FetchError) as caught:
                fetcher(max_attempts=2).fetch()
        self.assertEqual(caught.exception.reason, "http_error")
        self.assertEqual(caught.exception.status, 503)

    def test_backoff_is_bounded_and_jittered(self):
        instance = fetcher(backoff_base=2.0, backoff_cap=30.0)
        delays = [instance._delay(n) for n in range(1, 12)]
        self.assertTrue(all(0.0 <= d <= 30.0 for d in delays))
        self.assertGreater(len(set(delays)), 1, "identical delays would synchronise retries")

    def test_sleeps_between_attempts(self):
        slept = []
        with mock.patch("urllib.request.urlopen", side_effect=http_error(503)):
            with self.assertRaises(FetchError):
                Fetcher(sleeper=slept.append, rng=random.Random(0), max_attempts=3).fetch()
        self.assertTrue(slept)


class Limits(unittest.TestCase):
    def test_oversized_body_is_refused(self):
        response = FakeResponse(b"x" * 5000, 200)
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(FetchError) as caught:
                fetcher(max_body_bytes=100, max_attempts=1, mirrors=()).fetch()
        self.assertEqual(caught.exception.reason, "body_too_large")

    def test_unexpected_gzip_is_decompressed(self):
        import gzip
        response = FakeResponse(gzip.compress(FEED), 200)
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(fetcher().fetch().body, FEED)

    def test_corrupt_gzip_is_refused(self):
        response = FakeResponse(b"\x1f\x8b" + b"garbage", 200)
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(FetchError) as caught:
                fetcher(max_attempts=1, mirrors=()).fetch()
        self.assertEqual(caught.exception.reason, "bad_gzip")


if __name__ == "__main__":
    unittest.main()
