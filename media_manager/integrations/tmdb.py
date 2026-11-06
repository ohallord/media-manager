import logging
import httpx

from media_manager.integrations.base import BaseHTTPClient
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class TMDbClient(BaseHTTPClient):
    """
    Client for interacting with The Movie Database (TMDb) API v3.
    Docs: https://developer.themoviedb.org/reference/intro/getting-started
    """
    def __init__(self, api_url: str = "https://api.themoviedb.org/3", api_key: str = "", http_client: Optional[httpx.AsyncClient] = None):
        super().__init__(http_client)
        self.api_url = api_url.rstrip("/") if api_url else "https://api.themoviedb.org/3"
        self.api_key = api_key.strip()
        self.headers = {"Accept": "application/json"}
        # Support either v3 API key or v4 Bearer Access Token
        if self.api_key.startswith("eyJ"):
            self.headers["Authorization"] = f"Bearer {self.api_key}"
            self.params = {}
        else:
            self.params = {"api_key": self.api_key}

    async def _tmdb_get(self, path, *, params=None, timeout=10.0, default=None, label="TMDb request"):
        if not self.api_key:
            return default
        merged = {**self.params, **(params or {})}
        return await self._get_json(
            f"{self.api_url}{path}", timeout=timeout, headers=self.headers, params=merged,
            default=default, label=label, level=logging.WARNING,
        )

    async def _collect_pages(self, path, base_params, max_pages, label):
        if not self.api_key:
            return []
        results: List[Dict[str, Any]] = []
        try:
            async with self._client(timeout=10.0) as client:
                for page in range(1, max_pages + 1):
                    params = {**self.params, **base_params, "page": str(page)}
                    res = await client.get(f"{self.api_url}{path}", headers=self.headers, params=params)
                    if res.status_code != 200:
                        break
                    data = res.json()
                    results.extend(data.get("results", []))
                    if page >= data.get("total_pages", 1):
                        break
        except Exception as e:
            logger.warning("%s: %s", label, e)
        return results

    async def get_status(self) -> bool:
        """
        Tests API connection by validating credentials against TMDb /3/configuration endpoint.
        """
        if not self.api_key:
            return False
        return await self._get_ok(
            f"{self.api_url}/configuration", timeout=8.0, headers=self.headers, params=self.params,
            label="TMDb connection status check failed",
        )

    async def search_movie(self, title: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Searches TMDb for movies matching title.
        """
        params = {"query": title}
        if year:
            params["year"] = str(year)
        data = await self._tmdb_get("/search/movie", params=params, default={}, label=f"TMDb search_movie failed for '{title}'")
        return data.get("results", []) if isinstance(data, dict) else []

    async def search_tv(self, title: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Searches TMDb for TV series matching title.
        """
        params = {"query": title}
        if year:
            params["first_air_date_year"] = str(year)
        data = await self._tmdb_get("/search/tv", params=params, default={}, label=f"TMDb search_tv failed for '{title}'")
        return data.get("results", []) if isinstance(data, dict) else []

    async def get_movie_details(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        """
        Gets full movie metadata including cast, crew, overview, ratings, and videos.
        """
        data = await self._tmdb_get(
            f"/movie/{tmdb_id}", params={"append_to_response": "credits,videos"},
            label=f"TMDb get_movie_details failed for ID {tmdb_id}",
        )
        return data if isinstance(data, dict) else None

    @staticmethod
    def extract_trailer_url(details: Optional[Dict[str, Any]]) -> Optional[str]:
        """
        Picks the best YouTube trailer link out of a get_movie_details() response's
        appended `videos` data: prefers an official "Trailer", falls back to any
        YouTube "Trailer", then any YouTube "Teaser".
        """
        if not details:
            return None
        videos = (details.get("videos") or {}).get("results") or []
        youtube_videos = [v for v in videos if v.get("site") == "YouTube" and v.get("key")]
        if not youtube_videos:
            return None

        def best(video_type: str, official_only: bool) -> Optional[Dict[str, Any]]:
            candidates = [v for v in youtube_videos if v.get("type") == video_type]
            if official_only:
                candidates = [v for v in candidates if v.get("official")]
            return candidates[0] if candidates else None

        video = best("Trailer", official_only=True) or best("Trailer", official_only=False) or best("Teaser", official_only=False)
        return f"https://www.youtube.com/watch?v={video['key']}" if video else None

    async def get_tv_details(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        """
        Gets full TV show metadata including cast, crew, overview, and ratings.
        """
        data = await self._tmdb_get(
            f"/tv/{tmdb_id}", params={"append_to_response": "credits,videos"},
            label=f"TMDb get_tv_details failed for ID {tmdb_id}",
        )
        return data if isinstance(data, dict) else None

    async def get_tv_watch_providers(self, tmdb_id: int, region: str = "US") -> List[str]:
        """
        Gets the current flatrate (subscription) streaming provider names for a TV
        show in the given region. Informational only - TMDb doesn't expose a
        per-country release-type/date breakdown for TV the way it does for movies
        (via /movie/{id}/release_dates), so this can't be date-gated like the
        movie Coming Soon streaming detection.
        """
        data = await self._tmdb_get(
            f"/tv/{tmdb_id}/watch/providers", default={},
            label=f"TMDb get_tv_watch_providers failed for ID {tmdb_id}",
        )
        region_data = ((data or {}).get("results") or {}).get(region, {}) if isinstance(data, dict) else {}
        return [p["provider_name"] for p in region_data.get("flatrate", []) if p.get("provider_name")]

    async def get_movie_watch_providers(self, tmdb_id: int, region: str = "US") -> List[str]:
        """
        Gets the current flatrate (subscription) streaming provider names for a
        movie in the given region. Informational only, like get_tv_watch_providers -
        used by the title detail modal, not the date-gated Coming Soon streaming
        detection (which uses /movie/{id}/release_dates instead).
        """
        data = await self._tmdb_get(
            f"/movie/{tmdb_id}/watch/providers", default={},
            label=f"TMDb get_movie_watch_providers failed for ID {tmdb_id}",
        )
        region_data = ((data or {}).get("results") or {}).get(region, {}) if isinstance(data, dict) else {}
        return [p["provider_name"] for p in region_data.get("flatrate", []) if p.get("provider_name")]

    async def get_tv_external_ids(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        """
        Gets external IDs (tvdb_id, imdb_id, etc.) for a TV show. Needed to hand a
        TMDb-discovered show off to Sonarr, which is keyed by TVDB ID, not TMDb ID.
        """
        data = await self._tmdb_get(
            f"/tv/{tmdb_id}/external_ids",
            label=f"TMDb get_tv_external_ids failed for ID {tmdb_id}",
        )
        return data if isinstance(data, dict) else None

    async def get_movie_genres(self) -> Dict[int, str]:
        """
        Returns a map of {genre_id: genre_name} for movies.
        """
        data = await self._tmdb_get("/genre/movie/list", default={}, label="TMDb get_movie_genres failed")
        genres = data.get("genres", []) if isinstance(data, dict) else []
        return {g["id"]: g["name"] for g in genres if g.get("id") is not None and g.get("name")}

    async def get_tv_genres(self) -> Dict[int, str]:
        """
        Returns a map of {genre_id: genre_name} for TV shows (a distinct ID space
        from movie genres in TMDb).
        """
        data = await self._tmdb_get("/genre/tv/list", default={}, label="TMDb get_tv_genres failed")
        genres = data.get("genres", []) if isinstance(data, dict) else []
        return {g["id"]: g["name"] for g in genres if g.get("id") is not None and g.get("name")}

    async def discover_upcoming_movies(
        self,
        start_date: str,
        end_date: str,
        region: str = "US",
        max_pages: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Discovers movies with a confirmed release date in [start_date, end_date]
        (YYYY-MM-DD), restricted to release types (2=limited, 3=wide, 4=digital, 6=TV)
        in the given region. Filters/sorts on `release_date` in the given region so
        the date range correctly targets upcoming releases in the specified country.
        Pages are fetched most-popular-first so well-known titles aren't pushed out of the
        page budget by a long tail of obscure one-screen releases.
        """
        return await self._collect_pages("/discover/movie", {
            "region": region,
            "with_release_type": "2|3|4|6",
            "release_date.gte": start_date,
            "release_date.lte": end_date,
            "sort_by": "popularity.desc",
            "include_adult": "false",
        }, max_pages, "TMDb discover_upcoming_movies failed")

    async def discover_upcoming_tv(
        self,
        start_date: str,
        end_date: str,
        region: str = "US",
        max_pages: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Discovers TV series with a first-air-date in [start_date, end_date]
        (YYYY-MM-DD), restricted to the given origin country. Unlike movies, TV has
        no theatrical/streaming release-type breakdown to filter on - the
        first-air-date window alone is what keeps this to not-yet-aired series.
        Pages are fetched most-popular-first, same rationale as discover_upcoming_movies.
        """
        return await self._collect_pages("/discover/tv", {
            "with_origin_country": region,
            "first_air_date.gte": start_date,
            "first_air_date.lte": end_date,
            "sort_by": "popularity.desc",
            "include_adult": "false",
        }, max_pages, "TMDb discover_upcoming_tv failed")

    async def get_trending(
        self,
        media_type: str = "movie",
        time_window: str = "week",
        max_pages: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Gets currently-trending movies or TV shows (independent of release date),
        via /trending/{media_type}/{time_window}. `media_type` is "movie" or "tv",
        `time_window` is "day" or "week". Already popularity-ranked by TMDb, so
        this doesn't need as many pages as discover to surface well-known titles.
        """
        return await self._collect_pages(
            f"/trending/{media_type}/{time_window}", {}, max_pages, "TMDb get_trending failed",
        )

    async def _get_related(self, media_kind: str, tmdb_id: int, relation: str) -> List[Dict[str, Any]]:
        """Shared implementation for the four get_*_recommendations/get_similar_* methods."""
        data = await self._tmdb_get(
            f"/{media_kind}/{tmdb_id}/{relation}", default={},
            label=f"TMDb {media_kind}/{relation} failed for ID {tmdb_id}",
        )
        return data.get("results", []) if isinstance(data, dict) else []

    async def get_movie_recommendations(self, tmdb_id: int) -> List[Dict[str, Any]]:
        return await self._get_related("movie", tmdb_id, "recommendations")

    async def get_similar_movies(self, tmdb_id: int) -> List[Dict[str, Any]]:
        return await self._get_related("movie", tmdb_id, "similar")

    async def get_tv_recommendations(self, tmdb_id: int) -> List[Dict[str, Any]]:
        return await self._get_related("tv", tmdb_id, "recommendations")

    async def get_similar_tv(self, tmdb_id: int) -> List[Dict[str, Any]]:
        return await self._get_related("tv", tmdb_id, "similar")

    async def get_tv_episode_details(self, tmdb_id: int, season_number: int, episode_number: int) -> Optional[Dict[str, Any]]:
        """
        Gets TV episode metadata (name, overview, air_date, still_path, vote_average).
        """
        data = await self._tmdb_get(
            f"/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}",
            label=f"TMDb get_tv_episode_details failed for ID {tmdb_id} S{season_number}E{episode_number}",
        )
        return data if isinstance(data, dict) else None

    async def get_person_details(self, person_id: int) -> Optional[Dict[str, Any]]:
        """
        Gets details for a person (actor/crew), including biography.
        """
        data = await self._tmdb_get(f"/person/{person_id}", label=f"TMDb get_person_details failed for ID {person_id}")
        return data if isinstance(data, dict) else None

    async def find_tv_tmdb_id_by_tvdb(self, tvdb_id: int) -> Optional[int]:
        """Resolve a TVDB series id to a TMDb tv id via /find."""
        if not tvdb_id:
            return None
        data = await self._tmdb_get(
            f"/find/{tvdb_id}", params={"external_source": "tvdb_id"},
            label=f"TMDb find by tvdb {tvdb_id} failed",
        )
        results = ((data or {}).get("tv_results") or []) if isinstance(data, dict) else []
        try:
            return int(results[0]["id"]) if results and results[0].get("id") else None
        except (TypeError, ValueError, IndexError):
            return None
