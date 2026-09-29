#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def deployment_target() -> str:
    return os.getenv("DEPLOYMENT_TARGET", "local").strip().lower()


def loader_connection_string() -> str:
    explicit = os.getenv("CB_LOADER_CONN_STRING", "").strip()
    if explicit:
        return explicit
    runtime = os.getenv("CB_CONN_STRING", "couchbase://localhost").strip()
    if deployment_target() == "local" and "host.docker.internal" in runtime:
        return runtime.replace("host.docker.internal", "localhost")
    return runtime


def loader_username() -> str:
    explicit = os.getenv("CB_LOADER_USERNAME", "").strip()
    if explicit:
        return explicit
    if deployment_target() == "local":
        return os.getenv("CB_ADMIN_USERNAME", "Administrator")
    return os.getenv(
        "CB_PROVISION_USERNAME",
        os.getenv("CB_ADMIN_USERNAME", os.getenv("CB_USERNAME", "Administrator")),
    )


def loader_password() -> str:
    explicit = os.getenv("CB_LOADER_PASSWORD", "")
    if explicit:
        return explicit
    if deployment_target() == "local":
        return os.getenv("CB_ADMIN_PASSWORD", "password")
    return os.getenv(
        "CB_PROVISION_PASSWORD",
        os.getenv("CB_ADMIN_PASSWORD", os.getenv("CB_PASSWORD", "password")),
    )


def loader_embedding_url() -> str:
    explicit = os.getenv("EMBEDDING_LOADER_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    runtime = os.getenv(
        "EMBEDDING_BASE_URL",
        os.getenv("OLLAMA_EMBED_HOST_URL", "http://localhost:11434"),
    ).strip()
    if deployment_target() == "local":
        runtime = runtime.replace("http://ollama:", "http://localhost:")
        runtime = runtime.replace("https://ollama:", "https://localhost:")
        runtime = runtime.replace("host.docker.internal", "localhost")
    return runtime.rstrip("/")


def loader_search_url() -> str:
    explicit = os.getenv("CB_SEARCH_HOST_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    runtime = os.getenv("CB_SEARCH_URL", "http://localhost:8094").strip()
    if deployment_target() == "local":
        runtime = runtime.replace("host.docker.internal", "localhost")
    return runtime.rstrip("/")


@dataclass(frozen=True)
class Config:
    tmdb_token: str = os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()
    language: str = os.getenv("TMDB_LANGUAGE", "en-GB")
    region: str = os.getenv("TMDB_REGION", "GB")
    movie_limit: int = int(os.getenv("TMDB_MOVIE_LIMIT", "120"))
    tv_limit: int = int(os.getenv("TMDB_TV_LIMIT", "60"))
    concurrency: int = int(os.getenv("TMDB_CONCURRENCY", "8"))
    sample_only: bool = env_bool("LOAD_SAMPLE_CATALOGUE", False)

    # Host-side tools may need different addresses from Docker containers.
    # For example, containers use host.docker.internal while a loader running
    # on macOS should connect through localhost and the published ports.
    cb_conn_string: str = loader_connection_string()
    cb_username: str = loader_username()
    cb_password: str = loader_password()
    cb_connect_timeout_seconds: int = int(os.getenv("CB_LOADER_CONNECT_TIMEOUT_SECONDS", os.getenv("CB_CONNECT_TIMEOUT_SECONDS", "30")))
    cb_connect_attempts: int = int(os.getenv("CB_LOADER_CONNECT_ATTEMPTS", "4"))
    cb_connect_retry_delay_seconds: float = float(os.getenv("CB_LOADER_CONNECT_RETRY_DELAY_SECONDS", "2"))
    bucket: str = os.getenv("CONTENT_BUCKET", "streaming")

    embed_url: str = loader_embedding_url()
    embed_model: str = os.getenv("EMBEDDING_MODEL", os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))

    search_url: str = loader_search_url()
    search_username: str = os.getenv("CB_SEARCH_ADMIN_USERNAME", os.getenv("CB_PROVISION_USERNAME", os.getenv("CB_ADMIN_USERNAME", cb_username)))
    search_password: str = os.getenv("CB_SEARCH_ADMIN_PASSWORD", os.getenv("CB_PROVISION_PASSWORD", os.getenv("CB_ADMIN_PASSWORD", cb_password)))
    search_index: str = os.getenv("CATALOGUE_SEARCH_INDEX", "streaming-catalogue-search")
    plan_cache_search_index: str = os.getenv(
        "PLAN_CACHE_SEARCH_INDEX", "streaming-plan-cache"
    )
    seed_demo_viewers: bool = env_bool("SEED_DEMO_VIEWERS", True)
    manage_search_index: bool = env_bool("MANAGE_SEARCH_INDEX", os.getenv("DEPLOYMENT_TARGET", "local").lower() == "local")
    wan_profile: bool = env_bool("CB_WAN_PROFILE", os.getenv("DEPLOYMENT_TARGET", "local").lower() == "capella")


class TMDBClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = httpx.AsyncClient(
            base_url="https://api.themoviedb.org/3",
            headers={
                "Authorization": f"Bearer {config.tmdb_token}",
                "accept": "application/json",
                "User-Agent": "Couchbase-StreamAI-Demo/1.1",
            },
            timeout=httpx.Timeout(40.0),
        )
        self.semaphore = asyncio.Semaphore(config.concurrency)

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        async with self.semaphore:
            for attempt in range(5):
                response = await self.client.get(path, params=params)
                if response.status_code == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            raise RuntimeError(f"TMDB rate limit persisted for {path}")

    async def discover(self, kind: str, limit: int) -> list[dict[str, Any]]:
        pages = max(1, (limit + 19) // 20)
        tasks = []
        for page in range(1, pages + 1):
            tasks.append(
                self.get(
                    f"/discover/{kind}",
                    language=self.config.language,
                    region=self.config.region,
                    sort_by="popularity.desc",
                    include_adult="false",
                    include_video="false",
                    page=page,
                )
            )
        payloads = await asyncio.gather(*tasks)
        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        for payload in payloads:
            for item in payload.get("results", []):
                tmdb_id = int(item["id"])
                if tmdb_id not in seen:
                    seen.add(tmdb_id)
                    results.append(item)
        return results[:limit]

    async def detail(self, kind: str, tmdb_id: int) -> dict[str, Any]:
        append = "credits,keywords,release_dates" if kind == "movie" else "credits,keywords,content_ratings"
        return await self.get(
            f"/{kind}/{tmdb_id}",
            language=self.config.language,
            append_to_response=append,
        )


def certification(detail: dict[str, Any], kind: str, region: str) -> str | None:
    if kind == "movie":
        release = detail.get("release_dates", {}).get("results", [])
        country = next((item for item in release if item.get("iso_3166_1") == region), None)
        if not country and region != "US":
            country = next((item for item in release if item.get("iso_3166_1") == "US"), None)
        for item in (country or {}).get("release_dates", []):
            value = str(item.get("certification") or "").strip()
            if value:
                return value
    else:
        ratings = detail.get("content_ratings", {}).get("results", [])
        country = next((item for item in ratings if item.get("iso_3166_1") == region), None)
        if not country and region != "US":
            country = next((item for item in ratings if item.get("iso_3166_1") == "US"), None)
        value = str((country or {}).get("rating") or "").strip()
        if value:
            return value
    return None


def demo_availability(identifier: str | int, region: str = "GB") -> dict[str, Any]:
    """Create deterministic subscription metadata for the governed-agent demo.

    This is demo business data, not TMDB availability data. Most titles are
    included in the standard plan; a small, repeatable subset demonstrates
    premium-tier and regional policy decisions.
    """
    digest = int(hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:8], 16)
    offer_type = "rent" if digest % 13 == 0 else "included"
    minimum_tier = "premium" if offer_type == "included" and digest % 11 == 0 else "standard"
    regions = [region, "IE"] if digest % 17 else ["US", "CA"]
    return {
        "available": True,
        "regions": list(dict.fromkeys(value.upper() for value in regions)),
        "minimumTier": minimum_tier,
        "offerType": offer_type,
        "source": "streamai_demo_entitlement_policy",
    }


