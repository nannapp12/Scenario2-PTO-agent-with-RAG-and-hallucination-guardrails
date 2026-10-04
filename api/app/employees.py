"""Deterministic employee lookup in hr.employees (synced from Databricks on AWS).

An exact primary-key match is the only way an employee is found. Nothing is fuzzy-matched
or inferred, so an unknown ID can only produce "Employee not found".
"""
from dataclasses import dataclass
from datetime import date

from psycopg_pool import ConnectionPool

from common.pii import PiiCipher, aad


@dataclass(frozen=True)
class Employee:
    employee_id: str
    full_name: str | None
    email: str | None
    start_date: date
    employment_type: str


class EmployeeRepository:
    def __init__(self, pool: ConnectionPool, cipher: PiiCipher):
        self.pool, self.cipher = pool, cipher

    def find(self, employee_id: str) -> Employee | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT employee_id, full_name_enc, email_enc, start_date, employment_type "
                "FROM hr.employees WHERE employee_id = %s",
                (employee_id,),
            ).fetchone()
        if row is None:
            return None
        eid = row[0]
        return Employee(
            employee_id=eid,
            full_name=self.cipher.decrypt(row[1], aad(eid, "full_name")),
            email=self.cipher.decrypt(row[2], aad(eid, "email")),
            start_date=row[3],
            employment_type=row[4],
        )

    def handbook_sha256(self, doc_id: str) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT sha256 FROM rag.documents WHERE doc_id = %s", (doc_id,)).fetchone()
        return row[0] if row else None
