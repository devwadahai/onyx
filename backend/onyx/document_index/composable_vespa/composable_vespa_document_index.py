"""Thin HTTP shim over composable-vespa's onyx-adapter
(github.com/devwadahai/composable-vespa), following the same pattern already
proven by OpenSearchDocumentIndex's swap-in: this class does no translation
of its own beyond building the wire JSON bodies documented in that repo's
docs/05-api.md — onyx-adapter is where field-mapping/scoring fidelity
actually lives, this is routing/glue.

Known gaps, not silently papered over:
  - No secondary/reindex-port support yet (see ComposableVespaIndexPair) —
    composable-vespa is a single flat collection today, with no concept of a
    parallel secondary index to port into.
  - `update()` always raises: onyx-adapter's `/onyx/index/update` returns 501
    (needs a list-chunks-for-document primitive it doesn't have yet).
  - `delete()` requires `chunk_count` — every real caller in this codebase
    already supplies it (celery cleanup tasks, ingestion.py), so this isn't
    a practical limitation, but there's no way to discover it server-side.
  - `id_based_retrieval` only supports fully-bounded chunk ranges — an
    open-ended range (`max_chunk_ind=None`, "to the end of the document")
    can't be resolved without knowing the document's total chunk count,
    same root cause as the `update()` gap.
  - `keyword_retrieval` currently scores with onyx-adapter's
    `vespa_parity_keyword` profile (the hybrid formula's low-alpha variant),
    not a replica of Vespa's own `admin_search` profile
    (`bm25(content) + 5*bm25(title)`, no vector component at all) that real
    Vespa's `keyword_retrieval` actually uses — a real, separate formula
    fidelity gap from `hybrid_retrieval(query_type=KEYWORD)`, not yet fixed.
  - `semantic_retrieval` is implemented for real here even though real
    Vespa's own implementation just raises NotImplementedError — this is a
    genuine capability composable-vespa has that Vespa's current interface
    doesn't expose, not a fidelity gap.
"""

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import httpx

from onyx.configs.app_configs import COMPOSABLE_VESPA_URL, MAX_CHUNKS_PER_DOC_BATCH
from onyx.configs.constants import INDEX_SEPARATOR, DocumentSource
from onyx.connectors.cross_connector_utils.miscellaneous_utils import (
    get_experts_stores_representations,
)
from onyx.context.search.enums import QueryType
from onyx.context.search.models import (
    IndexFilters,
    InferenceChunk,
    InferenceChunkUncleaned,
)
from onyx.db.enums import EmbeddingPrecision
from onyx.document_index.chunk_content_enrichment import (
    cleanup_content_for_chunks,
    generate_enriched_content_for_chunk_text,
)
from onyx.document_index.interfaces_new import (
    DocumentIndex,
    DocumentInsertionRecord,
    DocumentSectionRequest,
    IndexingMetadata,
    MetadataUpdateRequest,
    TenantState,
)
from onyx.document_index.vespa.indexing_utils import (
    BaseHTTPXClientContext,
    GlobalHTTPXClientContext,
    TemporaryHTTPXClientContext,
)
from onyx.indexing.models import DocMetadataAwareIndexChunk
from onyx.utils.logger import setup_logger
from onyx.utils.text_processing import remove_invalid_unicode_chars
from shared_configs.model_server_models import Embedding

logger = setup_logger()


def _updated_at_epoch_seconds(t: datetime | None) -> int | None:
    if not t:
        return None
    if t.tzinfo != timezone.utc:
        raise ValueError("Connectors must provide document update time in UTC")
    return int(t.timestamp())