def normalize_title(detail: dict[str, Any], kind: str, config: Config) -> dict[str, Any]:
    title = detail.get("title") if kind == "movie" else detail.get("name")
    original = detail.get("original_title") if kind == "movie" else detail.get("original_name")
    date = detail.get("release_date") if kind == "movie" else detail.get("first_air_date")
    genres = [item.get("name") for item in detail.get("genres", []) if item.get("name")]
    keywords_payload = detail.get("keywords", {})
    keywords = keywords_payload.get("keywords", []) if kind == "movie" else keywords_payload.get("results", [])
    keyword_names = [item.get("name") for item in keywords if item.get("name")][:20]
    credits = detail.get("credits", {})
    cast = [item.get("name") for item in credits.get("cast", []) if item.get("name")][:10]
    crew = credits.get("crew", [])
    directors = [
        item.get("name")
        for item in crew
        if item.get("job") in {"Director", "Series Director"} and item.get("name")
    ][:5]
    if kind == "tv":
        directors = list(dict.fromkeys([*(item.get("name") for item in detail.get("created_by", []) if item.get("name")), *directors]))[:5]
    runtimes = detail.get("episode_run_time") or []
    runtime = detail.get("runtime") if kind == "movie" else (runtimes[0] if runtimes else None)
    origin = detail.get("origin_country") or [item.get("iso_3166_1") for item in detail.get("production_countries", [])]
    languages = [item.get("english_name") or item.get("name") for item in detail.get("spoken_languages", []) if item.get("english_name") or item.get("name")]
    overview = str(detail.get("overview") or "").strip()
    search_text = " | ".join(
        filter(
            None,
            [
                str(title or ""),
                str(original or ""),
                overview,
                "Genres: " + ", ".join(genres),
                "Keywords: " + ", ".join(keyword_names),
                "Cast: " + ", ".join(cast),
                "Directors and creators: " + ", ".join(directors),
            ],
        )
    )
    embedding_text = (
        f"Title: {title}. Type: {'film' if kind == 'movie' else 'television series'}. "
        f"Genres: {', '.join(genres)}. Keywords and themes: {', '.join(keyword_names)}. "
        f"Overview: {overview}. Directors and creators: {', '.join(directors)}. "
        f"Main cast: {', '.join(cast[:6])}."
    )
    return {
        "id": f"{kind}::{detail['id']}",
        "type": "title",
        "tmdbId": int(detail["id"]),
        "contentType": kind,
        "title": title,
        "originalTitle": original,
        "overview": overview,
        "tagline": detail.get("tagline"),
        "releaseDate": date,
        "releaseYear": int(str(date)[:4]) if date and str(date)[:4].isdigit() else None,
        "genres": genres,
        "keywords": keyword_names,
        "castNames": cast,
        "directorNames": directors,
        "runtimeMinutes": int(runtime) if runtime else None,
        "episodeRuntimeMinutes": int(runtime) if kind == "tv" and runtime else None,
        "numberOfSeasons": detail.get("number_of_seasons") if kind == "tv" else None,
        "numberOfEpisodes": detail.get("number_of_episodes") if kind == "tv" else None,
        "ageRating": certification(detail, kind, config.region),
        "adult": bool(detail.get("adult", False)),
        "availability": demo_availability(detail["id"], config.region),
        "governedAgentSchemaVersion": 1,
        "originCountries": [item for item in origin if item],
        "spokenLanguages": languages,
        "originalLanguage": detail.get("original_language"),
        "voteAverage": float(detail.get("vote_average") or 0),
        "voteCount": int(detail.get("vote_count") or 0),
        "popularity": float(detail.get("popularity") or 0),
        "posterPath": detail.get("poster_path"),
        "backdropPath": detail.get("backdrop_path"),
        "homepage": detail.get("homepage"),
        "status": detail.get("status"),
        "searchText": search_text,
        "embeddingText": embedding_text,
        "source": "TMDB",
        "sourceAttribution": "This product uses the TMDB API but is not endorsed or certified by TMDB.",
        "ingestedAt": time.time(),
    }


