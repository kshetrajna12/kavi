"""Shared constants for the agent layer."""

# Side-effect classes that require user confirmation before execution
CONFIRM_SIDE_EFFECTS: frozenset[str] = frozenset({
    "FILE_WRITE", "NETWORK", "SECRET_READ",
})

# Side-effect classes allowed from the chat interface by default.
# NETWORK and SECRET_READ are gated — they must be explicitly enabled
# via the allowed_effects parameter to prevent accidental exposure of
# new skills through conversation.
CHAT_DEFAULT_ALLOWED_EFFECTS: frozenset[str] = frozenset({
    "READ_ONLY", "FILE_WRITE",
})
