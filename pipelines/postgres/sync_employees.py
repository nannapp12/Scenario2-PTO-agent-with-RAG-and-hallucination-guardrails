"""Scheduled sync: Employee table in Databricks on AWS -> hr.employees in Azure PostgreSQL.

Runs hourly as the Container Apps Job `<prefix>-employee-sync`.

AWS -> Azure in transit: the Databricks SQL connector talks HTTPS (TLS 1.2+) to the
workspace on AWS, verifying the server certificate and hostname, and authenticates
with an OAuth M2M service principal (secret in Key Vault). The workspace IP access
list should only allow this job's egress IP. Azure side: PostgreSQL over TLS
(verify-full) with an Entra ID token.

PII: the Databricks view returns only the columns PTO needs, for active employees.
Names and emails are AES-256-GCM encrypted here, before they reach PostgreSQL, and
are never logged.

Safety: the sync is a full snapshot applied in one transaction (upsert + delete
employees no longer in the source). If the source suddenly has far fewer rows (an
upstream outage or a broken view), the job stops instead of deleting most employees.
"""
import argparse
import csv
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

if len(_parents := Path(__file__).resolve().parents) > 2:
    sys.path.insert(0, str(_parents[1]))  # repo root, for `common` when run locally (the image sets PYTHONPATH)

from common import db  # noqa: E402
from common.pii import PiiCipher, aad  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("employee-sync")

COLUMNS = ["employee_id", "first_name", "last_name", "email", "start_date", "employment_type", "updated_at"]
EMPLOYMENT_TYPES = {"FULL_TIME", "PART_TIME", "TEMPORARY"}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")   # keep in sync with EMPLOYEE_ID_PATTERN in the API
VIEW_RE = re.compile(r"[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){2}")


def read_databricks() -> list[dict]:
    from databricks import sql
    from databricks.sdk.core import Config, oauth_service_principal

    host = os.environ["DATABRICKS_AWS_HOST"]          # dbc-xxxx.cloud.databricks.com (HTTPS only)
    if "://" in host and not host.startswith("https://"):
        raise ValueError("DATABRICKS_AWS_HOST must use HTTPS.")
    host = host.removeprefix("https://").rstrip("/")
    view = os.environ.get("DATABRICKS_EMPLOYEES_VIEW", "hr_catalog.people.v_employees_pto_sync")
    if not VIEW_RE.fullmatch(view):
        raise ValueError(f"Invalid view name: {view}")

    def credentials():
        return oauth_service_principal(Config(
            host=f"https://{host}",
            client_id=os.environ["DATABRICKS_AWS_CLIENT_ID"],
            client_secret=os.environ["DATABRICKS_AWS_CLIENT_SECRET"],   # Key Vault reference
        ))

    with sql.connect(server_hostname=host, http_path=os.environ["DATABRICKS_AWS_HTTP_PATH"],
                     credentials_provider=credentials) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM {view}")
        return [dict(zip(COLUMNS, r)) for r in cur.fetchall()]


def read_csv(path: str) -> list[dict]:
    """Local development source with the same columns as the Databricks view."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _date(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def _ts(v) -> datetime | None:
    if v in (None, ""):
        return None
    ts = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def validate(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop rows that can't be used; keep the latest row per employee_id."""
    latest: dict[str, dict] = {}
    skipped = 0
    for r in rows:
        try:
            eid = str(r["employee_id"]).strip()
            etype = str(r["employment_type"]).strip().upper()
            if not ID_RE.fullmatch(eid) or etype not in EMPLOYMENT_TYPES:
                raise ValueError
            clean = {
                "employee_id": eid,
                "full_name": " ".join(p for p in (r.get("first_name"), r.get("last_name")) if p) or None,
                "email": (r.get("email") or "").strip() or None,
                "start_date": _date(r["start_date"]),
                "employment_type": etype,
                "updated_at": _ts(r.get("updated_at")),
            }
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        prev = latest.get(eid)
        if prev is None or (clean["updated_at"] or datetime.min.replace(tzinfo=timezone.utc)) >= \
                (prev["updated_at"] or datetime.min.replace(tzinfo=timezone.utc)):
            latest[eid] = clean
    return list(latest.values()), skipped


