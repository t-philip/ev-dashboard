"""Map tiles for the trips pages: fetched by this server, cached on disk, served from this server.

Off unless the owner turns it on (``EVDASH_MAP_TILES=1``). The browser never talks to the tile
service: it asks this server for ``/tiles/<z>/<x>/<y>.png`` and this server fetches the tile once,
keeps it, and hands it over. The tile service therefore sees this server's address, never the
viewer's device, and no page URL. It does see the tile numbers requested, which locate the area
being viewed: that is the cost of having a map, and why the feature is opt-in.

Only a fixed address template is ever fetched, with whole numbers substituted into it, so the
route cannot be used to reach any other address. Redirects are refused, a reply must be a small
PNG, uncached fetches are rate limited and the cache has a size limit.
"""
from __future__ import annotations

import collections
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("evdash.tiles")

DEFAULT_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MIN_ZOOM, MAX_ZOOM = 2, 17
MAX_TILE_BYTES = 300_000
CACHE_DAYS = 30
MAX_CACHE_BYTES = 200_000_000
MAX_FETCHES_PER_MINUTE = 150  # panning and zooming ask for a screenful of tiles at a time
_PNG = b"\x89PNG\r\n\x1a\n"


def valid(z: int, x: int, y: int) -> bool:
    return (all(isinstance(v, int) and not isinstance(v, bool) for v in (z, x, y))
            and MIN_ZOOM <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _download(url: str, user_agent: str) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with opener.open(request, timeout=10) as reply:
        return reply.read(MAX_TILE_BYTES + 1)


class TileCache:
    def __init__(self, directory: Path, url_template: str = DEFAULT_URL, user_agent: str = "evdash",
                 download: Callable[[str, str], bytes] = _download,
                 clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.url_template = url_template
        self.user_agent = user_agent
        self._download = download
        self._clock = clock
        self._lock = threading.Lock()
        self._recent: collections.deque = collections.deque()
        self._size: Optional[int] = None

    def _path(self, z: int, x: int, y: int) -> Path:
        return self.directory / str(z) / str(x) / f"{y}.png"

    def _allowed_now(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._recent and now - self._recent[0] > 60:
                self._recent.popleft()
            if len(self._recent) >= MAX_FETCHES_PER_MINUTE:
                return False
            self._recent.append(now)
            return True

    def _cache_size(self) -> int:
        if self._size is None:
            total = 0
            for root, _, files in os.walk(self.directory):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
            self._size = total
        return self._size

    def _store(self, path: Path, data: bytes) -> None:
        try:
            replaced = path.stat().st_size  # a refreshed tile replaces the old one: count the difference
        except OSError:
            replaced = 0
        with self._lock:
            if self._cache_size() - replaced + len(data) > MAX_CACHE_BYTES:
                return  # full: serve without keeping
            self._size = self._cache_size() - replaced + len(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def get(self, z: int, x: int, y: int) -> Optional[bytes]:
        """The tile as PNG bytes, or None when it is not available."""
        if not valid(z, x, y):
            return None
        path = self._path(z, x, y)
        stale: Optional[bytes] = None
        try:
            data = path.read_bytes()
            if data.startswith(_PNG):
                if self._clock() - path.stat().st_mtime < CACHE_DAYS * 86400:
                    return data
                stale = data
        except OSError:
            pass
        if not self._allowed_now():
            return stale
        url = self.url_template.format(z=z, x=x, y=y)
        try:
            data = self._download(url, self.user_agent)
        except (urllib.error.URLError, OSError, ValueError) as err:
            log.warning("tile fetch failed: %s", type(err).__name__)  # class only: never the address
            return stale
        if not data.startswith(_PNG) or len(data) > MAX_TILE_BYTES:
            log.warning("tile fetch returned something that is not a small PNG")
            return stale
        self._store(path, data)
        return data