def _chunk_to_json(chunk: DocMetadataAwareIndexChunk) -> dict[str, Any]:
    """Builds the `IndexChunkDto` JSON body onyx-adapter's `/onyx/index/chunks`
    expects — field-for-field the same source expressions
    `_index_vespa_chunk` (vespa/indexing_utils.py) uses, since the wire field
    names match the real schema exactly (per onyx-adapter's own dto.rs)."""
    document = chunk.source_document
    title = document.get_title_for_document_index()

    # onyx-adapter's `metadata` field is a flat `HashMap<String, String>`,
    # unlike the real schema's arbitrary JSON — list values are joined rather
    # than dropped, a lossy but honest simplification.
    metadata: dict[str, str] = {
        key: value if isinstance(value, str) else "; ".join(value)
        for key, value in document.metadata.items()
    }

    return {
        "document_id": document.id,
        "chunk_id": chunk.chunk_id,
        "semantic_identifier": remove_invalid_unicode_chars(
            document.semantic_identifier
        ),
        "title": remove_invalid_unicode_chars(title) if title else None,
        "content": remove_invalid_unicode_chars(
            generate_enriched_content_for_chunk_text(chunk)
        ),
        "title_embedding": chunk.title_embedding,
        "embeddings": chunk.embeddings.full_embedding,
        "blurb": remove_invalid_unicode_chars(chunk.blurb),
        "source_type": str(document.source.value),
        "source_links": {str(k): v for k, v in (chunk.source_links or {}).items()},
        "section_continuation": chunk.section_continuation,
        "boost": chunk.boost,
        "hidden": False,  # Never set at index time in the real system either.
        "aggregated_chunk_boost_factor": chunk.aggregated_chunk_boost_factor,
        "metadata": metadata,
        "metadata_list": document.get_metadata_str_attributes() or [],
        "metadata_suffix": (
            remove_invalid_unicode_chars(chunk.metadata_suffix_keyword)
            if chunk.metadata_suffix_keyword
            else None
        ),
        "chunk_context": chunk.chunk_context,
        "doc_summary": chunk.doc_summary,
        "doc_updated_at": _updated_at_epoch_seconds(document.doc_updated_at),
        "primary_owners": get_experts_stores_representations(document.primary_owners)
        or [],
        "secondary_owners": get_experts_stores_representations(
            document.secondary_owners
        )
        or [],
        "access_control_list": sorted(chunk.access.to_acl()),
        "document_sets": sorted(chunk.document_sets),
        "tenant_id": chunk.tenant_id,
        "large_chunk_reference_ids": chunk.large_chunk_reference_ids,
        "image_file_name": chunk.image_file_id,
    }


def _filters_to_json(
    filters: IndexFilters, *, include_hidden: bool = False
) -> dict[str, Any]:
    """`IndexFilters` -> onyx-adapter's `OnyxFilters` JSON shape. Not
    replicated: time-range filters (`created_at_range`/`updated_at_range`)
    and user_project/persona scoping — no equivalent on `OnyxFilters` today,
    dropped rather than silently misapplied. `IndexFilters` has no
    `metadata_list` field of its own; `tags` is the source for it, folded in
    using the real `INDEX_SEPARATOR` convention."""
    metadata_list = (
        [f"{tag.tag_key}{INDEX_SEPARATOR}{tag.tag_value}" for tag in filters.tags]
        if filters.tags
        else []
    )
    return {
        "access_control_list": filters.access_control_list or [],
        "bypass_acl": not filters.access_control_list,
        "tenant_id": filters.tenant_id,
        "source_type": [s.value for s in filters.source_type]
        if filters.source_type
        else [],
        "document_sets": list(filters.document_set) if filters.document_set else [],
        "metadata_list": metadata_list,
        "include_hidden": include_hidden,
    }


def _json_to_inference_chunk_uncleaned(
    fields: dict[str, Any],
) -> InferenceChunkUncleaned:
    """Inverse of onyx-adapter's `InferenceChunkDto` — its field names match
    `InferenceChunkUncleaned`'s directly (see that struct's doc comment), so
    this is close to a straight `**fields` unpack; the exceptions are typed
    conversions (`updated_at`) and fields InferenceChunkUncleaned doesn't
    carry (`granularity`, only meaningful internally)."""
    updated_at = (
        datetime.fromtimestamp(fields["doc_updated_at"], tz=timezone.utc)
        if fields.get("doc_updated_at") is not None
        else None
    )
    return InferenceChunkUncleaned(
        document_id=fields["document_id"],
        chunk_id=fields["chunk_id"],
        source_type=DocumentSource(
            fields.get("source_type") or DocumentSource.FILE.value
        ),
        semantic_identifier=fields.get("semantic_identifier") or "",
        title=fields.get("title"),
        boost=int(fields.get("boost", 1)),
        score=fields.get("score"),
        hidden=fields.get("hidden", False),
        metadata=fields.get("metadata") or {},
        match_highlights=fields.get("match_highlights") or [],
        doc_summary=fields.get("doc_summary") or "",
        chunk_context=fields.get("chunk_context") or "",
        updated_at=updated_at,
        primary_owners=fields.get("primary_owners"),
        secondary_owners=fields.get("secondary_owners"),
        large_chunk_reference_ids=fields.get("large_chunk_reference_ids") or [],
        blurb=fields.get("blurb") or "",
        content=fields["content"],
        source_links={int(k): v for k, v in (fields.get("source_links") or {}).items()}
        or None,
        image_file_id=fields.get("image_file_id"),
        section_continuation=fields.get("section_continuation", False),
        metadata_suffix=fields.get("metadata_suffix"),
    )


