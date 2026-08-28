"""Tests for the fetch/cache robustness cluster in scripts/_common.py.

Covers ``default_cache_dir`` (env-override / XDG resolution), atomic cache
writes (no partially-written file ever observable, no leftover temp file),
the stale-cache fallback on transport failure, the widened exception
handling (a plain ``URLError`` catch missed ``TimeoutError`` and
``http.client.IncompleteRead``, which used to reach the user as a raw
Python traceback), and ``load_lines``' ``errors="replace"`` decoding.

``fetch_url``'s failure paths need a mocked ``urllib.request.urlopen``
rather than a cache fixture — a cache fixture works precisely by making
``fetch_url`` short-circuit *before* any network call, which is the one
code path these tests need to get past.
"""

import contextlib
import gzip
import http.client
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

import _common


class DefaultCacheDirTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k) for k in ("LLMS_DOCS_CACHE_DIR", "XDG_CACHE_HOME")
        }
        os.environ.pop("LLMS_DOCS_CACHE_DIR", None)
        os.environ.pop("XDG_CACHE_HOME", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_llms_docs_cache_dir_override_wins(self):
        os.environ["LLMS_DOCS_CACHE_DIR"] = "/tmp/llms-docs-override-test"
        self.assertEqual(
            _common.default_cache_dir(), "/tmp/llms-docs-override-test"
        )

    def test_xdg_cache_home_used_when_no_override(self):
        os.environ["XDG_CACHE_HOME"] = "/tmp/xdg-test-dir"
        self.assertEqual(
            _common.default_cache_dir(), "/tmp/xdg-test-dir/llms-docs"
        )

    def test_falls_back_to_home_cache_when_neither_set(self):
        result = _common.default_cache_dir()
        self.assertTrue(result.endswith(os.path.join(".cache", "llms-docs")))
        self.assertNotIn("~", result)  # expanduser must have run

    def test_relative_xdg_cache_home_is_ignored(self):
        # Per the XDG Base Directory spec, a relative $XDG_CACHE_HOME is
        # invalid and must be ignored — not resolved against the cwd
        # (which would make the cache location depend on whichever
        # directory the script happened to be launched from, and could
        # read/write into an unrelated project directory).
        os.environ["XDG_CACHE_HOME"] = "relative/xdg/path"
        result = _common.default_cache_dir()
        self.assertTrue(os.path.isabs(result))
        self.assertNotIn("relative/xdg/path", result)
        self.assertTrue(result.endswith(os.path.join(".cache", "llms-docs")))

    def test_empty_xdg_cache_home_is_ignored(self):
        os.environ["XDG_CACHE_HOME"] = ""
        result = _common.default_cache_dir()
        self.assertTrue(os.path.isabs(result))
        self.assertTrue(result.endswith(os.path.join(".cache", "llms-docs")))

    def test_tilde_prefixed_xdg_cache_home_is_still_absolute(self):
        # Guards against a future "simplification" of the isabs() check
        # (e.g. to startswith("/")) that would wrongly reject this.
        os.environ["XDG_CACHE_HOME"] = "~/custom-cache"
        result = _common.default_cache_dir()
        self.assertTrue(os.path.isabs(result))
        self.assertEqual(
            result,
            os.path.join(os.path.expanduser("~/custom-cache"), "llms-docs"),
        )

    def test_llms_docs_cache_dir_override_accepts_relative_path(self):
        # Unlike $XDG_CACHE_HOME, $LLMS_DOCS_CACHE_DIR is this plugin's own
        # escape hatch and is not governed by the XDG spec — a relative
        # value is passed through (resolved by the OS against the cwd),
        # deliberately, not a bug.
        os.environ["LLMS_DOCS_CACHE_DIR"] = "relative/override"
        self.assertEqual(_common.default_cache_dir(), "relative/override")

    def test_does_not_create_the_directory_as_a_side_effect(self):
        target = "/tmp/llms-docs-should-not-be-created-test"
        if os.path.isdir(target):
            os.rmdir(target)
        os.environ["LLMS_DOCS_CACHE_DIR"] = target
        _common.default_cache_dir()
        self.assertFalse(os.path.isdir(target))


class FormatAgeTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(_common._format_age(30), "30s")

    def test_minutes(self):
        self.assertEqual(_common._format_age(180), "3m")

    def test_hours(self):
        self.assertEqual(_common._format_age(3 * 3600), "3h")

    def test_days(self):
        self.assertEqual(_common._format_age(9 * 86400), "9.0d")

    def test_negative_clamped_to_zero(self):
        self.assertEqual(_common._format_age(-5), "0s")


class _FakeResponse:
    """Minimal stand-in for the object ``urllib.request.urlopen`` returns."""

    def __init__(self, data, headers=None, url="https://example.com/x"):
        self._data = data
        self.headers = headers or {}
        # Every existing test fetches "https://example.com/x" — defaulting
        # to that keeps them all reading as "no redirect happened" without
        # having to pass url= at every call site. A test that needs to
        # simulate a redirect passes a different url= explicitly.
        self.url = url

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FetchUrlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _cache_path(self, name="cache.txt"):
        return os.path.join(self.tmp, name)

    def test_successful_fetch_writes_file_and_leaves_no_temp_file(self):
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(b"hello world")
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)
        with open(cache_path, "rb") as f:
            self.assertEqual(f.read(), b"hello world")
        leftover = [n for n in os.listdir(self.tmp) if n.startswith(".fetch-tmp-")]
        self.assertEqual(leftover, [])

    def test_content_length_match_succeeds(self):
        cache_path = self._cache_path()
        data = b"exactly eleven"
        resp = _FakeResponse(data, headers={"Content-Length": str(len(data))})
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)

    def test_content_length_mismatch_is_treated_as_failure(self):
        cache_path = self._cache_path()
        bad_resp = _FakeResponse(b"short", headers={"Content-Length": "999"})
        with mock.patch("urllib.request.urlopen", return_value=bad_resp):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(cache_path))

    def test_content_length_mismatch_warning_does_not_dump_the_body(self):
        # http.client.IncompleteRead carries the partial body as its sole
        # constructor arg; if str(exc) ever rendered that raw payload, a
        # truncated ~24MB fetch would dump megabytes into stderr instead of
        # a short warning. Empirically verified safe on 3.11/3.12/3.14
        # (IncompleteRead has a dedicated __str__), but pinned here as a
        # regression test since that's an implementation detail of the
        # stdlib, not a documented contract.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        body = b"x" * 100_000
        resp = _FakeResponse(body, headers={"Content-Length": "999999"})
        err = io.StringIO()
        with mock.patch(
            "urllib.request.urlopen", return_value=resp
        ), contextlib.redirect_stderr(err):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        self.assertLess(len(err.getvalue()), 500)

    def test_non_integer_content_length_does_not_raise(self):
        # A malformed/non-conformant Content-Length header used to reach
        # users as a raw ValueError traceback (int("not-a-number") raises,
        # and ValueError isn't in fetch_url's except tuple). It is now
        # treated the same as no Content-Length at all: the check is
        # skipped and the (successfully read) body is cached normally.
        cache_path = self._cache_path()
        resp = _FakeResponse(
            b"hello world", headers={"Content-Length": "not-a-number"}
        )
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)
        with open(cache_path, "rb") as f:
            self.assertEqual(f.read(), b"hello world")

    def test_no_cache_and_fetch_fails_exits_1(self):
        cache_path = self._cache_path()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_no_cache_and_raise_on_error_true_raises_instead_of_exiting(self):
        # A caller fetching many independent pages in a loop (Firebase's
        # search/search-content) needs to catch one page's failure and
        # keep going, rather than the whole process dying via sys.exit.
        cache_path = self._cache_path()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            with self.assertRaises(_common.FetchError) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua",
                    raise_on_error=True,
                )
        self.assertEqual(cm.exception.url, "https://example.com/x")
        self.assertIsInstance(cm.exception.cause, OSError)
        self.assertFalse(os.path.exists(cache_path))

    def test_stale_cache_still_served_with_raise_on_error_true(self):
        # raise_on_error only changes the *no cache at all* branch — the
        # stale-serve fallback is a success path (returns a cache path),
        # not a failure, and must stay that way regardless of the flag.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800, raise_on_error=True,
            )
        self.assertEqual(result, cache_path)

    def test_stale_cache_served_on_fetch_failure(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400  # 8 days old (past the 7-day default)
        os.utime(cache_path, (old_time, old_time))

        err = io.StringIO()
        with mock.patch(
            "urllib.request.urlopen", side_effect=OSError("network down")
        ), contextlib.redirect_stderr(err):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "stale content")
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("using cached copy", err.getvalue())

    def test_timeout_error_does_not_propagate_as_raw_traceback(self):
        # TimeoutError is an OSError subclass but not a urllib.error.URLError
        # — this is exactly the gap the widened except clause closes.
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_incomplete_read_does_not_propagate_as_raw_traceback(self):
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"", 10),
        ):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_fresh_cache_short_circuits_without_network_call(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("fresh")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        mock_urlopen.assert_not_called()
        self.assertEqual(result, cache_path)


def _http_error(code: int, url: str = "https://example.com/x") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"HTTP {code}", {}, None)


