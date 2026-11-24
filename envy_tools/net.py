"""Talking to GitHub: the only network ENVY Tools uses, and every request is logged.

* The latest version comes from the ``/releases/latest`` redirect (no API key, no
  API rate limit).
* Downloads are HTTPS only and may only be served from GitHub's own hosts, even
  after redirects.
"""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .catalog import OWNER, ToolsError

ALLOWED_HOSTS = ("github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")
USER_AGENT = f"envy-tools/{__version__}"
_TAG = re.compile(r"^v\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise ToolsError(f"refusing to fetch from {parts.scheme}://{parts.hostname}: not a GitHub host")


class _Guard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _context():
    # Standalone builds can't rely on finding the OS certificate store, so use
    # certifi's Mozilla roots (bundled), falling back to the system defaults.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        return ssl.create_default_context()


class Http:
    """Real network access. Tests swap in a fake with the same three methods."""

    def __init__(self, log=print, timeout: float = 30):
        self.log = log
        self.timeout = timeout
        ctx = _context()
        https = urllib.request.HTTPSHandler(context=ctx)
        self._open = urllib.request.build_opener(https, _Guard()).open
        self._open_nofollow = urllib.request.build_opener(https, _NoFollow()).open

    def _req(self, url):
        _check_url(url)
        self.log(f"→ GET {url}")
        return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    def latest_tag(self, repo: str) -> str:
        url = f"https://github.com/{OWNER}/{repo}/releases/latest"
        try:
            resp = self._open_nofollow(self._req(url), timeout=self.timeout)
            location = resp.headers.get("Location") or resp.geturl()
            resp.close()
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "")
            elif exc.code == 404:
                raise ToolsError(f"{repo} has no published release yet") from None
            else:
                raise ToolsError(f"GitHub answered {exc.code} for {repo}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ToolsError(f"couldn't reach GitHub ({getattr(exc, 'reason', exc)}). Are you online?") from None
        tag = location.rstrip("/").rsplit("/", 1)[-1]
        if "/releases/tag/" not in location or not _TAG.match(tag):
            raise ToolsError(f"couldn't work out the latest release of {repo}")
        return tag

    def get_text(self, url: str, limit: int = 2_000_000) -> str:
        try:
            with self._open(self._req(url), timeout=self.timeout) as resp:
                data = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise ToolsError(f"GitHub answered {exc.code} for {url}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ToolsError(f"download failed ({getattr(exc, 'reason', exc)})") from None
        if len(data) > limit:
            raise ToolsError(f"{url} is unexpectedly large")
        return data.decode("utf-8")

    def download(self, url: str, dest: Path, progress=None, limit: int = 1_500_000_000) -> None:
        got = 0
        try:
            with self._open(self._req(url), timeout=self.timeout) as resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit:
                        raise ToolsError("download is unexpectedly large; stopped")
                    out.write(chunk)
                    if progress:
                        progress(got, total)
        except urllib.error.HTTPError as exc:
            raise ToolsError(f"GitHub answered {exc.code} for {url}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ToolsError(f"download failed ({getattr(exc, 'reason', exc)})") from None


def parse_sums(text: str) -> dict[str, str]:
    """SHA256SUMS.txt → {file name: hex digest}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64}) [ *]?(\S+)$", line.strip())
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out
