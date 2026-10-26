"""Wiring: settings -> stores -> pipeline. One place builds the app,
so the API, the CLI, the demo and the evals all serve the same stack.

Round 15 adds the model gateway to this one place: the gateway is
built from the shipped profile/price files, ledgers into the credit
file store (which implements the ledger port), audits budget
refusals onto the same store, and validates the deployment's own
profile (`CREDIT_MODEL_PROFILE`) at construction — an unknown or
unconfigured profile stops the stack here, before it serves
anything. When `CREDIT_EXTRACTION_PROFILE` names a live profile,
the model-assisted extractor stands behind the extraction port;
unset, the offline reference extractor serves, exactly as before.
"""

from __future__ import annotations

from .config import (
    MIGRATIONS_DIR,
    MODEL_PRICES_PATH,
    MODEL_PROFILES_PATH,
    Settings,
    load_settings,
)
from .db import run_migrations
from .documents import LocalDocumentStore, S3DocumentStore
from .gateway import ModelAssistedExtractor, ModelGateway, load_gateway_files
from .pipeline import CreditPipeline
from .ports import CreditFileStore, DocumentStore, Extractor
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


def build_gateway(
    settings: Settings, store: CreditFileStore
) -> ModelGateway:
    """The deployment's model gateway, fail-closed at wiring time:
    the shipped profile/price files load or the stack does not
    start, and the deployment's own profile must resolve and be
    configured in this environment."""
    config = load_gateway_files(MODEL_PROFILES_PATH, MODEL_PRICES_PATH)
    return ModelGateway(
        config,
        store,
        audit_store=store,
        active_profile=settings.model_profile,
    )


def build_extractor(
    settings: Settings, gateway: ModelGateway
) -> Extractor | None:
    """The model-assisted extractor when the deployment names an
    extraction profile; None (the pipeline's offline reference
    extractor) otherwise. Construction itself refuses an offline
    or unconfigured profile — extraction assist requires a live
    model that can read documents."""
    if not settings.extraction_profile:
        return None
    return ModelAssistedExtractor(gateway, settings.extraction_profile)


def build_pipeline(settings: Settings | None = None) -> CreditPipeline:
    settings = settings or load_settings()
    store = build_store(settings)
    gateway = build_gateway(settings, store)
    return CreditPipeline(
        store=store,
        documents=build_document_store(settings),
        extractor=build_extractor(settings, gateway),
        gateway=gateway,
    )
