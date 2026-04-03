from app.database import normalize_database_url


def test_normalize_database_url_uses_psycopg_for_plain_postgres_urls() -> None:
    assert normalize_database_url("postgresql://user:pass@host:5432/db") == "postgresql+psycopg://user:pass@host:5432/db"
    assert normalize_database_url("postgres://user:pass@host:5432/db") == "postgresql+psycopg://user:pass@host:5432/db"


def test_normalize_database_url_preserves_explicit_driver_and_sqlite() -> None:
    assert normalize_database_url("postgresql+psycopg://user:pass@host:5432/db") == "postgresql+psycopg://user:pass@host:5432/db"
    assert normalize_database_url("sqlite:///./kalshi_sports_copilot.db") == "sqlite:///./kalshi_sports_copilot.db"
