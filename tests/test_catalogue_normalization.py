from tools.load_catalogue import Config, normalize_title, sample_documents


def test_sample_documents_have_search_and_embedding_text() -> None:
    docs = sample_documents()
    assert docs
    assert all(doc["searchText"] for doc in docs)
    assert all(doc["embeddingText"] for doc in docs)
    assert all(doc["contentType"] in {"movie", "tv"} for doc in docs)


def test_movie_normalization() -> None:
    detail = {
        "id": 42,
        "title": "Example Film",
        "original_title": "Example Film",
        "overview": "A thoughtful journey through memory.",
        "release_date": "2025-04-01",
        "genres": [{"name": "Drama"}],
        "keywords": {"keywords": [{"name": "memory"}]},
        "credits": {
            "cast": [{"name": "A. Actor"}],
            "crew": [{"name": "D. Director", "job": "Director"}],
        },
        "release_dates": {"results": []},
        "runtime": 101,
        "production_countries": [{"iso_3166_1": "GB"}],
        "spoken_languages": [{"english_name": "English"}],
        "original_language": "en",
        "vote_average": 7.8,
        "vote_count": 100,
        "popularity": 50.0,
        "poster_path": "/poster.jpg",
        "backdrop_path": "/backdrop.jpg",
        "status": "Released",
    }
    doc = normalize_title(detail, "movie", Config())
    assert doc["id"] == "movie::42"
    assert doc["releaseYear"] == 2025
    assert doc["directorNames"] == ["D. Director"]
    assert "memory" in doc["searchText"]
