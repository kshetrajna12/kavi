"""Kavi configuration and path constants."""

from pathlib import Path

# Project root is determined relative to where kavi is invoked
# In practice, this will be the repo root
PROJECT_ROOT = Path.cwd()

# Output directories
VAULT_OUT = PROJECT_ROOT / "vault_out"
ARTIFACTS_OUT = PROJECT_ROOT / "artifacts_out"

# Default subdirectory for Obsidian-compatible notes
VAULT_INBOX = VAULT_OUT / "Inbox" / "AI"

# Ledger database
LEDGER_DB = PROJECT_ROOT / "kavi.db"

# Skill registry
SKILLS_DIR = Path(__file__).parent / "skills"
REGISTRY_PATH = SKILLS_DIR / "registry.yaml"

# Policy config
POLICY_PATH = Path(__file__).parent / "policies" / "policy.yaml"

# Sparkstation (local LLM gateway)
SPARK_BASE_URL = "http://localhost:8000/v1"
SPARK_MODEL = "gpt-oss-20b"
SPARK_EMBED_MODEL = "bge-large"
SPARK_TIMEOUT = 30  # seconds
SPARK_MAX_PROMPT_CHARS = 8000  # bound input before sending