class Embedder:
    def __init__(self, config: Config) -> None:
        self.config = config
        headers = {"Authorization": f"Bearer {os.getenv('EMBEDDING_API_KEY', '')}"} if os.getenv("EMBEDDING_API_KEY") else {}
        extra_headers = os.getenv("EMBEDDING_HEADERS_JSON", "")
        if extra_headers:
            headers.update(json.loads(extra_headers))
        self.send_input_type = env_bool("EMBEDDING_SEND_INPUT_TYPE", os.getenv("EMBEDDING_PROVIDER") == "capella_model_service")
        self.client = httpx.AsyncClient(base_url=config.embed_url.rstrip('/').removesuffix('/v1'), headers=headers, timeout=httpx.Timeout(180.0))

    def payload(self, value):
        body = {"model": self.config.embed_model, "input": value}
        if self.send_input_type:
            body["input_type"] = "passage"
        return body

    async def close(self) -> None:
        await self.client.aclose()

    async def batch(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self.client.post(
                "/v1/embeddings",
                json=self.payload(texts),
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item.get("index", 0))
            return [[float(value) for value in item["embedding"]] for item in data]
        except Exception:
            vectors = []
            for text in texts:
                response = await self.client.post(
                    "/v1/embeddings",
                    json=self.payload(text),
                )
                response.raise_for_status()
                vectors.append([float(value) for value in response.json()["data"][0]["embedding"]])
            return vectors


class CouchbaseLoader:
    def __init__(self, config: Config) -> None:
        from couchbase.auth import PasswordAuthenticator
        from couchbase.cluster import Cluster
        from couchbase.options import ClusterOptions

        self.config = config
        self.cluster = None
        last_error: Exception | None = None
        for attempt in range(1, max(1, config.cb_connect_attempts) + 1):
            auth = PasswordAuthenticator(config.cb_username, config.cb_password)
            options = ClusterOptions(auth)
            if config.wan_profile:
                options.apply_profile("wan_development")
            candidate = None
            try:
                candidate = Cluster.connect(config.cb_conn_string, options)
                candidate.wait_until_ready(timedelta(seconds=config.cb_connect_timeout_seconds))
                self.cluster = candidate
                break
            except Exception as exc:
                last_error = exc
                close = getattr(candidate, "close", None)
                if callable(close):
                    close()
                if attempt < max(1, config.cb_connect_attempts):
                    print(
                        f"  Couchbase loader connection attempt {attempt}/{config.cb_connect_attempts} "
                        f"failed for {config.cb_conn_string}: {type(exc).__name__}; retrying...",
                        file=sys.stderr,
                    )
                    time.sleep(config.cb_connect_retry_delay_seconds)
        if self.cluster is None:
            raise RuntimeError(
                f"Catalogue loader could not connect to Couchbase at {config.cb_conn_string} "
                f"after {config.cb_connect_attempts} attempts. For local Docker use "
                "CB_LOADER_CONN_STRING=couchbase://localhost."
            ) from last_error
        bucket = self.cluster.bucket(config.bucket)
        self.titles = bucket.scope("catalogue").collection("titles")
        viewers = bucket.scope("viewers")
        self.profiles = viewers.collection("profiles")
        self.history = viewers.collection("watch_history")
        self.interactions = viewers.collection("interactions")
        self.jobs = bucket.scope("operations").collection("ingestion_jobs")

    def close(self) -> None:
        close = getattr(self.cluster, "close", None)
        if callable(close):
            close()

    def upsert_titles(self, documents: list[dict[str, Any]]) -> None:
        from couchbase.exceptions import DocumentNotFoundException, CasMismatchException, DocumentExistsException
        from couchbase.options import ReplaceOptions
        for index, doc in enumerate(documents, start=1):
            for attempt in range(3):
                try:
                    try:
                        existing = self.titles.get(doc["id"])
                    except DocumentNotFoundException:
                        self.titles.insert(doc["id"], doc)
                    else:
                        updated = dict(doc)
                        saved = existing.content_as[dict].get("aiEnrichment")
                        if saved is not None:
                            updated["aiEnrichment"] = saved
                        # Keep saved guides across ingestion. Source fingerprints
                        # prevent the app reusing them if the synopsis changes.
                        self.titles.replace(doc["id"], updated, ReplaceOptions(cas=existing.cas))
                    break
                except (CasMismatchException, DocumentExistsException):
                    if attempt == 2:
                        raise
            if index % 25 == 0 or index == len(documents):
                print(f"  upserted {index}/{len(documents)} catalogue documents")

    def seed_viewers(self, documents: list[dict[str, Any]]) -> None:
        profiles = {
            "sai": {
                "displayName": "Sai",
                "preferredGenres": ["Science Fiction", "Thriller", "Crime"],
                "dislikedGenres": ["Horror"],
                "preferredPeople": ["Christopher Nolan", "Denis Villeneuve"],
            },
            "maya": {
                "displayName": "Maya",
                "preferredGenres": ["Animation", "Family", "Comedy"],
                "dislikedGenres": ["Horror"],
                "preferredPeople": [],
            },
            "alex": {
                "displayName": "Alex",
                "preferredGenres": ["Crime", "Drama", "Documentary"],
                "dislikedGenres": [],
                "preferredPeople": [],
            },
        }
        now = time.time()
        by_genre: dict[str, list[dict[str, Any]]] = {}
        for doc in documents:
            for genre in doc.get("genres", []):
                by_genre.setdefault(genre, []).append(doc)
        for viewer_id, spec in profiles.items():
            profile = {
                "type": "viewer_profile",
                "viewerId": viewer_id,
                "displayName": spec["displayName"],
                "preferredGenres": spec["preferredGenres"],
                "dislikedGenres": spec["dislikedGenres"],
                "preferredPeople": spec["preferredPeople"],
                "preferredLanguages": ["English"],
                "preferredThemes": [],
                "dislikedThemes": [],
                "dislikedPeople": [],
                "preferredContentTypes": [],
                "likedTitleIds": [],
                "dislikedTitleIds": [],
                "watchlistTitleIds": [],
                "genreAffinities": {},
                "themeAffinities": {},
                "maxRuntimeMinutes": None,
                "avoidGraphicViolence": False,
                "recommendationVersion": 1,
                "createdAt": now,
                "updatedAt": now,
                "seeded": True,
            }
            self.profiles.upsert(f"profile::{viewer_id}", profile)
            candidates: list[dict[str, Any]] = []
            for genre in spec["preferredGenres"]:
                candidates.extend(by_genre.get(genre, [])[:5])
            unique = list({item["id"]: item for item in candidates}.values())
            random.Random(viewer_id).shuffle(unique)
            for i, title in enumerate(unique[:7]):
                progress = [100, 100, 100, 62, 28, 100, 87][i]
                watched_at = now - (i + 1) * 86400 * 3
                self.history.upsert(
                    f"watch::{viewer_id}::{title['id']}",
                    {
                        "type": "watch_event",
                        "viewerId": viewer_id,
                        "titleId": title["id"],
                        "progressPct": progress,
                        "status": "completed" if progress >= 85 else "in_progress",
                        "lastWatchedAt": watched_at,
                        "device": "living-room-tv" if i % 2 == 0 else "tablet",
                        "seeded": True,
                    },
                )
                if i < 3:
                    self.interactions.upsert(
                        f"interaction::{viewer_id}::{title['id']}::like",
                        {
                            "type": "viewer_interaction",
                            "viewerId": viewer_id,
                            "titleId": title["id"],
                            "action": "like",
                            "timestamp": watched_at + 3600,
                            "seeded": True,
                        },
                    )
        print("  seeded viewer profiles and deterministic watch histories")

    def job(self, status: str, details: dict[str, Any]) -> None:
        self.jobs.upsert(
            "ingestion::latest",
            {
                "type": "catalogue_ingestion_job",
                "status": status,
                "updatedAt": time.time(),
                **details,
            },
        )


def sample_documents() -> list[dict[str, Any]]:
    rows = [
        ("movie::sample-1", "The Last Signal", "movie", ["Science Fiction", "Drama"], "A linguist decodes a signal that appears to arrive from humanity's future."),
        ("movie::sample-2", "Northern Shadow", "movie", ["Thriller", "Crime"], "A detective returns to a remote coastal town to investigate a disappearance."),
        ("tv::sample-3", "Glass Harbour", "tv", ["Crime", "Drama"], "A British harbour police unit uncovers a network of secrets."),
        ("movie::sample-4", "Sunday Orbit", "movie", ["Comedy", "Science Fiction"], "A family accidentally spends a weekend aboard a private space station."),
        ("tv::sample-5", "Wild Isles", "tv", ["Documentary"], "A journey through dramatic landscapes and wildlife."),
        ("movie::sample-6", "Paper Kingdom", "movie", ["Animation", "Family"], "Two siblings enter a world constructed from unfinished stories."),
    ]
    docs = []
    for i, (doc_id, title, kind, genres, overview) in enumerate(rows):
        docs.append({
            "id": doc_id,
            "type": "title",
            "tmdbId": None,
            "contentType": kind,
            "title": title,
            "originalTitle": title,
            "overview": overview,
            "releaseYear": 2022 + i % 4,
            "genres": genres,
            "keywords": [],
            "adult": False,
            "availability": demo_availability(doc_id, "GB"),
            "governedAgentSchemaVersion": 1,
            "castNames": [],
            "directorNames": [],
            "originCountries": ["GB"],
            "spokenLanguages": ["English"],
            "originalLanguage": "en",
            "runtimeMinutes": 92 + i * 5,
            "voteAverage": 7.1 + i * .2,
            "voteCount": 1000 + i * 50,
            "popularity": 100 - i,
            "posterPath": None,
            "backdropPath": None,
            "searchText": f"{title}. {overview}. Genres: {', '.join(genres)}",
            "embeddingText": f"Title: {title}. Type: {kind}. Genres: {', '.join(genres)}. Overview: {overview}",
            "source": "offline_sample",
            "ingestedAt": time.time(),
        })
    return docs


def create_search_index(config: Config, dimensions: int) -> None:
    fields: dict[str, Any] = {}

    def text_field(name: str, analyzer: str = "en", store: bool = True) -> None:
        fields[name] = {
            "enabled": True,
            "dynamic": False,
            "fields": [{
                "name": name,
                "type": "text",
                "analyzer": analyzer,
                "index": True,
                "store": store,
                "include_term_vectors": True,
            }],
        }

    text_field("title", "en")
    text_field("originalTitle", "en")
    text_field("overview", "en")
    text_field("searchText", "en")
    text_field("genres", "keyword")
    text_field("keywords", "keyword")
    text_field("castNames", "keyword")
    text_field("directorNames", "keyword")
    text_field("originCountries", "keyword")
    text_field("spokenLanguages", "keyword")
    text_field("originalLanguage", "keyword")
    text_field("contentType", "keyword")
    fields["releaseYear"] = {
        "enabled": True,
        "dynamic": False,
        "fields": [{"name": "releaseYear", "type": "number", "index": True, "store": True}],
    }
    fields["runtimeMinutes"] = {
        "enabled": True,
        "dynamic": False,
        "fields": [{"name": "runtimeMinutes", "type": "number", "index": True, "store": True}],
    }
    fields["voteAverage"] = {
        "enabled": True,
        "dynamic": False,
        "fields": [{"name": "voteAverage", "type": "number", "index": True, "store": True}],
    }
    fields["embedding"] = {
        "enabled": True,
        "dynamic": False,
        "fields": [{
            "name": "embedding",
            "type": "vector",
            "dims": dimensions,
            "similarity": "cosine",
            "index": True,
            "vector_index_optimized_for": "recall",
        }],
    }
    definition = {
        "type": "fulltext-index",
        "name": config.search_index,
        "sourceType": "gocbcore",
        "sourceName": config.bucket,
        "sourceParams": {},
        "planParams": {
            "maxPartitionsPerPIndex": 1024,
            "indexPartitions": 1,
            "numReplicas": 0,
        },
        "params": {
            "doc_config": {
                "docid_prefix_delim": "",
                "docid_regexp": "",
                "mode": "scope.collection.type_field",
                "type_field": "type",
            },
            "mapping": {
                "default_analyzer": "standard",
                "default_datetime_parser": "dateTimeOptional",
                "default_field": "_all",
                "default_mapping": {"enabled": False, "dynamic": False},
                "types": {
                    "catalogue.titles.title": {
                        "enabled": True,
                        "dynamic": False,
                        "properties": fields,
                    }
                },
            },
            "store": {"indexType": "scorch", "segmentVersion": 16},
        },
    }
    url = (
        f"{config.search_url}/api/bucket/{config.bucket}/scope/catalogue/"
        f"index/{config.search_index}"
    )
    with httpx.Client(auth=(config.search_username, config.search_password), timeout=60.0) as client:
        current = client.get(url)
        if current.status_code == 200:
            # Recreate the first-cut index so a changed embedding model or
            # dimension cannot leave an incompatible definition behind.
            delete_response = client.delete(url)
            delete_response.raise_for_status()
            time.sleep(1.0)
        response = client.put(url, json=definition)
        response.raise_for_status()
        print(f"  search index submitted: {config.bucket}.catalogue.{config.search_index}")


def create_plan_cache_search_index(config: Config, dimensions: int) -> None:
    fields = {
        "appVersion": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "appVersion",
                    "type": "text",
                    "analyzer": "keyword",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "normalisedQuery": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "normalisedQuery",
                    "type": "text",
                    "analyzer": "en",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "plannerVersion": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "plannerVersion",
                    "type": "text",
                    "analyzer": "keyword",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "toolSchemaVersion": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "toolSchemaVersion",
                    "type": "text",
                    "analyzer": "keyword",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "embeddingModel": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "embeddingModel",
                    "type": "text",
                    "analyzer": "keyword",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "semanticEligible": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "semanticEligible",
                    "type": "boolean",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "expiresAt": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "expiresAt",
                    "type": "number",
                    "index": True,
                    "store": True,
                }
            ],
        },
        "queryEmbedding": {
            "enabled": True,
            "dynamic": False,
            "fields": [
                {
                    "name": "queryEmbedding",
                    "type": "vector",
                    "dims": dimensions,
                    "similarity": "cosine",
                    "index": True,
                    "vector_index_optimized_for": "recall",
                }
            ],
        },
    }
    definition = {
        "type": "fulltext-index",
        "name": config.plan_cache_search_index,
        "sourceType": "gocbcore",
        "sourceName": config.bucket,
        "sourceParams": {},
        "planParams": {
            "maxPartitionsPerPIndex": 1024,
            "indexPartitions": 1,
            "numReplicas": 0,
        },
        "params": {
            "doc_config": {
                "docid_prefix_delim": "",
                "docid_regexp": "",
                "mode": "scope.collection.type_field",
                "type_field": "type",
            },
            "mapping": {
                "default_analyzer": "standard",
                "default_datetime_parser": "dateTimeOptional",
                "default_field": "_all",
                "default_mapping": {"enabled": False, "dynamic": False},
                "types": {
                    "recommendations.plan_cache.assistant_plan_cache": {
                        "enabled": True,
                        "dynamic": False,
                        "properties": fields,
                    }
                },
            },
            "store": {"indexType": "scorch", "segmentVersion": 16},
        },
    }
    url = (
        f"{config.search_url}/api/bucket/{config.bucket}/scope/recommendations/"
        f"index/{config.plan_cache_search_index}"
    )
    with httpx.Client(
        auth=(config.search_username, config.search_password), timeout=60.0
    ) as client:
        current = client.get(url)
        if current.status_code == 200:
            delete_response = client.delete(url)
            delete_response.raise_for_status()
            time.sleep(1.0)
        response = client.put(url, json=definition)
        response.raise_for_status()
        print(
            "  plan-cache search index submitted: "
            f"{config.bucket}.recommendations.{config.plan_cache_search_index}"
        )