def write(conn, cipher: PiiCipher, rows: list[dict], skipped: int, source_rows: int,
          max_delete_fraction: float, started: datetime) -> None:
    with conn.transaction():
        current = conn.execute("SELECT count(*) FROM hr.employees").fetchone()[0]
        if current and len(rows) < current * (1 - max_delete_fraction):
            raise RuntimeError(f"Source has {len(rows)} valid employees vs {current} loaded; refusing to delete "
                               f"more than {max_delete_fraction:.0%}. Check the Databricks view, then rerun "
                               "with a higher SYNC_MAX_DELETE_FRACTION if the drop is real.")
        conn.execute("CREATE TEMP TABLE stage (LIKE hr.employees INCLUDING DEFAULTS) ON COMMIT DROP")
        with conn.cursor() as cur, cur.copy(
            "COPY stage (employee_id, full_name_enc, email_enc, start_date, employment_type, source_updated_at) "
            "FROM STDIN"
        ) as copy:
            for r in rows:
                eid = r["employee_id"]
                copy.write_row((eid,
                                cipher.encrypt(r["full_name"], aad(eid, "full_name")),
                                cipher.encrypt(r["email"], aad(eid, "email")),
                                r["start_date"], r["employment_type"], r["updated_at"]))
        upserted = conn.execute(
            "INSERT INTO hr.employees (employee_id, full_name_enc, email_enc, start_date, employment_type, "
            "source_updated_at, synced_at) "
            "SELECT employee_id, full_name_enc, email_enc, start_date, employment_type, source_updated_at, now() "
            "FROM stage ON CONFLICT (employee_id) DO UPDATE SET "
            "full_name_enc = EXCLUDED.full_name_enc, email_enc = EXCLUDED.email_enc, "
            "start_date = EXCLUDED.start_date, employment_type = EXCLUDED.employment_type, "
            "source_updated_at = EXCLUDED.source_updated_at, synced_at = now()"
        ).rowcount
        deleted = conn.execute(
            "DELETE FROM hr.employees e WHERE NOT EXISTS (SELECT 1 FROM stage s WHERE s.employee_id = e.employee_id)"
        ).rowcount
        conn.execute(
            "INSERT INTO hr.sync_runs (started_at, source_rows, skipped_rows, upserted_rows, deleted_rows) "
            "VALUES (%s, %s, %s, %s, %s)",
            (started, source_rows, skipped, upserted, deleted),
        )
    log.info("Synced employees: source=%d skipped=%d upserted=%d deleted=%d",
             source_rows, skipped, upserted, deleted)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", choices=["databricks", "csv"], default="databricks")
    p.add_argument("--csv", help="CSV path for --source csv (local development)")
    args = p.parse_args(argv)

    started = datetime.now(timezone.utc)
    raw = read_databricks() if args.source == "databricks" else read_csv(args.csv)
    rows, skipped = validate(raw)
    if not rows:
        raise RuntimeError("Source returned no valid employees; nothing was changed.")
    if skipped:
        log.warning("Skipped %d invalid source rows (bad ID, employment type or start date).", skipped)

    cipher = PiiCipher(os.environ["PII_ENCRYPTION_KEY"], os.environ.get("PII_ENCRYPTION_KEY_PREVIOUS", ""))
    info = db.conninfo(os.environ["PG_HOST"], os.environ.get("PG_DATABASE", "hr_rag"), os.environ["PG_USER"],
                       os.environ.get("PG_SSLMODE", "verify-full"),
                       os.environ.get("PG_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"))
    with db.connect(info, os.environ.get("PG_PASSWORD", "")) as conn:
        write(conn, cipher, rows, skipped, len(raw),
              float(os.environ.get("SYNC_MAX_DELETE_FRACTION", "0.2")), started)


if __name__ == "__main__":
    main()