class ComposableVespaDocumentIndex(DocumentIndex):
    """composable-vespa-backed implementation of the DocumentIndex interface.

    `index_name` matches the constructor shape the other implementations
    take, but composable-vespa itself has no per-index concept — one
    deployment is one flat collection — so it's kept only for logging/
    identification, never sent over the wire.
    """

    def __init__(
        self,
        index_name: str,
        tenant_state: TenantState,
        httpx_client: httpx.Client | None = None,
    ) -> None:
        self._index_name = index_name
        self._tenant_state = tenant_state
        self._base_url = COMPOSABLE_VESPA_URL
        self._httpx_client_context: BaseHTTPXClientContext = (
            GlobalHTTPXClientContext(httpx_client)
            if httpx_client
            else TemporaryHTTPXClientContext(lambda: httpx.Client(timeout=30))
        )

    def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        with self._httpx_client_context as client:
            response = client.post(f"{self._base_url}{path}", json=json_body)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RuntimeError(
                    f"composable-vespa request to {path} failed "
                    f"(status={e.response.status_code}): {e.response.text[:500]}"
                ) from e
            # /onyx/index/verify responds 200 with no body (schema bootstrap
            # is a startup-time concern for composable-vespa, not per-request
            # — see handlers.rs::verify) — every other endpoint returns JSON.
            return response.json() if response.content else {}

    def _post_retrieval(
        self, path: str, json_body: dict[str, Any]
    ) -> list[InferenceChunkUncleaned]:
        data = self._post(path, json_body)
        return [_json_to_inference_chunk_uncleaned(r) for r in data["results"]]

    def verify_and_create_index_if_necessary(
        self,
        embedding_dim: int,
        embedding_precision: EmbeddingPrecision,
    ) -> None:
        self._post(
            "/onyx/index/verify",
            {
                "embedding_dim": embedding_dim,
                "embedding_precision": embedding_precision.value,
            },
        )

    def index(
        self,
        chunks: Iterable[DocMetadataAwareIndexChunk],
        indexing_metadata: IndexingMetadata,
    ) -> list[DocumentInsertionRecord]:
        # composable-vespa's Qdrant/Tantivy upsert is idempotent per
        # (document_id, chunk_id) — it doesn't need old/new chunk counts to
        # clean up a shrunk document's stale tail chunks the way Vespa/
        # OpenSearch's index() does. The one thing it IS needed for:
        # already_existed, which onyx-adapter derives the same way real
        # Vespa/OpenSearch do — old_chunk_cnt > 0 — not a live lookup.
        indexing_metadata_json = {
            "doc_id_to_chunk_cnt_diff": {
                doc_id: {
                    "old_chunk_cnt": diff.old_chunk_cnt,
                    "new_chunk_cnt": diff.new_chunk_cnt,
                }
                for doc_id, diff in indexing_metadata.doc_id_to_chunk_cnt_diff.items()
            }
        }

        chunk_batch: list[dict[str, Any]] = []
        inserted_by_doc: dict[str, DocumentInsertionRecord] = {}

        def _flush() -> None:
            if not chunk_batch:
                return
            data = self._post(
                "/onyx/index/chunks",
                {
                    "chunks": list(chunk_batch),
                    "indexing_metadata": indexing_metadata_json,
                },
            )
            for entry in data["inserted"]:
                inserted_by_doc[entry["document_id"]] = DocumentInsertionRecord(
                    document_id=entry["document_id"],
                    already_existed=entry["already_existed"],
                )
            chunk_batch.clear()

        for chunk in chunks:
            chunk_batch.append(_chunk_to_json(chunk))
            if len(chunk_batch) >= MAX_CHUNKS_PER_DOC_BATCH:
                _flush()
        _flush()

        return list(inserted_by_doc.values())

    def delete(self, document_id: str, chunk_count: int | None = None) -> int:
        if chunk_count is None:
            raise NotImplementedError(
                "composable-vespa's delete needs an explicit chunk_count — it has no "
                "list-chunks-for-document primitive to discover it server-side. Every "
                "real caller in this codebase already supplies it."
            )
        with self._httpx_client_context as client:
            response = client.delete(
                f"{self._base_url}/onyx/index/documents/{document_id}",
                params={"chunk_count": chunk_count},
            )
            response.raise_for_status()
            return int(response.json()["deleted_chunk_count"])

    def update(self, update_requests: list[MetadataUpdateRequest]) -> None:
        raise NotImplementedError(
            "onyx-adapter's /onyx/index/update returns 501 — partial chunk metadata "
            "update (boost/ACL/document_sets/hidden without a full reindex) isn't "
            "implemented yet, see composable-vespa's handlers.rs::update_chunks."
        )

    def id_based_retrieval(
        self,
        chunk_requests: list[DocumentSectionRequest],
        filters: IndexFilters,
        batch_retrieval: bool = False,  # noqa: ARG002 — composable-vespa has no separate batch vs. per-request code path; every id-based lookup is already a single batched HTTP call.
    ) -> list[InferenceChunk]:
        resolved: list[dict[str, Any]] = []
        for req in chunk_requests:
            if req.min_chunk_ind is None or req.max_chunk_ind is None:
                raise NotImplementedError(
                    f"id_based_retrieval for document {req.document_id!r} needs both "
                    "min_chunk_ind and max_chunk_ind — composable-vespa can't resolve "
                    "an open-ended range without knowing the document's total chunk "
                    "count (same root cause as the update() gap)."
                )
            for chunk_id in range(req.min_chunk_ind, req.max_chunk_ind + 1):
                resolved.append({"document_id": req.document_id, "chunk_id": chunk_id})

        data = self._post(
            "/onyx/retrieval/id",
            {"chunk_requests": resolved, "filters": _filters_to_json(filters)},
        )
        return cleanup_content_for_chunks(
            [_json_to_inference_chunk_uncleaned(r) for r in data["results"]]
        )

    def hybrid_retrieval(
        self,
        query: str,
        query_embedding: Embedding,
        final_keywords: list[str] | None,
        query_type: QueryType,
        filters: IndexFilters,
        num_to_retrieve: int,
    ) -> list[InferenceChunk]:
        # Resolved client-side, same as real Vespa's own hybrid_retrieval —
        # onyx-adapter's wire DTO never sees final_keywords separately.
        final_query = " ".join(final_keywords) if final_keywords else query
        body = {
            "query": final_query,
            "query_embedding": list(query_embedding),
            "query_type": query_type.value,
            "filters": _filters_to_json(filters),
            "num_to_retrieve": num_to_retrieve,
        }
        return cleanup_content_for_chunks(
            self._post_retrieval("/onyx/retrieval/hybrid", body)
        )

    def keyword_retrieval(
        self,
        query: str,
        filters: IndexFilters,
        num_to_retrieve: int,
        include_hidden: bool = False,
    ) -> list[InferenceChunk]:
        body = {
            "query": query,
            "filters": _filters_to_json(filters, include_hidden=include_hidden),
            "num_to_retrieve": num_to_retrieve,
            "include_hidden": include_hidden,
        }
        return cleanup_content_for_chunks(
            self._post_retrieval("/onyx/retrieval/keyword", body)
        )

    def semantic_retrieval(
        self,
        query_embedding: Embedding,
        filters: IndexFilters,
        num_to_retrieve: int,
    ) -> list[InferenceChunk]:
        body = {
            "query_embedding": list(query_embedding),
            "filters": _filters_to_json(filters),
            "num_to_retrieve": num_to_retrieve,
        }
        return cleanup_content_for_chunks(
            self._post_retrieval("/onyx/retrieval/semantic", body)
        )

    def random_retrieval(
        self,
        filters: IndexFilters,
        num_to_retrieve: int = 10,
        dirty: bool | None = None,
    ) -> list[InferenceChunk]:
        if dirty is not None:
            raise NotImplementedError(
                "composable-vespa has no secondary/reindex-port concept yet, so "
                "'dirty' has nothing to filter against."
            )
        body = {
            "filters": _filters_to_json(filters),
            "num_to_retrieve": num_to_retrieve,
        }
        return cleanup_content_for_chunks(
            self._post_retrieval("/onyx/retrieval/random", body)
        )

    @property
    def index_name(self) -> str:
        return self._index_name