class GzipAndConditionalGetTest(unittest.TestCase):
    """gzip decompression and ETag/Last-Modified conditional GET.

    ``urllib`` never auto-decompresses ``Content-Encoding: gzip`` and never
    returns a 304 as a normal response object (it raises ``HTTPError`` with
    ``code=304`` instead) — both need to be handled explicitly, and both
    need a mocked ``urlopen`` (a cache fixture alone can't exercise either).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _cache_path(self, name="cache.txt"):
        return os.path.join(self.tmp, name)

    def _meta_path(self, cache_path):
        return cache_path + ".meta.json"

    def test_gzip_content_encoding_is_decompressed_before_caching(self):
        body = b"plain text body" * 100
        compressed = gzip.compress(body)
        resp = _FakeResponse(compressed, headers={
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
        })
        cache_path = self._cache_path()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)
        with open(cache_path, "rb") as f:
            self.assertEqual(f.read(), body)

    def test_content_length_is_validated_against_compressed_bytes(self):
        # Content-Length describes what was actually transferred (the
        # gzip-encoded body), not the decompressed size — a mismatch here
        # must still be caught even though the *decompressed* size might
        # look unrelated to either number.
        body = b"plain text body" * 100
        compressed = gzip.compress(body)
        bad_resp = _FakeResponse(compressed[:-5], headers={
            "Content-Encoding": "gzip",
            "Content-Length": str(len(compressed)),
        })
        cache_path = self._cache_path()
        with mock.patch("urllib.request.urlopen", return_value=bad_resp):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(cache_path))

    def test_accept_encoding_gzip_header_is_always_sent(self):
        cache_path = self._cache_path()
        captured = {}

        def _capture(req, timeout=None):
            captured["accept_encoding"] = req.get_header("Accept-encoding")
            return _FakeResponse(b"hello")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            _common.fetch_url("https://example.com/x", cache_path, user_agent="ua")
        self.assertEqual(captured["accept_encoding"], "gzip")

    def test_successful_fetch_persists_etag_and_last_modified_sidecar(self):
        cache_path = self._cache_path()
        resp = _FakeResponse(b"hello", headers={
            "ETag": '"abc123"', "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
        })
        with mock.patch("urllib.request.urlopen", return_value=resp):
            _common.fetch_url("https://example.com/x", cache_path, user_agent="ua")
        with open(self._meta_path(cache_path), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta, {
            "content_hash": _common._content_hash(b"hello"),
            "etag": '"abc123"', "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
        })

    def test_redirected_fetch_does_not_persist_etag_or_last_modified(self):
        # urllib.request.HTTPRedirectHandler forwards a request's headers
        # unchanged to the redirected request (verified empirically against
        # a live redirect) — including any future If-None-Match/
        # If-Modified-Since this function would send. If the requested
        # url's redirect target ever changes later, a validator cached
        # from *this* redirect's destination would be sent toward the
        # *new* target instead; a coincidental 304 there would lock in the
        # old target's body indefinitely. So a fetch that resolves to a
        # different URL than requested (resp.url != url, exactly what
        # urllib sets on a real redirected response) must not persist
        # etag/last_modified — content_hash is unaffected and still saved.
        cache_path = self._cache_path()
        resp = _FakeResponse(
            b"hello",
            headers={
                "ETag": '"abc123"',
                "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
            url="https://example.com/redirected-destination",
        )
        with mock.patch("urllib.request.urlopen", return_value=resp):
            _common.fetch_url("https://example.com/x", cache_path, user_agent="ua")
        with open(self._meta_path(cache_path), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta, {"content_hash": _common._content_hash(b"hello")})

    def test_response_without_validators_still_persists_a_content_hash(self):
        # A server that never sends ETag/Last-Modified must not leave a
        # *previous* fetch's stale validators lying around to be sent on
        # the next conditional GET — but the content_hash (this function's
        # own bookkeeping, not server-dependent) is always (re)written.
        cache_path = self._cache_path()
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({"etag": '"stale"'}, f)
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(b"hello")
        ):
            _common.fetch_url("https://example.com/x", cache_path, user_agent="ua")
        with open(self._meta_path(cache_path), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta, {"content_hash": _common._content_hash(b"hello")})

    def test_sidecar_write_failure_does_not_fail_an_otherwise_successful_fetch(self):
        # The cache file itself is already written by the time the sidecar
        # write is attempted — losing the sidecar is a pure optimization
        # loss (no conditional GET next time), never a reason to report
        # this fetch as failed.
        cache_path = self._cache_path()
        real_atomic_write = _common._atomic_write

        def _fail_only_for_sidecar(path, data):
            if path.endswith(".meta.json"):
                raise OSError("disk full")
            return real_atomic_write(path, data)

        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(b"hello", headers={"ETag": '"abc123"'}),
        ), mock.patch.object(
            _common, "_atomic_write", side_effect=_fail_only_for_sidecar
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua"
            )
        self.assertEqual(result, cache_path)
        with open(cache_path, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        self.assertFalse(os.path.exists(self._meta_path(cache_path)))

    def test_non_dict_sidecar_is_treated_like_a_missing_one(self):
        # Valid JSON that isn't an object (a list, a bare string, ...) —
        # nothing guarantees the sidecar file was never hand-edited or
        # written by some future version with a different shape.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)

        captured = {}

        def _capture(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            return _FakeResponse(b"fresh content")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        self.assertIsNone(captured["if_none_match"])

    def test_non_string_validator_in_sidecar_is_treated_like_a_missing_one(self):
        # A hand edit, corruption, or a future format change could leave a
        # truthy non-string etag/last_modified in an otherwise-valid sidecar
        # (matching content_hash included). Inserted as-is into the request
        # headers, urllib raises an uncaught TypeError while sending the
        # request (verified empirically: http.client.putheader() special-
        # cases int — silently str()-coerced, no crash — but a list/dict
        # value fails bytes.join() inside putheader with exactly this
        # TypeError, which is what a JSON array value round-trips to).
        # Reject the whole sidecar instead, same as a structurally invalid
        # one.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"stale content"),
                "etag": ["not-a-string"],
            }, f)

        captured = {}

        def _capture(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            return _FakeResponse(b"fresh content")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        self.assertIsNone(captured["if_none_match"])

    def test_non_string_content_hash_in_sidecar_is_treated_like_a_missing_one(self):
        # A non-string content_hash can't crash the way a non-string etag/
        # last_modified can (it just never equals the current file's
        # string hash, so the sidecar already reads as "mismatched" and
        # falls through to no conditional headers) — but _load_fetch_meta
        # rejects it anyway, for the same reason it rejects a non-dict
        # top-level shape: every value it hands back should be a string a
        # caller can trust without a second type check.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": ["not-a-string"],
                "etag": '"abc123"',
            }, f)

        captured = {}

        def _capture(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            return _FakeResponse(b"fresh content")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        self.assertIsNone(captured["if_none_match"])

    def _putheader_failure(self, value):
        # Drives the *real* http.client.putheader() (never-connected — no
        # network I/O) so these tests fail if a future Python version
        # changes what it accepts, instead of asserting against a
        # hand-constructed exception that could drift from reality.
        def _side_effect(req, timeout=None):
            conn = http.client.HTTPConnection("localhost", 1)
            conn.putrequest("GET", "/x", skip_host=True,
                             skip_accept_encoding=True)
            conn.putheader("If-None-Match", value)
            raise AssertionError(
                "putheader() did not raise for %r as expected" % (value,)
            )
        return _side_effect

    def test_header_injection_etag_falls_back_to_stale_cache_like_any_other_failure(self):
        # A string etag that is nonetheless an unsafe HTTP header value (a
        # bare newline — header injection) isn't rejected by
        # _load_fetch_meta's type check (it's a string) but is rejected by
        # http.client.putheader() itself, raising ValueError. Confirmed
        # empirically before adding ValueError to fetch_url's caught
        # exceptions.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"stale content"),
                "etag": "abc\ninjected: header",
            }, f)

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=self._putheader_failure("abc\ninjected: header"),
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "stale content")

    def test_non_latin1_etag_falls_back_to_stale_cache_like_any_other_failure(self):
        # A string etag containing a character outside Latin-1 (an
        # upstream that started sending exotic characters, or a hand edit)
        # isn't rejected by _load_fetch_meta's type check either, but
        # http.client.putheader()'s str.encode('latin-1') raises
        # UnicodeEncodeError — a ValueError subclass, confirmed
        # empirically — while building the request.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"stale content"),
                "etag": "etag-日本語",
            }, f)

        with mock.patch(
            "urllib.request.urlopen",
            side_effect=self._putheader_failure("etag-日本語"),
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "stale content")

    def test_stale_past_max_age_sends_conditional_headers_from_sidecar(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"stale content"),
                "etag": '"abc123"', "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            }, f)

        captured = {}

        def _capture(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            captured["if_modified_since"] = req.get_header("If-modified-since")
            return _FakeResponse(b"fresh content")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(captured["if_none_match"], '"abc123"')
        self.assertEqual(captured["if_modified_since"], "Wed, 01 Jan 2026 00:00:00 GMT")

    def test_no_conditional_headers_sent_without_a_sidecar(self):
        # A cache file written before this feature existed has no sidecar
        # at all — must fall back to a plain unconditional GET, not crash.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))

        captured = {}

        def _capture(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            captured["if_modified_since"] = req.get_header("If-modified-since")
            return _FakeResponse(b"fresh content")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertIsNone(captured["if_none_match"])
        self.assertIsNone(captured["if_modified_since"])

    def test_mismatched_content_hash_sends_no_conditional_headers(self):
        # Simulates the interleaving Codex flagged: two processes racing
        # to refresh the same stale cache pair one response's body with a
        # *different* response's sidecar (independently atomic writes,
        # not atomic as a pair). The sidecar's content_hash then no longer
        # matches the file's actual bytes — the fix is to distrust the
        # validators in that case and force an unconditional GET, rather
        # than risk a 304 that's valid for the sidecar's response but not
        # for the body actually on disk.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("body from process A")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"body from process B"),
                "etag": '"etag-from-process-b"',
            }, f)

        captured = {}

        def _capture(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            return _FakeResponse(b"resynced content")

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertIsNone(captured["if_none_match"])
        with open(cache_path) as f:
            self.assertEqual(f.read(), "resynced content")
        # The resync must be self-healing: the rewritten sidecar has to match
        # the rewritten body, or the next refresh repeats the same mismatch.
        with open(self._meta_path(cache_path), encoding="utf-8") as f:
            self.assertEqual(
                json.load(f)["content_hash"],
                _common._content_hash(b"resynced content"),
            )

    def test_truncated_gzip_stream_is_treated_as_a_transport_failure(self):
        # gzip.decompress() raises EOFError/zlib.error for a truncated or
        # corrupt stream — neither is an OSError subclass, so without
        # explicit handling these would escape as a raw traceback instead
        # of hitting the same stale-serve/exit/raise path as any other
        # transport failure.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        truncated = gzip.compress(b"x" * 1000)[:-5]
        resp = _FakeResponse(truncated, headers={
            "Content-Encoding": "gzip", "Content-Length": str(len(truncated)),
        })
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "stale content")

    def test_truncated_gzip_stream_with_no_cache_exits_1(self):
        cache_path = self._cache_path()
        truncated = gzip.compress(b"x" * 1000)[:-5]
        resp = _FakeResponse(truncated, headers={
            "Content-Encoding": "gzip", "Content-Length": str(len(truncated)),
        })
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(cache_path))

    def test_304_bumps_mtime_without_rewriting_or_redownloading_content(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("still valid content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"still valid content"),
                "etag": '"abc123"',
            }, f)

        captured = {}

        def _raise_304(req, timeout=None):
            captured["if_none_match"] = req.get_header("If-none-match")
            raise _http_error(304)

        with mock.patch(
            "urllib.request.urlopen", side_effect=_raise_304
        ) as mock_urlopen:
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        mock_urlopen.assert_called_once()
        # Confirms this 304 followed a real conditional request (matching
        # the sidecar's content_hash) rather than an unconditional one a
        # real server would never 304.
        self.assertEqual(captured["if_none_match"], '"abc123"')
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "still valid content")
        # The sidecar is untouched (still just the original content_hash/
        # etag, no last_modified key added) — a 304 means "your validators
        # are still current," not "here is a fresh response to re-persist."
        with open(self._meta_path(cache_path), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {
                "content_hash": _common._content_hash(b"still valid content"),
                "etag": '"abc123"',
            })
        age = time.time() - os.path.getmtime(cache_path)
        self.assertLess(age, 5)

    def test_utime_failure_after_304_falls_back_to_stale_cache_like_any_other_failure(self):
        # os.utime() can raise OSError (read-only filesystem, cache deleted
        # concurrently, ownership change, ...). It runs inside the
        # `except HTTPError` block handling the 304 itself, so the sibling
        # `except (..., OSError, ...)` below it can never catch it — that
        # sibling only covers the try block, not other except blocks. Left
        # unhandled, a *successful* conditional request would end in a raw
        # traceback instead of the documented stale-cache fallback.
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("still valid content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with open(self._meta_path(cache_path), "w", encoding="utf-8") as f:
            json.dump({
                "content_hash": _common._content_hash(b"still valid content"),
                "etag": '"abc123"',
            }, f)

        with mock.patch(
            "urllib.request.urlopen", side_effect=_http_error(304)
        ), mock.patch(
            "os.utime", side_effect=OSError("Read-only file system")
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "still valid content")

    def test_304_with_no_existing_cache_is_a_normal_failure(self):
        # Can't happen via this function's own request (no cache means no
        # conditional headers are ever sent) but a defensively-coded path
        # for a server that 304s an unconditional request anyway: there is
        # no body to fall back to, so this must behave like any other
        # fetch failure with nothing cached — not crash on the 304 itself.
        cache_path = self._cache_path()
        with mock.patch(
            "urllib.request.urlopen", side_effect=_http_error(304)
        ):
            with self.assertRaises(SystemExit) as cm:
                _common.fetch_url(
                    "https://example.com/x", cache_path, user_agent="ua"
                )
        self.assertEqual(cm.exception.code, 1)

    def test_non_304_http_error_falls_back_to_stale_cache_like_any_other_failure(self):
        cache_path = self._cache_path()
        with open(cache_path, "w") as f:
            f.write("stale content")
        old_time = time.time() - 8 * 86400
        os.utime(cache_path, (old_time, old_time))
        with mock.patch(
            "urllib.request.urlopen", side_effect=_http_error(404)
        ):
            result = _common.fetch_url(
                "https://example.com/x", cache_path, user_agent="ua",
                max_age=604800,
            )
        self.assertEqual(result, cache_path)
        with open(cache_path) as f:
            self.assertEqual(f.read(), "stale content")


class LoadLinesTest(unittest.TestCase):
    def test_invalid_utf8_is_replaced_not_raised(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "bad.txt")
        with open(path, "wb") as f:
            f.write(b"hello \xff\xfe world\n")
        lines = _common.load_lines(path)  # must not raise UnicodeDecodeError
        self.assertEqual(len(lines), 1)
        self.assertIn("hello", lines[0])
        self.assertIn("world", lines[0])

    def test_valid_utf8_round_trips_unchanged(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "good.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("非ASCII テキスト\n")
        lines = _common.load_lines(path)
        self.assertEqual(lines, ["非ASCII テキスト\n"])


class NoValidatorRedirectHandlerTest(unittest.TestCase):
    """Drives the real (base-class) redirect_request() logic directly —
    no network, no mocked urlopen — to prove the actual stdlib interaction
    this depends on, the same way the putheader() tests above do for a
    different stdlib boundary. A live end-to-end test through a real
    redirecting server would prove the same thing less directly, at the
    cost of making the whole suite network-dependent; this file's tests
    are deliberately network-free (see the module docstring).
    """

    def test_conditional_headers_are_stripped_from_the_redirected_request(self):
        handler = _common._NoValidatorRedirectHandler()
        req = urllib.request.Request(
            "https://example.com/old",
            headers={
                "If-None-Match": '"stale-etag"',
                "If-Modified-Since": "Wed, 01 Jan 2026 00:00:00 GMT",
                "User-Agent": "ua",
            },
        )
        new_req = handler.redirect_request(
            req, fp=None, code=302, msg="Found", headers={},
            newurl="https://example.com/new",
        )
        self.assertIsNotNone(new_req)
        self.assertNotIn("If-none-match", new_req.headers)
        self.assertNotIn("If-modified-since", new_req.headers)
        # Confirms this exercised the base class's real redirect-following
        # logic (constructing an actual new Request for the new URL, method
        # preserved, other headers intact) rather than a stub that merely
        # returns something non-None.
        self.assertEqual(new_req.full_url, "https://example.com/new")
        self.assertEqual(new_req.get_method(), "GET")
        self.assertEqual(new_req.headers.get("User-agent"), "ua")

    def test_redirect_handler_is_installed_as_the_process_default_opener(self):
        # fetch_url relies on plain urllib.request.urlopen() picking up
        # this handler automatically (see _common's module-level
        # install_opener call) rather than callers having to build/pass
        # a custom opener themselves.
        opener = urllib.request._opener
        self.assertIsNotNone(opener)
        self.assertTrue(
            any(isinstance(h, _common._NoValidatorRedirectHandler)
                for h in opener.handlers)
        )


if __name__ == "__main__":
    unittest.main()
