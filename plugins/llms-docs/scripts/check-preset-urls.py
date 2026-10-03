#!/usr/bin/env python3
"""Check where every bundled preset's ``url`` / ``index_url`` points today.

A development tool, run by hand: it goes out to the network (one HEAD request
per URL, redirects not followed automatically), so it is not part of the test
suite. Sites move their ``llms-full.txt`` (a product renamed, a docs host
changed); the loader still follows a redirect, so a moved preset keeps working
and nobody notices until the old URL is gone.

Each URL is printed with its status. A 3xx is followed hop by hop and the chain
is shown with the final status, so a move to another path or host is visible;
a 404 (or any other failure) is listed as broken. The exit code is 0 when every
URL answers 200 directly, 1 otherwise, so a script can use it.

    python3 check-preset-urls.py
    python3 check-preset-urls.py --file path/to/sources.json
"""

import argparse
import concurrent.futures
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin

HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_FILE = os.path.join(HERE, "presets.json")
USER_AGENT = "llms-docs-check-preset-urls/1.0"
URL_FIELDS = ("url", "index_url")
MAX_HOPS = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back as an ``HTTPError`` instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def probe(url: str, timeout: float = 20.0) -> tuple:
    """``(status, location)`` of one HEAD request; ``status`` is ``0`` on a
    network failure (then *location* holds the reason). Falls back to GET for a
    server that refuses HEAD (405 / 501), without reading the body."""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status, None
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (405, 501):
                continue
            location = e.headers.get("Location") if e.headers else None
            return e.code, urljoin(url, location) if location else None
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, str(getattr(e, "reason", e))
    return 0, "no answer"


def check(url: str) -> dict:
    """Follow *url* hop by hop: ``{"url", "hops": [(status, location)], "final"}``."""
    hops = []
    current = url
    for _ in range(MAX_HOPS):
        status, location = probe(current)
        hops.append((status, location))
        if 300 <= status < 400 and location:
            current = location
            continue
        break
    return {"url": url, "hops": hops, "final": hops[-1][0]}


def verdict(result: dict) -> str:
    first = result["hops"][0][0]
    if first == 200:
        return "ok"
    if 300 <= first < 400:
        return "moved" if result["final"] == 200 else "moved-broken"
    return "broken"


def load_sources(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["sources"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--file", default=DEFAULT_FILE,
                        help="sources JSON to check (default: the bundled presets.json)")
    parser.add_argument("--workers", type=int, default=4, help="parallel requests (default: 4)")
    args = parser.parse_args(argv)

    sources = load_sources(args.file)
    jobs = [(name, field, profile[field])
            for name, profile in sorted(sources.items())
            for field in URL_FIELDS if profile.get(field)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda j: check(j[2]), jobs))

    bad = 0
    for (name, field, url), result in zip(jobs, results):
        state = verdict(result)
        if state != "ok":
            bad += 1
        print(f"{state:13} {name} {field}: {url}")
        if state != "ok":
            for status, location in result["hops"]:
                print(f"{'':13}   {status or 'ERR'} {location or ''}".rstrip())
    print()
    print(f"({len(jobs)} URLs, {bad} not answering 200 directly)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