async def build_documents(config: Config) -> list[dict[str, Any]]:
    if config.sample_only:
        print("Using the small offline fictional catalogue.")
        return sample_documents()
    if not config.tmdb_token:
        print(
            "TMDB_READ_ACCESS_TOKEN is empty; using the bundled sample catalogue. "
            "Set a token later to load a larger real catalogue."
        )
        return sample_documents()
    tmdb = TMDBClient(config)
    try:
        print(f"Discovering {config.movie_limit} films and {config.tv_limit} TV series from TMDB...")
        movies, tv = await asyncio.gather(
            tmdb.discover("movie", config.movie_limit),
            tmdb.discover("tv", config.tv_limit),
        )
        tasks: list[tuple[str, asyncio.Task[dict[str, Any]]]] = []
        for item in movies:
            tasks.append(("movie", asyncio.create_task(tmdb.detail("movie", int(item["id"])))))
        for item in tv:
            tasks.append(("tv", asyncio.create_task(tmdb.detail("tv", int(item["id"])))))
        documents: list[dict[str, Any]] = []
        for index, (kind, task) in enumerate(tasks, start=1):
            try:
                documents.append(normalize_title(await task, kind, config))
            except Exception as exc:
                print(f"  warning: skipped {kind} detail: {exc}", file=sys.stderr)
            if index % 25 == 0 or index == len(tasks):
                print(f"  fetched {index}/{len(tasks)} details")
        return documents
    finally:
        await tmdb.close()


