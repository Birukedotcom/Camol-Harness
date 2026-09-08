#!/usr/bin/env python3
"""Verify checked-in Homebrew source-resource hashes without executing packages."""

import concurrent.futures
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def verify(resource):
    name, url, expected = resource
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org" or parsed.username or parsed.password:
        raise ValueError("resource must come from the declared HTTPS package host")
    digest = hashlib.sha256()
    count = 0
    with urlopen(Request(url, headers={"User-Agent": "camol-release-verifier/1"}), timeout=30) as response:
        if urlsplit(response.url).hostname != parsed.hostname:
            raise ValueError("unexpected package redirect host")
        while True:
            data = response.read(1 << 20)
            if not data:
                break
            count += len(data)
            if count > 64 << 20:
                raise ValueError("source resource exceeds verification bound")
            digest.update(data)
    if digest.hexdigest() != expected:
        raise ValueError("{} source checksum mismatch".format(name))
    return dict(resource=name, bytes=count, sha256=expected, verified=True)


def main():
    formula = Path(__file__).resolve().parents[1] / "Formula" / "camol.rb"
    resources = re.findall(r'resource "([^"]+)" do\s+url "([^"]+)"\s+sha256 "([0-9a-f]{64})"', formula.read_text(encoding="utf-8"))
    if len(resources) != formula.read_text(encoding="utf-8").count("resource \""):
        raise ValueError("resource blocks are malformed or missing checksums")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(verify, resources))
    print(json.dumps(dict(verified=True, resources=results), indent=2))


if __name__ == "__main__":
    main()