class ComposableVespaIndexPair(DocumentIndex):
    """Primary+secondary pairing to match `VespaIndexPair`/`OpenSearchIndexPair`'s
    shape for `factory.py`. Only a primary is actually supported today —
    composable-vespa is one flat collection with no secondary-index/reindex-
    port concept, so a `secondary` here would silently write into and read
    from the exact same collection as `primary`, which is worse than not
    supporting it. Constructing this with a non-`None` secondary raises.
    """

    def __init__(
        self,
        primary: ComposableVespaDocumentIndex,
        secondary: ComposableVespaDocumentIndex | None,
    ) -> None:
        if secondary is not None:
            raise NotImplementedError(
                "composable-vespa doesn't support a secondary/reindex-port index yet "
                "— a search_settings swap needs a second, genuinely separate backing "
                "collection, which composable-vespa's single-collection model doesn't "
                "have today."
            )
        self._primary = primary

    def verify_and_create_index_if_necessary(
        self, embedding_dim: int, embedding_precision: EmbeddingPrecision
    ) -> None:
        self._primary.verify_and_create_index_if_necessary(
            embedding_dim, embedding_precision
        )

    def index(
        self,
        chunks: Iterable[DocMetadataAwareIndexChunk],
        indexing_metadata: IndexingMetadata,
    ) -> list[DocumentInsertionRecord]:
        return self._primary.index(chunks, indexing_metadata)

    def delete(self, document_id: str, chunk_count: int | None = None) -> int:
        return self._primary.delete(document_id, chunk_count)

    def update(self, update_requests: list[MetadataUpdateRequest]) -> None:
        self._primary.update(update_requests)

    def id_based_retrieval(
        self,
        chunk_requests: list[DocumentSectionRequest],
        filters: IndexFilters,
        batch_retrieval: bool = False,
    ) -> list[InferenceChunk]:
        return self._primary.id_based_retrieval(
            chunk_requests, filters, batch_retrieval
        )

    def hybrid_retrieval(
        self,
        query: str,
        query_embedding: Embedding,
        final_keywords: list[str] | None,
        query_type: QueryType,
        filters: IndexFilters,
        num_to_retrieve: int,
    ) -> list[InferenceChunk]:
        return self._primary.hybrid_retrieval(
            query, query_embedding, final_keywords, query_type, filters, num_to_retrieve
        )

    def keyword_retrieval(
        self,
        query: str,
        filters: IndexFilters,
        num_to_retrieve: int,
        include_hidden: bool = False,
    ) -> list[InferenceChunk]:
        return self._primary.keyword_retrieval(
            query, filters, num_to_retrieve, include_hidden
        )

    def semantic_retrieval(
        self, query_embedding: Embedding, filters: IndexFilters, num_to_retrieve: int
    ) -> list[InferenceChunk]:
        return self._primary.semantic_retrieval(
            query_embedding, filters, num_to_retrieve
        )

    def random_retrieval(
        self,
        filters: IndexFilters,
        num_to_retrieve: int = 10,
        dirty: bool | None = None,
    ) -> list[InferenceChunk]:
        return self._primary.random_retrieval(filters, num_to_retrieve, dirty)

    @property
    def primary(self) -> ComposableVespaDocumentIndex:
        return self._primary

    @property
    def secondary(self) -> ComposableVespaDocumentIndex | None:
        return None
