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
    e = urllib.error.HTTPError(url, code, f"HTTP {code}", {}, None)
    _ERRORS.append(e)
    return e


# Closed at module teardown: an HTTPError left open is cleaned up by the GC
# with a ResourceWarning (Python 3.14), and when that GC runs inside another
# test that compares stderr exactly, the warning text lands in its stderr.
_ERRORS: list[urllib.error.HTTPError] = []


def tearDownModule():
    for e in _ERRORS:
        e.close()
    _ERRORS.clear()


class HttpErrorHelperTest(unittest.TestCase):
    def test_errors_from_the_helper_are_closed_at_module_teardown(self):
        # closing early is harmless: each error is used within the test
        # that made it
        e = _http_error(500)
        self.assertFalse(e.fp.closed)
        tearDownModule()
        self.assertTrue(e.fp.closed)


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
            "content_hash": _common._content_hash(b"hello"), "url": "https://example.com/x",
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
        self.assertEqual(meta, {"content_hash": _common._content_hash(b"hello"),
                                "url": "https://example.com/x"})

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
        self.assertEqual(meta, {"content_hash": _common._content_hash(b"hello"),
                                "url": "https://example.com/x"})

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


class RecordedUrlTest(unittest.TestCase):
    """The sidecar records the requested URL. A cache whose name doesn't
    carry the URL (a preset's ``cache_name``) is fetched again when the URL
    it is asked for differs from the recorded one — even with
    ``max_age=None`` — and is never served for the other URL."""

    OLD = "https://old.example/llms-full.txt"
    NEW = "https://new.example/llms-full.txt"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache_path = os.path.join(self.tmp, "site-docs-llms-full.txt")
        with open(self.cache_path, "w") as f:
            f.write("old corpus")

    def _write_meta(self, **extra):
        meta = {"content_hash": _common._content_hash(b"old corpus"), "etag": '"old-etag"',
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT", **extra}
        with open(self.cache_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)

    def _read(self):
        with open(self.cache_path) as f:
            return f.read()

    def test_a_different_recorded_url_is_fetched_again_without_validators(self):
        self._write_meta(url=self.OLD)
        captured = {}

        def _capture(req, timeout=None):
            captured["url"] = req.full_url
            captured["if_none_match"] = req.get_header("If-none-match")
            captured["if_modified_since"] = req.get_header("If-modified-since")
            return _FakeResponse(b"new corpus", url=self.NEW)

        with mock.patch("urllib.request.urlopen", side_effect=_capture):
            result = _common.fetch_url(self.NEW, self.cache_path, user_agent="ua")
        self.assertEqual(result, self.cache_path)
        self.assertEqual(captured, {"url": self.NEW, "if_none_match": None, "if_modified_since": None})
        self.assertEqual(self._read(), "new corpus")
        with open(self.cache_path + ".meta.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f).get("url"), self.NEW)

    def test_the_same_recorded_url_or_none_serves_the_cache(self):
        # no url: a sidecar written before the url was recorded (or one whose
        # url isn't a string, which the loader discards) counts as a match,
        # so upgrading doesn't re-fetch every cache once
        for label, extra in (("same url", {"url": self.NEW}), ("no url", {}),
                             ("url not a string", {"url": 1})):
            with self.subTest(label):
                self._write_meta(**extra)
                # AssertionError is not among the errors fetch_url catches,
                # so a fetch reaches the test as a failure
                with mock.patch("urllib.request.urlopen",
                                side_effect=AssertionError("fetched although the cache matches")):
                    result = _common.fetch_url(self.NEW, self.cache_path, user_agent="ua")
                self.assertEqual(result, self.cache_path)
                self.assertEqual(self._read(), "old corpus")

    def test_a_failed_fetch_does_not_fall_back_to_the_other_urls_cache(self):
        failures = {
            "transport error": urllib.error.URLError("offline"),
            "HTTP error": _http_error(503, self.NEW),
            # validators are not sent, so a 304 can't confirm this cache
            "304": _http_error(304, self.NEW),
        }
        for label, error in failures.items():
            with self.subTest(label):
                self._write_meta(url=self.OLD)
                with mock.patch("urllib.request.urlopen", side_effect=error), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as cm:
                        _common.fetch_url(self.NEW, self.cache_path, user_agent="ua")
                self.assertEqual(cm.exception.code, 1)
                self.assertIn(f"Failed to fetch {self.NEW}", err.getvalue())
                self.assertNotIn("using cached copy", err.getvalue())
                with mock.patch("urllib.request.urlopen", side_effect=error), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(_common.FetchError):
                        _common.fetch_url(self.NEW, self.cache_path, user_agent="ua",
                                          max_age=604800, raise_on_error=True)
                self.assertEqual(self._read(), "old corpus")

    def test_a_failed_fetch_of_the_same_url_still_serves_the_stale_cache(self):
        self._write_meta(url=self.NEW)
        old_time = time.time() - 8 * 86400
        os.utime(self.cache_path, (old_time, old_time))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = _common.fetch_url(self.NEW, self.cache_path, user_agent="ua", max_age=604800)
        self.assertEqual(result, self.cache_path)
        self.assertIn("using cached copy", err.getvalue())

    def _served_offline(self, url):
        """What ``fetch_url(url)`` serves with the network down: the cached
        text, or ``None`` when it fails."""
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                path = _common.fetch_url(url, self.cache_path, user_agent="ua")
            except SystemExit:
                return None
        with open(path) as f:
            return f.read()

    def test_a_write_failing_after_a_url_change_never_serves_one_urls_body_for_the_other(self):
        """The cache holds OLD's body and sidecar; NEW is fetched and one of
        the writes fails (the old body's removal, the sidecar, or the body).
        The fetch fails, and afterwards neither URL is served the other's
        body (before, a failed sidecar write left NEW's body under OLD's
        sidecar, which a later request for OLD then served)."""
        real_write = _common._atomic_write
        real_unlink = os.unlink

        def failing_write(which):
            def write(path, data):
                if path.endswith(".meta.json") == (which == "sidecar"):
                    raise OSError(f"{which} not written")
                return real_write(path, data)
            return write

        def failing_unlink(path, *a, **kw):
            if path == self.cache_path:
                raise PermissionError("old body not removed")
            return real_unlink(path, *a, **kw)

        failures = {
            "sidecar": mock.patch.object(_common, "_atomic_write", side_effect=failing_write("sidecar")),
            "body": mock.patch.object(_common, "_atomic_write", side_effect=failing_write("body")),
            "old body removal": mock.patch.object(_common.os, "unlink", side_effect=failing_unlink),
        }
        for label, patch in failures.items():
            with self.subTest(label):
                with open(self.cache_path, "w") as f:
                    f.write("old corpus")
                self._write_meta(url=self.OLD)
                with patch, mock.patch("urllib.request.urlopen",
                                       return_value=_FakeResponse(b"new corpus", url=self.NEW)), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as cm:
                        _common.fetch_url(self.NEW, self.cache_path, user_agent="ua")
                self.assertEqual(cm.exception.code, 1)
                self.assertIn(f"Failed to fetch {self.NEW}", err.getvalue())
                self.assertIn(self._served_offline(self.OLD), (None, "old corpus"))
                self.assertIn(self._served_offline(self.NEW), (None, "new corpus"))

    # Failure points of one fetch, in write order: the sidecar written before
    # the body (only on a URL change), the body, the sidecar written last.
    _FAILURE_POINTS = ("body", "sidecar first", "sidecar last", "none")

    def _fetch_failing_at(self, url, failure, body, max_age=0):
        """``fetch_url(url)`` answered with *body*, one write failing at
        *failure*. Returns the text it served, or ``None`` when it failed."""
        real_write = _common._atomic_write
        written = {"body": False}

        def write(path, data):
            is_sidecar = path.endswith(".meta.json")
            point = ("sidecar last" if written["body"] else "sidecar first") if is_sidecar else "body"
            if point == failure:
                raise OSError(f"{point} not written")
            real_write(path, data)
            if not is_sidecar:
                written["body"] = True

        with mock.patch.object(_common, "_atomic_write", side_effect=write), \
                mock.patch("urllib.request.urlopen", return_value=_FakeResponse(body.encode(), url=url)), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                path = _common.fetch_url(url, self.cache_path, user_agent="ua", max_age=max_age)
            except SystemExit:
                return None
        with open(path) as f:
            return f.read()

    def _remove(self, path):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    def test_a_sidecar_naming_another_url_without_a_body_still_routes_through_the_url_change(self):
        """Only the sidecar is left (it names OLD; a body write failed after a
        URL change). NEW is fetched and its last sidecar write fails: the
        sidecar written first already names NEW, so OLD is not served NEW's
        body (before, the sidecar was not read without a body, so NEW's body
        was left under OLD's sidecar)."""
        os.unlink(self.cache_path)
        self._write_meta(url=self.OLD)
        self.assertEqual(self._fetch_failing_at(self.NEW, "sidecar last", "new corpus"), "new corpus")
        self.assertEqual(_common._load_fetch_meta(self.cache_path).get("url"), self.NEW)
        self.assertIsNone(self._served_offline(self.OLD))
        self.assertEqual(self._served_offline(self.NEW), "new corpus")

    def test_two_failed_writes_in_a_row_never_serve_one_urls_body_for_the_other(self):
        """A fetched, then B fetched with the body write failing (only B's
        sidecar is left), then A fetched with the last sidecar write failing.
        B must not then be served A's body."""
        A, B = self.OLD, self.NEW
        with self.subTest("A, B (body fails), A (last sidecar fails), B"):
            self._remove(self.cache_path)
            self.assertEqual(self._fetch_failing_at(A, "none", "A corpus"), "A corpus")
            self.assertIsNone(self._fetch_failing_at(B, "body", "B corpus"))
            self.assertFalse(os.path.exists(self.cache_path))
            self.assertEqual(self._fetch_failing_at(A, "sidecar last", "A corpus"), "A corpus")
            self.assertIn(self._served_offline(B), (None, "B corpus"))
            self.assertEqual(self._served_offline(A), "A corpus")

    def test_no_start_state_and_failure_point_serves_one_urls_body_for_the_other(self):
        """Every start (body present / absent × sidecar none / naming NEW /
        naming OLD) × every failure point of a fetch of NEW: neither that
        fetch nor a later request for either URL is served the other URL's
        body. A body is told apart by its text: ``old ...`` is OLD's,
        ``new ...`` is NEW's.

        The known exception is fixed here too: a body with no sidecar counts
        as a match for every URL (a cache written before the URL was
        recorded), so the cells that end with a body and no sidecar serve it
        for the other URL."""
        known_exception = {
            # the body write fails and the stale fallback serves OLD's body
            # (no sidecar says whose it is) for NEW, now and later
            ("body present", "sidecar none", "body"): {"fetch": "old corpus", self.NEW: "old corpus"},
            # NEW's body is written but its only sidecar write fails
            ("body present", "sidecar none", "sidecar last"): {self.OLD: "new corpus"},
            ("body absent", "sidecar none", "sidecar last"): {self.OLD: "new corpus"},
        }
        starts = {
            "sidecar none": (None, "old corpus"),
            "sidecar names NEW": (self.NEW, "new corpus (earlier)"),
            "sidecar names OLD": (self.OLD, "old corpus"),
        }
        owner = {"old": self.OLD, "new": self.NEW}
        seen_exceptions = {}
        for body_state in ("body present", "body absent"):
            for sidecar_state, (recorded, start_body) in starts.items():
                for failure in self._FAILURE_POINTS:
                    cell = (body_state, sidecar_state, failure)
                    with self.subTest(cell=cell):
                        self._remove(self.cache_path)
                        self._remove(self.cache_path + ".meta.json")
                        if body_state == "body present":
                            with open(self.cache_path, "w") as f:
                                f.write(start_body)
                        if recorded is not None:
                            self._write_meta(url=recorded)
                        served = {"fetch": self._fetch_failing_at(self.NEW, failure, "new corpus")}
                        for url in (self.OLD, self.NEW):
                            served[url] = self._served_offline(url)
                        wrong = {}
                        for who, text in served.items():
                            wanted = self.NEW if who == "fetch" else who
                            if text is not None and owner[text.split()[0]] != wanted:
                                wrong[who] = text
                        if wrong:
                            seen_exceptions[cell] = wrong
                        self.assertEqual(wrong, known_exception.get(cell, {}))
        self.assertEqual(seen_exceptions, known_exception)

    def test_a_url_change_writes_the_sidecar_before_the_body(self):
        """The order itself: a run stopped between the two writes (no
        exception to handle) leaves no body under the other URL's sidecar."""
        self._write_meta(url=self.OLD)
        seen = []
        real_write = _common._atomic_write

        def record(path, data):
            meta = _common._load_fetch_meta(self.cache_path)
            seen.append((os.path.basename(path), os.path.exists(self.cache_path), meta.get("url")))
            return real_write(path, data)

        with mock.patch.object(_common, "_atomic_write", side_effect=record), \
                mock.patch("urllib.request.urlopen", return_value=_FakeResponse(b"new corpus", url=self.NEW)):
            _common.fetch_url(self.NEW, self.cache_path, user_agent="ua")
        name = os.path.basename(self.cache_path)
        self.assertEqual(seen[:2], [(name + ".meta.json", False, self.OLD), (name, False, self.NEW)])
        self.assertEqual(self._read(), "new corpus")
        with open(self.cache_path + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual((meta["url"], meta["content_hash"]), (self.NEW, _common._content_hash(b"new corpus")))


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
