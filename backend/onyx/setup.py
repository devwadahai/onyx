import time

from sqlalchemy.orm import Session

from onyx.auth.schemas import UserRole
from onyx.configs.app_configs import (
    DISABLE_INDEX_UPDATE_ON_SWAP,
    DISABLE_VECTOR_DB,
    ENABLE_OPENSEARCH_INDEXING_FOR_ONYX,
    INTEGRATION_TESTS_MODE,
    MANAGED_VESPA,
    ONYX_DISABLE_VESPA,
    UNIFI_NETWORK_MCP_URL,
    VESPA_NUM_ATTEMPTS_ON_STARTUP,
)
from onyx.configs.constants import KV_REINDEX_KEY
from onyx.configs.embedding_configs import (
    SUPPORTED_EMBEDDING_MODELS,
    SupportedEmbeddingModel,
)
from onyx.configs.model_configs import (
    GEN_AI_API_KEY,
    GEN_AI_MODEL_VERSION,
    OPENROUTER_API_KEY,
    OPENROUTER_DEFAULT_MODEL,
)
from onyx.context.search.models import SavedSearchSettings
from onyx.db.connector import check_connectors_exist, create_initial_default_connector
from onyx.db.connector_credential_pair import (
    associate_default_cc_pair,
    get_connector_credential_pairs,
    resync_cc_pair,
)
from onyx.db.credentials import create_initial_public_credential
from onyx.db.document import check_docs_exist
from onyx.db.enums import EmbeddingPrecision, MCPTransport
from onyx.db.index_attempt import (
    cancel_indexing_attempts_past_model,
    expire_index_attempts,
)
from onyx.db.llm import (
    fetch_default_llm_model,
    fetch_existing_llm_provider,
    update_default_provider,
    upsert_llm_provider,
)
from onyx.db.mcp import create_mcp_server__no_commit, get_all_mcp_servers
from onyx.db.models import Persona
from onyx.db.search_settings import (
    get_active_search_settings,
    get_current_search_settings,
    update_current_search_settings,
)
from onyx.db.swap_index import check_and_perform_index_swap
from onyx.db.tools import create_tool__no_commit, get_tools_by_mcp_server_id
from onyx.db.users import get_all_users
from onyx.document_index.factory import get_all_document_indices
from onyx.document_index.interfaces_new import DocumentIndex
from onyx.document_index.opensearch.client import (
    OpenSearchClient,
    wait_for_opensearch_with_timeout,
)
from onyx.document_index.opensearch.opensearch_document_index import set_cluster_state
from onyx.document_index.vespa.vespa_document_index import (
    register_multitenant_vespa_indices,
)
from onyx.indexing.models import IndexingSetting
from onyx.key_value_store.factory import get_kv_store
from onyx.key_value_store.interface import KvKeyNotFoundError
from onyx.llm.constants import LlmProviderNames
from onyx.llm.well_known_providers.llm_provider_options import get_openai_model_names
from onyx.natural_language_processing.search_nlp_models import (
    EmbeddingModel,
    warm_up_bi_encoder,
)
from onyx.server.features.mcp.client import discover_mcp_tools
from onyx.server.features.unifi_events.api import SECURITY_AGENT_PERSONA_NAME
from onyx.server.manage.llm.models import (
    LLMProviderUpsertRequest,
    ModelConfigurationUpsertRequest,
)
from onyx.server.settings.store import load_settings, store_settings
from onyx.utils.gpu_utils import gpu_status_request
from onyx.utils.logger import setup_logger
from shared_configs.configs import (
    ALT_INDEX_SUFFIX,
    MODEL_SERVER_HOST,
    MODEL_SERVER_PORT,
    MULTI_TENANT,
)

logger = setup_logger()