async def main() -> None:
    config = Config()
    managed_vectors = os.getenv("DATA_PROCESSING_MODE", "python_loader") == "capella_workflow"
    if managed_vectors and config.manage_search_index:
        raise ValueError("Managed vectorization requires MANAGE_SEARCH_INDEX=false")
    loader = CouchbaseLoader(config)
    embedder = None if managed_vectors else Embedder(config)
    started = time.time()
    try:
        source = "offline_sample" if config.sample_only or not config.tmdb_token else "TMDB"
        loader.job("running", {"source": source})
        documents = await build_documents(config)
        if not documents:
            raise RuntimeError("No catalogue documents were loaded.")
        dimension = 0
        if not managed_vectors:
            print(f"Generating {len(documents)} embeddings with {config.embed_model}...")
            batch_size = 12
            for start in range(0, len(documents), batch_size):
                batch = documents[start:start + batch_size]
                vectors = await embedder.batch([item["embeddingText"] for item in batch])
                for doc, vector in zip(batch, vectors, strict=True):
                    expected_dimension = int(os.getenv("EMBEDDING_DIMENSIONS", "2048"))
                    if len(vector) != expected_dimension:
                        raise ValueError(f"Embedding model returned {len(vector)} dimensions; expected {expected_dimension}")
                    doc["embedding"] = vector
                    doc["embeddingModel"] = config.embed_model
                    dimension = len(vector)
                print(f"  embedded {min(start + batch_size, len(documents))}/{len(documents)}")
        else:
            print("Writing source documents; Capella workflow will populate embedding later.")
        loader.upsert_titles(documents)
        if config.seed_demo_viewers:
            loader.seed_viewers(documents)
        if config.manage_search_index:
            create_search_index(config, dimension)
            create_plan_cache_search_index(config, dimension)
        else:
            print("  Search index creation skipped (MANAGE_SEARCH_INDEX=false). Import/validate the scoped index through the target platform.")
        if config.manage_search_index:
            loader.jobs.upsert(
                "search_index::schema",
                {"type": "search_index_schema", "version": 3, "updatedAt": time.time()},
            )
        loader.job(
            "completed",
            {
                "source": source,
                "documentCount": len(documents),
                "embeddingModel": config.embed_model,
                "embeddingDimension": dimension,
                "vectorization": "pending_workflow" if managed_vectors else "complete",
                "elapsedSeconds": round(time.time() - started, 1),
            },
        )
        print("\nCatalogue load complete.")
        print(f"  Documents: {len(documents)}")
        print(f"  Embedding dimension: {dimension}")
        print(f"  Search index: {config.bucket}.catalogue.{config.search_index}" + (" (managed)" if config.manage_search_index else " (must already be provisioned)"))
        print(
            "  Plan cache index: "
            f"{config.bucket}.recommendations.{config.plan_cache_search_index}"
            + (" (managed)" if config.manage_search_index else " (must already be provisioned)")
        )
    except Exception as exc:
        loader.job("failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if embedder is not None:
            await embedder.close()
        loader.close()


if __name__ == "__main__":
    asyncio.run(main())
