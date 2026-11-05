"""Small API clients for Sonarr, Radarr, Bazarr and Tautulli."""
from __future__ import annotations

import httpx

from . import settings
from .redact import redact


class ServiceError(RuntimeError):
    """Errors from *arr/Tautulli calls. Messages are redacted: httpx errors embed the full URL,
    and Tautulli's URL carries its API key."""

    def __init__(self, message: str = ""):
        super().__init__(redact(message))


class Client:
    service = ""
    header = "X-Api-Key"

    def __init__(self, url: str | None = None, api_key: str | None = None):
        cfg = settings.get().get(self.service, {})
        self.url = (url if url is not None else cfg.get("url", "")).rstrip("/")
        self.key = api_key if api_key is not None else cfg.get("api_key", "")

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def req(self, method: str, path: str, **kw):
        if not self.configured:
            raise ServiceError(f"{self.service} is not configured")
        headers = {self.header: self.key, **kw.pop("headers", {})}
        try:
            r = httpx.request(method, self.url + path, headers=headers, timeout=kw.pop("timeout", 60), **kw)
        except httpx.HTTPError as e:
            raise ServiceError(f"{self.service}: {e}") from e
        if r.status_code >= 400:
            raise ServiceError(f"{self.service} {method} {path} -> {r.status_code}: {r.text[:300]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path, **params):
        return self.req("GET", path, params=params)


class Sonarr(Client):
    service = "sonarr"

    def status(self):
        return self.get("/api/v3/system/status")

    def series(self, series_id: int | None = None):
        return self.get(f"/api/v3/series/{series_id}") if series_id else self.get("/api/v3/series")

    def episodes(self, series_id: int):
        return self.get("/api/v3/episode", seriesId=series_id)

    def episode_files(self, series_id: int):
        return self.get("/api/v3/episodefile", seriesId=series_id)

    def grab_history(self, series_id: int, episode_id: int):
        rows = self.get("/api/v3/history/series", seriesId=series_id, episodeId=episode_id, eventType=1) or []
        return sorted(rows, key=lambda r: r.get("date", ""), reverse=True)

    def mark_failed(self, history_id: int):
        return self.req("POST", f"/api/v3/history/failed/{history_id}")

    def delete_file(self, file_id: int):
        return self.req("DELETE", f"/api/v3/episodefile/{file_id}")

    def search(self, episode_ids: list[int]):
        return self.req("POST", "/api/v3/command", json={"name": "EpisodeSearch", "episodeIds": episode_ids})

    def history_since(self, iso: str) -> list[dict]:
        return self.get("/api/v3/history/since", date=iso) or []


class Radarr(Client):
    service = "radarr"

    def status(self):
        return self.get("/api/v3/system/status")

    def movies(self, movie_id: int | None = None):
        return self.get(f"/api/v3/movie/{movie_id}") if movie_id else self.get("/api/v3/movie")

    def grab_history(self, movie_id: int):
        rows = self.get("/api/v3/history/movie", movieId=movie_id, eventType=1) or []
        return sorted(rows, key=lambda r: r.get("date", ""), reverse=True)

    def mark_failed(self, history_id: int):
        return self.req("POST", f"/api/v3/history/failed/{history_id}")

    def delete_file(self, file_id: int):
        return self.req("DELETE", f"/api/v3/moviefile/{file_id}")

    def search(self, movie_ids: list[int]):
        return self.req("POST", "/api/v3/command", json={"name": "MoviesSearch", "movieIds": movie_ids})

    def history_since(self, iso: str) -> list[dict]:
        return self.get("/api/v3/history/since", date=iso) or []


class Bazarr(Client):
    service = "bazarr"
    header = "X-API-KEY"

    def status(self):
        return self.get("/api/system/status")

    def search_episode(self, series_id: int, episode_id: int, lang2: str, forced=False, hi=False):
        return self.req("PATCH", "/api/episodes/subtitles", timeout=300, params={
            "seriesid": series_id, "episodeid": episode_id, "language": lang2,
            "forced": str(forced).lower(), "hi": str(hi).lower()})

    def search_movie(self, radarr_id: int, lang2: str, forced=False, hi=False):
        return self.req("PATCH", "/api/movies/subtitles", timeout=300, params={
            "radarrid": radarr_id, "language": lang2, "forced": str(forced).lower(), "hi": str(hi).lower()})

    # manual search + download (used to pick one provider, e.g. Whisper). Bazarr's Whisper provider transcribes
    # while downloading, so the download call can take many minutes.
    def provider_results(self, episode_id: int | None = None, radarr_id: int | None = None) -> list[dict]:
        r = (self.get("/api/providers/episodes", episodeid=episode_id) if episode_id is not None
             else self.get("/api/providers/movies", radarrid=radarr_id))
        return (r or {}).get("data", []) if isinstance(r, dict) else (r or [])

    def provider_download(self, result: dict, series_id: int | None = None, episode_id: int | None = None,
                          radarr_id: int | None = None, timeout: int = 3600):
        params = {"hi": str(result.get("hearing_impaired", False)).lower(), "forced": str(result.get("forced", False)).lower(),
                  "original_format": str(result.get("original_format", False)).lower(),
                  "provider": result["provider"], "subtitle": result["subtitle"]}
        if result.get("language"):
            params["language"] = result["language"]
        if episode_id is not None:
            params.update(seriesid=series_id, episodeid=episode_id)
            return self.req("POST", "/api/providers/episodes", timeout=timeout, params=params, data=params)
        params["radarrid"] = radarr_id
        return self.req("POST", "/api/providers/movies", timeout=timeout, params=params, data=params)


class Tautulli(Client):
    service = "tautulli"

    def call(self, cmd: str, **params):
        if not self.configured:
            raise ServiceError("tautulli is not configured")
        try:
            r = httpx.get(self.url + "/api/v2", params={"apikey": self.key, "cmd": cmd, **params}, timeout=15)
            r.raise_for_status()
            data = r.json()["response"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise ServiceError(f"tautulli: {e}") from e
        if data.get("result") != "success":
            raise ServiceError(f"tautulli: {data.get('message')}")
        return data.get("data")

    def status(self):
        return self.call("get_server_info")

    def stream_count(self) -> int:
        d = self.call("get_activity") or {}
        return int(d.get("stream_count") or 0)

    def sessions(self) -> list[dict]:
        d = self.call("get_activity") or {}
        return [{"file": s.get("file") or "", "title": s.get("full_title") or s.get("title") or "",
                 "state": s.get("state", "")} for s in d.get("sessions") or []]


CLIENTS = {"sonarr": Sonarr, "radarr": Radarr, "bazarr": Bazarr, "tautulli": Tautulli}


def test(service: str, url: str | None = None, api_key: str | None = None) -> dict:
    cls = CLIENTS[service]
    c = cls(url, api_key if api_key not in (None, "", "********") else None)
    try:
        st = c.status()
        version = (st or {}).get("version") or (st or {}).get("data", {}).get("bazarr_version") \
            or (st or {}).get("pms_version") or "ok"
        return {"ok": True, "version": version}
    except ServiceError as e:
        return {"ok": False, "error": str(e)}