def setup_onyx(
    db_session: Session,
    tenant_id: str,  # noqa: ARG001
    cohere_enabled: bool = False,  # noqa: ARG001
) -> None:
    """
    Setup Onyx for a particular tenant. In the Single Tenant case, it will set it up for the default schema
    on server startup. In the MT case, it will be called when the tenant is created.

    The Tenant Service calls the tenants/create endpoint which runs this.
    """
    check_and_perform_index_swap(db_session=db_session)

    active_search_settings = get_active_search_settings(db_session)
    search_settings = active_search_settings.primary
    secondary_search_settings = active_search_settings.secondary

    # search_settings = get_current_search_settings(db_session)
    # multipass_config_1 = get_multipass_config(search_settings)

    # secondary_large_chunks_enabled: bool | None = None
    # secondary_search_settings = get_secondary_search_settings(db_session)
    # if secondary_search_settings:
    #     multipass_config_2 = get_multipass_config(secondary_search_settings)
    #     secondary_large_chunks_enabled = multipass_config_2.enable_large_chunks

    # Break bad state for thrashing indexes
    if secondary_search_settings and DISABLE_INDEX_UPDATE_ON_SWAP:
        expire_index_attempts(
            search_settings_id=search_settings.id, db_session=db_session
        )

        for cc_pair in get_connector_credential_pairs(db_session):
            resync_cc_pair(
                cc_pair=cc_pair,
                search_settings_id=search_settings.id,
                db_session=db_session,
            )

    # Expire all old embedding models indexing attempts, technically redundant
    cancel_indexing_attempts_past_model(db_session)

    logger.notice('Using Embedding model: "%s"', search_settings.model_name)
    if search_settings.query_prefix or search_settings.passage_prefix:
        logger.notice('Query embedding prefix: "%s"', search_settings.query_prefix)
        logger.notice('Passage embedding prefix: "%s"', search_settings.passage_prefix)

    # setup Postgres with default credential, llm providers, etc.
    setup_postgres(db_session)

    # No-op unless UNIFI_NETWORK_MCP_URL is set (see its docstring)
    setup_unifi_security_agent_mcp(db_session)

    # Does the user need to trigger a reindexing to bring the document index
    # into a good state, marked in the kv store
    if not MULTI_TENANT:
        mark_reindex_flag(db_session)

    if DISABLE_VECTOR_DB:
        logger.notice(
            "DISABLE_VECTOR_DB is set — skipping document index setup and embedding model warm-up."
        )
    else:
        # Ensure the document indices are setup correctly. This step is
        # relatively near the end because Vespa takes a bit of time to start up.
        logger.notice("Verifying Document Index(s) is/are available.")
        # This flow is for setting up the document index so we get all indices
        # here.
        document_indices = get_all_document_indices(
            search_settings,
            secondary_search_settings,
            None,
        )

        success = setup_document_indices(
            document_indices,
            IndexingSetting.from_db_model(search_settings),
        )
        if not success:
            raise RuntimeError(
                "Could not connect to a document index within the specified timeout."
            )

        logger.notice(
            "Model Server: http://%s:%s", MODEL_SERVER_HOST, MODEL_SERVER_PORT
        )
        if search_settings.provider_type is None:
            # In integration tests, do not block API startup on warm-up
            warm_up_bi_encoder(
                embedding_model=EmbeddingModel.from_db_model(
                    search_settings=search_settings,
                    server_host=MODEL_SERVER_HOST,
                    server_port=MODEL_SERVER_PORT,
                ),
                non_blocking=INTEGRATION_TESTS_MODE,
            )

        # update multipass indexing setting based on GPU availability
        update_default_multipass_indexing(db_session)


def mark_reindex_flag(db_session: Session) -> None:
    kv_store = get_kv_store()
    try:
        value = kv_store.load(KV_REINDEX_KEY)
        logger.debug("Re-indexing flag has value %s", value)
        return
    except KvKeyNotFoundError:
        # Only need to update the flag if it hasn't been set
        pass

    # If their first deployment is after the changes, it will
    # enable this when the other changes go in, need to avoid
    # this being set to False, then the user indexes things on the old version
    docs_exist = check_docs_exist(db_session)
    connectors_exist = check_connectors_exist(db_session)
    if docs_exist or connectors_exist:
        kv_store.store(KV_REINDEX_KEY, True)
    else:
        kv_store.store(KV_REINDEX_KEY, False)


