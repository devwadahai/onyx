"""seed unifi security agent persona

Revision ID: 77970041a87b
Revises: 3debc2b55899
Create Date: 2026-08-12 19:40:00.000000

Adds the "UniFi Security Agent" persona referenced by name (not id -- ids
differ across environments) from
backend/onyx/server/features/unifi_events/api.py's SECURITY_AGENT_PERSONA_NAME
and the deep-link it builds into security-event notifications. Previously
this persona only existed if someone created it by hand through the admin
UI on a given deployment, which is exactly the kind of "not actually in the
repo" gap that leaves a fresh/reset deployment silently missing it. This
migration makes it a normal, portable part of the schema/seed data, the
same way the default "Assistant" persona (see
505c488f6662_merge_default_assistants_into_unified.py) is seeded.

Deliberately does NOT set default_model_configuration_id (left NULL, so the
persona uses whatever the deployment's default LLM provider/model is) and
does NOT attach any MCP tools here -- that part is env-specific (which
unifi-network-mcp instance, reachable at which URL) and handled at
application startup instead, driven by UNIFI_NETWORK_MCP_URL (see
onyx/setup.py), not baked into a migration that runs identically in every
environment including ones with no Mac Mini at all.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "77970041a87b"
down_revision = "3debc2b55899"
branch_labels = None
depends_on = None

PERSONA_NAME = "UniFi Security Agent"
PERSONA_DESCRIPTION = (
    "Investigates UniFi network security events and answers questions about "
    "the state of the network -- clients, devices, firewall rules, alerts. "
    "Read-only by default; any change to the network requires your explicit "
    "confirmation before it happens."
)
SYSTEM_PROMPT = """
You are the UniFi Security Agent for this property's network, built on the UniFi Network \
Integration API via unifi-network-mcp. Your job is to help a human understand what's \
happening on the network and investigate security events -- unusual clients, firewall \
activity, device status, IPS/IDS alerts -- and to explain findings in plain language, not \
raw API output.

Default to read-only investigation: list/get tools first to understand what's actually \
going on before suggesting any action. Tools that change network state (blocking a client, \
changing firewall rules, rebooting a device, etc.) require the user's explicit confirmation \
in the conversation before you call them -- never invoke a mutating tool as a first response \
to an ambiguous request, and always say plainly what a mutating tool will do before calling it.

Be specific and cite what you actually found (device names, client MACs/IPs, timestamps, \
rule names) rather than generic reassurance. If something looks like it needs the property \
owner's attention, say so directly instead of downplaying it.
""".strip()


def upgrade() -> None:
    conn = op.get_bind()

    existing = conn.execute(
        sa.text("SELECT id FROM persona WHERE name = :name"),
        {"name": PERSONA_NAME},
    ).fetchone()
    if existing:
        return

    conn.execute(
        sa.text(
            """
            INSERT INTO persona (
                name, description, system_prompt, builtin_persona,
                is_featured, is_listed, is_public, display_priority, deleted,
                replace_base_system_prompt, datetime_aware
            ) VALUES (
                :name, :description, :system_prompt, true,
                false, true, true, NULL, false,
                false, true
            )
            """
        ),
        {
            "name": PERSONA_NAME,
            "description": PERSONA_DESCRIPTION,
            "system_prompt": SYSTEM_PROMPT,
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM persona WHERE name = :name AND builtin_persona = true"),
        {"name": PERSONA_NAME},
    )
