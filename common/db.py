"""PostgreSQL connections for the PTO feature.

- TLS with certificate and hostname verification (sslmode=verify-full). The server
  also enforces it (require_secure_transport=ON, ssl_min_protocol_version=TLSv1.2).
- Microsoft Entra ID auth: the password is a short-lived token for the app's managed
  identity, fetched for every new connection, so no database password exists.
  PG_PASSWORD is for local development against a non-Azure Postgres only.
"""
import psycopg
from azure.identity import DefaultAzureCredential
from psycopg.conninfo import make_conninfo

_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_credential: DefaultAzureCredential | None = None


def _token() -> str:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential.get_token(_SCOPE).token


def conninfo(host: str, dbname: str, user: str, sslmode: str = "verify-full",
             sslrootcert: str = "/etc/ssl/certs/ca-certificates.crt", port: int = 5432) -> str:
    params = {"host": host, "port": port, "dbname": dbname, "user": user,
              "sslmode": sslmode, "connect_timeout": 10, "application_name": "pto"}
    if sslmode in ("verify-ca", "verify-full"):
        params["sslrootcert"] = sslrootcert
    return make_conninfo(**params)


def connection_class(password: str = "") -> type[psycopg.Connection]:
    """A Connection subclass that adds the password (or a fresh Entra token) on connect.
    Pass it to psycopg_pool.ConnectionPool(connection_class=...) so pooled connections
    opened after the first token expires still authenticate."""

    class _Connection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            kwargs["password"] = password or _token()
            return super().connect(conninfo, **kwargs)

    return _Connection


def connect(info: str, password: str = "", **kwargs) -> psycopg.Connection:
    return connection_class(password).connect(info, **kwargs)


def vector_literal(values: list[float]) -> str:
    """pgvector text form; pass with a ::vector cast."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"