def setup_document_indices(
    document_indices: list[DocumentIndex],
    index_setting: IndexingSetting,
    num_attempts: int = VESPA_NUM_ATTEMPTS_ON_STARTUP,
) -> bool:
    """Sets up all input document indices.

    If any document index setup fails, the function will return False. Otherwise
    returns True.
    """
    for document_index in document_indices:
        # Document index startup is a bit slow, so give it a few seconds.
        WAIT_SECONDS = 5
        document_index_setup_success = False
        for x in range(num_attempts):
            try:
                logger.notice(
                    "Setting up document index %s (attempt %s/%s)...",
                    document_index.__class__.__name__,
                    x + 1,
                    num_attempts,
                )
                document_index.verify_and_create_index_if_necessary(
                    embedding_dim=index_setting.final_embedding_dim,
                    embedding_precision=index_setting.embedding_precision,
                )

                logger.notice(
                    "Document index %s setup complete.",
                    document_index.__class__.__name__,
                )
                document_index_setup_success = True
                break
            except Exception:
                logger.exception(
                    "Document index %s setup did not succeed. The relevant service may not be ready yet. Retrying in %s seconds.",
                    document_index.__class__.__name__,
                    WAIT_SECONDS,
                )
                time.sleep(WAIT_SECONDS)

        if not document_index_setup_success:
            logger.error(
                "Document index %s setup did not succeed. Attempt limit reached. (%s)",
                document_index.__class__.__name__,
                num_attempts,
            )
            return False

    return True


def setup_postgres(db_session: Session) -> None:
    logger.notice("Verifying default connector/credential exist.")
    create_initial_public_credential(db_session)
    create_initial_default_connector(db_session)
    associate_default_cc_pair(db_session)

    if GEN_AI_API_KEY and fetch_default_llm_model(db_session) is None:
        # Only for dev flows
        logger.notice("Setting up default OpenAI LLM for dev.")

        llm_model = GEN_AI_MODEL_VERSION or "gpt-4o-mini"
        provider_name = "DevEnvPresetOpenAI"
        existing = fetch_existing_llm_provider(
            name=provider_name, db_session=db_session
        )
        model_req = LLMProviderUpsertRequest(
            id=existing.id if existing else None,
            name=provider_name,
            provider=LlmProviderNames.OPENAI,
            api_key=GEN_AI_API_KEY,
            api_base=None,
            api_version=None,
            custom_config=None,
            is_public=True,
            groups=[],
            model_configurations=[
                ModelConfigurationUpsertRequest(name=name, is_visible=True)
                for name in get_openai_model_names()
            ],
            api_key_changed=True,
        )
        try:
            new_llm_provider = upsert_llm_provider(
                llm_provider_upsert_request=model_req, db_session=db_session
            )
        except ValueError as e:
            logger.warning("Failed to upsert LLM provider during setup: %s", e)
            return
        update_default_provider(
            provider_id=new_llm_provider.id, model_name=llm_model, db_session=db_session
        )

    if OPENROUTER_API_KEY and fetch_default_llm_model(db_session) is None:
        # Same dev-flow shortcut as the GEN_AI_API_KEY/OpenAI block above, for
        # deployments that provision via OpenRouter instead. See
        # spec/mac-mini-production-deployment.md / cloud-vm-onyx-deployment.md
        # in unifi-mcp-secure -- this is what makes a fresh Onyx instance
        # bootable with a working default model from one env var, rather
        # than requiring a manual admin-UI LLM setup step every time.
        logger.notice("Setting up default OpenRouter LLM provider.")

        provider_name = "OpenRouter"
        existing = fetch_existing_llm_provider(
            name=provider_name, db_session=db_session
        )
        model_req = LLMProviderUpsertRequest(
            id=existing.id if existing else None,
            name=provider_name,
            provider=LlmProviderNames.OPENROUTER,
            api_key=OPENROUTER_API_KEY,
            api_base=None,
            api_version=None,
            custom_config=None,
            is_public=True,
            groups=[],
            model_configurations=[
                ModelConfigurationUpsertRequest(
                    name=OPENROUTER_DEFAULT_MODEL, is_visible=True
                )
            ],
            api_key_changed=True,
        )
        try:
            new_llm_provider = upsert_llm_provider(
                llm_provider_upsert_request=model_req, db_session=db_session
            )
        except ValueError as e:
            logger.warning(
                "Failed to upsert OpenRouter LLM provider during setup: %s", e
            )
            return
        update_default_provider(
            provider_id=new_llm_provider.id,
            model_name=OPENROUTER_DEFAULT_MODEL,
            db_session=db_session,
        )


