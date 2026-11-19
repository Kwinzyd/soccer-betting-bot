"""BallDontLie client with explicit failure modes. Transport is injectable,
so every behaviour here is tested offline with a fake.

* Endpoint versions are centralised in ENDPOINT_VERSION and validated: an
  unknown league raises instead of silently 404-ing (the legacy loggers
  each carried their own copy of this map).
* 401/403 -> AuthError (fail loud; the legacy logger logged "0 quotes" and
  carried on through a day of 401s).
* 429 / 5xx / timeouts -> bounded retries with backoff, then ProviderError.
* Every page must be a dict with a list under 'data'; anything else is
  IncompleteResponse. Pagination stops only on a null cursor; a repeated
  cursor or exceeding max_pages raises.
* Each raw page can be archived via an `on_page(url, status, body)` hook.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.balldontlie.io"
ENDPOINT_VERSION = {"epl": "v2", "laliga": "v1", "seriea": "v1", "ligue1": "v1"}
ENDPOINTS = {"matches", "teams", "team_match_stats", "odds"}


class ProviderError(RuntimeError):
    pass


class AuthError(ProviderError):
    pass


class IncompleteResponse(ProviderError):
    pass


def url_for(league, endpoint, params):
    if league not in ENDPOINT_VERSION:
        raise ValueError(f"no endpoint version configured for league {league!r}")
    if endpoint not in ENDPOINTS:
        raise ValueError(f"unknown endpoint {endpoint!r}")
    q = urllib.parse.urlencode(params, doseq=True)
    return f"{BASE}/{league}/{ENDPOINT_VERSION[league]}/{endpoint}" + (f"?{q}" if q else "")


def urllib_transport(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (fixed https host)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Client:
    def __init__(self, key, transport=urllib_transport, max_retries=4, backoff=(5, 15, 30, 60),
                 spacing=13.0, sleep=time.sleep, on_page=None):
        if not key:
            raise AuthError("no API key configured")
        self._key = key
        self.transport, self.max_retries, self.backoff = transport, max_retries, backoff
        self.spacing, self.sleep, self.on_page = spacing, sleep, on_page

    def get(self, url):
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                status, body = self.transport(url, {"Authorization": self._key})
            except Exception as exc:                                  # noqa: BLE001 (timeouts, resets)
                status, body, last = None, b"", f"{type(exc).__name__}"
            if self.on_page:
                self.on_page(url, status, body)
            if status in (401, 403):
                raise AuthError(f"HTTP {status} from provider (check API key)")
            if status == 200:
                try:
                    data = json.loads(body)
                except ValueError as exc:
                    raise IncompleteResponse("response is not JSON") from exc
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise IncompleteResponse("response missing 'data' list")
                return data
            if status is not None and status not in (429,) and status < 500:
                raise ProviderError(f"HTTP {status}")
            last = last or f"HTTP {status}"
            if attempt < self.max_retries:
                self.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        raise ProviderError(f"gave up after {self.max_retries + 1} attempts ({last})")

    def paginate(self, league, endpoint, params, max_pages=50):
        params = dict(params)
        params.setdefault("per_page", 100)
        out, seen, cursor = [], set(), None
        for _ in range(max_pages):
            p = dict(params, **({"cursor": cursor} if cursor is not None else {}))
            data = self.get(url_for(league, endpoint, p))
            out.extend(data["data"])
            cursor = (data.get("meta") or {}).get("next_cursor")
            if cursor is None:
                return out
            if cursor in seen:
                raise IncompleteResponse(f"repeated pagination cursor {cursor}")
            seen.add(cursor)
            self.sleep(self.spacing)
        raise IncompleteResponse(f"more than {max_pages} pages")
