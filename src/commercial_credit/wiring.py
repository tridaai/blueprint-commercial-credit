"""Wiring: settings -> stores -> pipeline. One place builds the app,
so the API, the CLI, the demo and the evals all serve the same stack.
"""

from __future__ import annotations

from .config import MIGRATIONS_DIR, Settings, load_settings
from .db import run_migrations
from .documents import LocalDocumentStore, S3DocumentStore
from .pipeline import CreditPipeline
from .ports import CreditFileStore, DocumentStore
from .store import PostgresCreditStore, SqliteCreditStore


def build_store(settings: Settings) -> CreditFileStore:
    if settings.database_url:
        run_migrations(settings.database_url, MIGRATIONS_DIR)
        return PostgresCreditStore(settings.database_url)
    return SqliteCreditStore(settings.sqlite_path)


def build_document_store(settings: Settings) -> DocumentStore:
    if settings.s3_bucket:
        return S3DocumentStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
        )
    return LocalDocumentStore()


def build_pipeline(settings: Settings | None = None) -> CreditPipeline:
    settings = settings or load_settings()
    return CreditPipeline(
        store=build_store(settings),
        documents=build_document_store(settings),
    )