def setup_unifi_security_agent_mcp(db_session: Session) -> None:
    """Idempotently registers unifi-network-mcp as an Onyx MCP server and
    attaches its discovered tools to the "UniFi Security Agent" persona
    (seeded by 77970041a87b_seed_unifi_security_agent_persona.py), driven by
    UNIFI_NETWORK_MCP_URL.

    A no-op if that env var isn't set -- most environments (local dev, CI)
    have no Mac Mini to point at. Best-effort otherwise: logs and returns on
    any failure (e.g. the Mac Mini is temporarily unreachable over
    Tailscale) rather than blocking Onyx's own startup -- this can just run
    again on the next boot.

    This is deliberately code, not a migration: the server URL is
    environment-specific (which physical Mac Mini, which Tailscale IP),
    unlike the persona itself which is identical everywhere.
    """
    if not UNIFI_NETWORK_MCP_URL:
        return

    persona = (
        db_session.query(Persona)
        .filter(Persona.name == SECURITY_AGENT_PERSONA_NAME)
        .first()
    )
    if persona is None:
        logger.warning(
            "UNIFI_NETWORK_MCP_URL is set but the '%s' persona doesn't exist "
            "yet -- skipping MCP registration.",
            SECURITY_AGENT_PERSONA_NAME,
        )
        return

    try:
        mcp_server = next(
            (
                s
                for s in get_all_mcp_servers(db_session)
                if s.server_url == UNIFI_NETWORK_MCP_URL
            ),
            None,
        )
        if mcp_server is None:
            owner_email = next(
                (u.email for u in get_all_users(db_session) if u.role == UserRole.ADMIN),
                "unifi-mcp-setup@onyx.internal",
            )
            mcp_server = create_mcp_server__no_commit(
                owner_email=owner_email,
                name="UniFi Network (Mac Mini)",
                description=(
                    "unifi-network-mcp on the on-site Mac Mini, reachable "
                    "over Tailscale. No authentication -- access is scoped "
                    "by network reachability, not credentials."
                ),
                server_url=UNIFI_NETWORK_MCP_URL,
                auth_type=None,
                transport=MCPTransport.STREAMABLE_HTTP,
                auth_performer=None,
                db_session=db_session,
                is_public=True,
            )
            db_session.flush()
            logger.notice(
                "Registered MCP server '%s' (id=%s) for %s",
                mcp_server.name,
                mcp_server.id,
                UNIFI_NETWORK_MCP_URL,
            )

        discovered_tools = discover_mcp_tools(
            UNIFI_NETWORK_MCP_URL, transport=MCPTransport.STREAMABLE_HTTP
        )
        existing_by_name = {
            tool.name: tool
            for tool in get_tools_by_mcp_server_id(mcp_server.id, db_session)
        }
        for tool in discovered_tools:
            if tool.name in existing_by_name:
                continue
            new_tool = create_tool__no_commit(
                name=tool.name,
                description=tool.description or "",
                openapi_schema=None,
                custom_headers=None,
                user_id=None,
                db_session=db_session,
                passthrough_auth=False,
                mcp_server_id=mcp_server.id,
                enabled=True,
            )
            new_tool.display_name = tool.title or tool.name
            new_tool.mcp_input_schema = tool.inputSchema

        tools = get_tools_by_mcp_server_id(mcp_server.id, db_session)
        existing_persona_tool_ids = {t.id for t in persona.tools}
        attached = 0
        for tool in tools:
            if tool.id not in existing_persona_tool_ids:
                persona.tools.append(tool)
                attached += 1

        db_session.commit()
        logger.notice(
            "UniFi Security Agent: %s tool(s) discovered, %s newly attached",
            len(tools),
            attached,
        )

        # This deployment is purpose-built for UniFi security monitoring
        # (Kenny has no need for the generic default assistant) -- make the
        # persona the fallback for any chat where no agent was explicitly
        # picked, instead of the generic id=0 "Assistant". Does NOT delete
        # or hide "Assistant" -- it stays fully selectable, this only
        # changes what an unpicked chat defaults to (see liveAgent's
        # fallback logic in web/src/lib/agents/hooks.ts).
        settings = load_settings()
        if not settings.disable_default_assistant:
            settings.disable_default_assistant = True
            store_settings(settings)
            logger.notice(
                "Set disable_default_assistant=true so '%s' is the default "
                "agent instead of 'Assistant'.",
                SECURITY_AGENT_PERSONA_NAME,
            )
    except Exception:
        db_session.rollback()
        logger.exception(
            "Failed to register unifi-network-mcp / attach tools to '%s' "
            "(is the Mac Mini reachable at %s?) -- will retry on next boot.",
            SECURITY_AGENT_PERSONA_NAME,
            UNIFI_NETWORK_MCP_URL,
        )


