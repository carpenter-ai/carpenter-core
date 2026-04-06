"""Trust boundary system -- integrity levels, audit logging, encryption.

Re-exports key public symbols::

    from carpenter.core.trust import IntegrityLevel, AgentType, log_trust_event
"""

# Integrity lattice
from .integrity import IntegrityLevel, join, is_trusted, is_non_trusted, validate_integrity_level  # noqa: F401

# Type system
from .types import OutputType, AgentType, get_agent_capabilities, validate_output_type, validate_agent_type  # noqa: F401

# Audit
from .audit import log_trust_event, get_trust_events  # noqa: F401

# Encryption
from .encryption import generate_arc_key, encrypt_output, decrypt_for_reviewer, decrypt_after_promotion  # noqa: F401

# Template capability grants
from .capabilities import (  # noqa: F401
    CAPABILITY_TOOL_GRANTS, SCOPE_BYPASS_CAPABILITIES,
    get_arc_capabilities, resolve_capability_tools,
)
