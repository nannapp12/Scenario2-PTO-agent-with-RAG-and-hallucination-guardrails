"""Authorization for the PTO endpoint, which returns employee data (PII).

In Azure, Container Apps built-in auth (Entra ID) signs the user in and injects the
X-MS-CLIENT-PRINCIPAL header, removing any copy the client sent. The header can only
be trusted when that auth is on, so mode "easyauth" is set by infra only when the
auth config is deployed. Without it, /pto is off ("disabled") instead of being open.

On AKS (Scenario3) mode "proxy" is used instead: every /pto request goes through
oauth2-proxy (Entra ID sign-in, app role required), which replaces any client-sent
X-Forwarded-User/-Groups headers with the signed-in user's. A NetworkPolicy admits
traffic to this pod only from oauth2-proxy, so forged headers can't reach it.
"""
import base64
import binascii
import json

from fastapi import HTTPException, Request

from .config import settings

_ROLE_CLAIMS = {"roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}


def require_pto_reader(request: Request) -> None:
    mode = settings.pto_auth_mode
    if mode == "none":
        return
    if mode == "proxy":
        if not request.headers.get("x-forwarded-user"):
            raise HTTPException(401, "Sign in to look up PTO balances.")
        # oauth2-proxy runs with --oidc-groups-claim=roles, so the app roles arrive as groups.
        roles = {r.strip() for h in request.headers.getlist("x-forwarded-groups") for r in h.split(",")}
        if settings.pto_required_role not in roles:
            raise HTTPException(403, "You don't have permission to look up PTO balances.")
        return
    if mode != "easyauth":
        raise HTTPException(503, "PTO lookup is disabled until sign-in is configured.")

    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        raise HTTPException(401, "Sign in to look up PTO balances.")
    try:
        principal = json.loads(base64.b64decode(raw))
    except (binascii.Error, ValueError):
        raise HTTPException(401, "Sign in to look up PTO balances.")
    roles = {c.get("val") for c in principal.get("claims", []) if c.get("typ") in _ROLE_CLAIMS}
    if settings.pto_required_role not in roles:
        raise HTTPException(403, "You don't have permission to look up PTO balances.")