def update_default_multipass_indexing(db_session: Session) -> None:
    docs_exist = check_docs_exist(db_session)
    connectors_exist = check_connectors_exist(db_session)
    logger.debug("Docs exist: %s, Connectors exist: %s", docs_exist, connectors_exist)

    if not docs_exist and not connectors_exist:
        logger.info(
            "No existing docs or connectors found. Checking GPU availability for multipass indexing."
        )
        gpu_available = gpu_status_request(indexing=True)
        logger.info("GPU available: %s", gpu_available)

        current_settings = get_current_search_settings(db_session)

        logger.notice("Updating multipass indexing setting to: %s", gpu_available)
        updated_settings = SavedSearchSettings.from_db_model(current_settings)
        # Enable multipass indexing if GPU is available or if using a cloud provider
        updated_settings.multipass_indexing = (
            gpu_available or current_settings.cloud_provider is not None
        )
        update_current_search_settings(db_session, updated_settings)

        # Update settings with GPU availability
        settings = load_settings()
        settings.gpu_enabled = gpu_available
        store_settings(settings)
        logger.notice("Updated settings with GPU availability: %s", gpu_available)

    else:
        logger.debug(
            "Existing docs or connectors found. Skipping multipass indexing update."
        )


def setup_multitenant_onyx() -> None:
    if DISABLE_VECTOR_DB:
        logger.notice("DISABLE_VECTOR_DB is set — skipping multitenant Vespa setup.")
        return

    if ENABLE_OPENSEARCH_INDEXING_FOR_ONYX:
        opensearch_client = OpenSearchClient()
        if not wait_for_opensearch_with_timeout(client=opensearch_client):
            raise RuntimeError("Failed to connect to OpenSearch.")
        set_cluster_state(opensearch_client)

    # For Managed Vespa, the schema is sent over via the Vespa Console manually.
    # NOTE: Pretty sure this code is never hit in any production environment.
    if not MANAGED_VESPA and not ONYX_DISABLE_VESPA:
        setup_vespa_multitenant(SUPPORTED_EMBEDDING_MODELS)


def setup_vespa_multitenant(supported_indices: list[SupportedEmbeddingModel]) -> bool:
    # TODO(andrei): We don't yet support OpenSearch for multi-tenant instances
    # so this function remains unchanged.
    # This is for local testing
    WAIT_SECONDS = 5
    VESPA_ATTEMPTS = 5
    for x in range(VESPA_ATTEMPTS):
        try:
            logger.notice("Setting up Vespa (attempt %s/%s)...", x + 1, VESPA_ATTEMPTS)
            register_multitenant_vespa_indices(
                indices=[index.index_name for index in supported_indices]
                + [
                    f"{index.index_name}{ALT_INDEX_SUFFIX}"
                    for index in supported_indices
                ],
                embedding_dims=[index.dim for index in supported_indices]
                + [index.dim for index in supported_indices],
                # on the cloud, just use float for all indices, the option to change this
                # is not exposed to the user
                embedding_precisions=[
                    EmbeddingPrecision.FLOAT for _ in range(len(supported_indices) * 2)
                ],
            )

            logger.notice("Vespa setup complete.")
            return True
        except Exception:
            logger.notice(
                "Vespa setup did not succeed. The Vespa service may not be ready yet. Retrying in %s seconds.",
                WAIT_SECONDS,
            )
            time.sleep(WAIT_SECONDS)

    logger.error(
        "Vespa setup did not succeed. Attempt limit reached. (%s)", VESPA_ATTEMPTS
    )
    return False
