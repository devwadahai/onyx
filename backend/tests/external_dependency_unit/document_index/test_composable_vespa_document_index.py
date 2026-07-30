"""External dependency tests for ComposableVespaDocumentIndex against a real,
live composable-vespa instance (github.com/devwadahai/composable-vespa).

Not parametrized alongside `test_document_index.py`'s `document_indices`
fixture: most of that file's tests exercise `update()`, which
ComposableVespaDocumentIndex deliberately raises NotImplementedError for
(onyx-adapter's `/onyx/index/update` is a real, documented 501 upstream —
see that class's doc comment) — forcing it into the same parametrization
would just be a wall of expected failures, not real coverage.
"""

import time
import uuid
from collections.abc import Generator

import httpx
import pytest

from onyx.configs.constants import PUBLIC_DOC_PAT
from onyx.context.search.enums import QueryType
from onyx.context.search.models import IndexFilters
from onyx.db.enums import EmbeddingPrecision
from onyx.document_index.composable_vespa.composable_vespa_document_index import (
    ComposableVespaDocumentIndex,
)
from onyx.document_index.interfaces_new import (
    DocumentSectionRequest,
    MetadataUpdateRequest,
    TenantState,
)
from shared_configs.configs import POSTGRES_DEFAULT_SCHEMA_STANDARD_VALUE
from tests.external_dependency_unit.document_index.conftest import (
    EMBEDDING_DIM,
    make_chunk,
    make_indexing_metadata,
)

COMPOSABLE_VESPA_TEST_URL = "http://localhost:8096"


