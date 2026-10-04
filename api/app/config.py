"""Settings. In Azure, secret values are injected from Key Vault references;
locally they come from a .env file (never commit it)."""
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Azure Database for PostgreSQL (pgvector handbook chunks + synced employees)
    pg_host: str
    pg_database: str = "hr_rag"
    pg_user: str                            # managed identity name (Entra auth) or a local user
    pg_password: str = ""                   # local dev only; empty = Entra ID token
    pg_sslmode: str = "verify-full"
    pg_sslrootcert: str = "/etc/ssl/certs/ca-certificates.crt"

    pii_encryption_key: str                 # Key Vault secret: pii-encryption-key (base64, 32 bytes)
    pii_encryption_key_previous: str = ""   # set only while rotating keys

    # Azure OpenAI deployments in the Foundry resource (Entra ID auth). Empty endpoint
    # = no retrieval and templated explanations; the PTO numbers are unaffected.
    azure_openai_endpoint: str = ""         # https://<resource>.openai.azure.com
    azure_openai_api_version: str = "2024-10-21"
    chat_deployment: str = "gpt-4.1"
    embedding_deployment: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536        # must match rag.chunks.embedding
    pto_llm_explanation: bool = True        # False = templated explanation only

    # "easyauth": Container Apps Entra ID auth + app role; "proxy": oauth2-proxy behind the
    # AKS ingress (Scenario3); "none": local dev only; "disabled": /pto returns 503.
    # Never use "none" on a public endpoint.
    pto_auth_mode: Literal["easyauth", "proxy", "none", "disabled"] = "disabled"
    pto_required_role: str = "PTO.Read"
    employee_id_pattern: str = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"
    company_timezone: str = "UTC"
    rag_top_k: int = 4
    rag_min_score: float = 0.25

    allowed_origins: str = ""               # comma-separated; empty = same-origin only


settings = Settings()