def _wait_for_composable_vespa(timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if (
                httpx.get(f"{COMPOSABLE_VESPA_TEST_URL}/health", timeout=2).status_code
                == 200
            ):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def composable_vespa_document_index(
    tenant_context: None,  # noqa: ARG001
) -> Generator[ComposableVespaDocumentIndex, None, None]:
    if not _wait_for_composable_vespa():
        pytest.fail(
            f"composable-vespa is not available at {COMPOSABLE_VESPA_TEST_URL}."
        )
    index = ComposableVespaDocumentIndex(
        index_name="test_index",
        tenant_state=TenantState(
            tenant_id=POSTGRES_DEFAULT_SCHEMA_STANDARD_VALUE, multitenant=False
        ),
    )
    index._base_url = COMPOSABLE_VESPA_TEST_URL  # noqa: SLF001 — test-only override, no env var plumbed through for this.
    index.verify_and_create_index_if_necessary(
        embedding_dim=EMBEDDING_DIM, embedding_precision=EmbeddingPrecision.FLOAT
    )
    yield index


def _open_filters() -> IndexFilters:
    return IndexFilters(
        access_control_list=[PUBLIC_DOC_PAT],
        tenant_id=POSTGRES_DEFAULT_SCHEMA_STANDARD_VALUE,
    )


class TestComposableVespaDocumentIndex:
    def test_index_single_new_doc(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_single_{uuid.uuid4().hex[:8]}"
        chunk = make_chunk(doc_id, content="a document about rust ownership")
        metadata = make_indexing_metadata([doc_id], old_counts=[0], new_counts=[1])

        results = composable_vespa_document_index.index(
            chunks=[chunk], indexing_metadata=metadata
        )

        assert len(results) == 1
        assert results[0].document_id == doc_id
        assert results[0].already_existed is False

    def test_index_existing_doc_already_existed_true(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_existing_{uuid.uuid4().hex[:8]}"
        chunk = make_chunk(doc_id)
        metadata = make_indexing_metadata([doc_id], old_counts=[0], new_counts=[1])
        composable_vespa_document_index.index(
            chunks=[chunk], indexing_metadata=metadata
        )

        results = composable_vespa_document_index.index(
            chunks=[chunk],
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[1], new_counts=[1]
            ),
        )

        assert len(results) == 1
        assert results[0].already_existed is True

    def test_hybrid_retrieval_finds_indexed_chunk(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_hybrid_{uuid.uuid4().hex[:8]}"
        chunk = make_chunk(
            doc_id, content="composable-vespa hybrid retrieval smoke test"
        )
        composable_vespa_document_index.index(
            chunks=[chunk],
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[0], new_counts=[1]
            ),
        )

        results = composable_vespa_document_index.hybrid_retrieval(
            query="composable-vespa hybrid retrieval smoke test",
            query_embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            final_keywords=None,
            query_type=QueryType.SEMANTIC,
            filters=_open_filters(),
            num_to_retrieve=10,
        )

        assert any(r.document_id == doc_id for r in results)

    def test_keyword_retrieval_finds_indexed_chunk(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_keyword_{uuid.uuid4().hex[:8]}"
        chunk = make_chunk(
            doc_id, content="a distinctly wordable keyword needle phrase"
        )
        composable_vespa_document_index.index(
            chunks=[chunk],
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[0], new_counts=[1]
            ),
        )

        results = composable_vespa_document_index.keyword_retrieval(
            query="distinctly wordable keyword needle phrase",
            filters=_open_filters(),
            num_to_retrieve=10,
        )

        assert any(r.document_id == doc_id for r in results)

    def test_semantic_retrieval_finds_indexed_chunk(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_semantic_{uuid.uuid4().hex[:8]}"
        chunk = make_chunk(doc_id)
        composable_vespa_document_index.index(
            chunks=[chunk],
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[0], new_counts=[1]
            ),
        )

        results = composable_vespa_document_index.semantic_retrieval(
            query_embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            filters=_open_filters(),
            num_to_retrieve=10,
        )

        assert any(r.document_id == doc_id for r in results)

    def test_random_retrieval_returns_chunks(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_random_{uuid.uuid4().hex[:8]}"
        chunk = make_chunk(doc_id)
        composable_vespa_document_index.index(
            chunks=[chunk],
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[0], new_counts=[1]
            ),
        )

        results = composable_vespa_document_index.random_retrieval(
            filters=_open_filters(), num_to_retrieve=50
        )

        assert len(results) > 0

    def test_random_retrieval_dirty_raises(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        with pytest.raises(NotImplementedError):
            composable_vespa_document_index.random_retrieval(
                filters=_open_filters(), num_to_retrieve=10, dirty=True
            )

    def test_id_based_retrieval_returns_requested_chunks(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_idbased_{uuid.uuid4().hex[:8]}"
        chunks = [make_chunk(doc_id, chunk_id=0), make_chunk(doc_id, chunk_id=1)]
        composable_vespa_document_index.index(
            chunks=chunks,
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[0], new_counts=[2]
            ),
        )

        results = composable_vespa_document_index.id_based_retrieval(
            chunk_requests=[
                DocumentSectionRequest(
                    document_id=doc_id, min_chunk_ind=0, max_chunk_ind=1
                )
            ],
            filters=_open_filters(),
        )

        assert {r.chunk_id for r in results} == {0, 1}

    def test_id_based_retrieval_open_ended_range_raises(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        with pytest.raises(NotImplementedError):
            composable_vespa_document_index.id_based_retrieval(
                chunk_requests=[DocumentSectionRequest(document_id="whatever")],
                filters=_open_filters(),
            )

    def test_delete_removes_chunks(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        doc_id = f"cv_test_delete_{uuid.uuid4().hex[:8]}"
        chunks = [make_chunk(doc_id, chunk_id=0), make_chunk(doc_id, chunk_id=1)]
        composable_vespa_document_index.index(
            chunks=chunks,
            indexing_metadata=make_indexing_metadata(
                [doc_id], old_counts=[0], new_counts=[2]
            ),
        )

        deleted_count = composable_vespa_document_index.delete(doc_id, chunk_count=2)

        assert deleted_count == 2
        remaining = composable_vespa_document_index.id_based_retrieval(
            chunk_requests=[
                DocumentSectionRequest(
                    document_id=doc_id, min_chunk_ind=0, max_chunk_ind=1
                )
            ],
            filters=_open_filters(),
        )
        assert remaining == []

    def test_delete_without_chunk_count_raises(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        with pytest.raises(NotImplementedError):
            composable_vespa_document_index.delete("whatever")

    def test_update_raises_not_implemented(
        self, composable_vespa_document_index: ComposableVespaDocumentIndex
    ) -> None:
        update_request = MetadataUpdateRequest(
            document_ids=["whatever"], doc_id_to_chunk_cnt={"whatever": 1}, boost=5
        )
        with pytest.raises(NotImplementedError):
            composable_vespa_document_index.update([update_request])
