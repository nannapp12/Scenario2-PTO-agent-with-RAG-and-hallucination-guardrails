# Employee PTO Balance (Handbook RAG + Databricks on AWS)

The website looks up an employee ID and shows how much vacation (PTO) the employee has
available today, assuming no leave has been taken. The balance is **computed in code**
from the start date in the Employee table (Databricks on AWS) and the accrual policy in
the Employee Handbook. The handbook is chunked, embedded and stored in **PostgreSQL +
pgvector**; retrieval supplies the policy text that's cited on the page and given to an
LLM, which only writes a plain-language explanation of numbers it is handed.

```
 Browser (web/)                 Azure Container Apps (VNet, HTTPS only)
 ┌──────────────────┐  HTTPS   ┌─────────────────────────────┐ Entra ID ┌──────────────────────────┐
 │ Employee ID form │─────────▶│ FastAPI (api/)               │─────────▶│ Azure OpenAI (Foundry)   │
 │ Entra sign-in    │◀─────────│ GET /pto/{employee_id}       │          │ embeddings + gpt-4.1     │
 └──────────────────┘          │ 1 validate ID → DB lookup    │          └──────────────────────────┘
                               │   (unknown → 404 "Employee   │
                               │    not found", stop)         │ TLS verify-full + Entra token
                               │ 2 compute PTO (pto.py)       │────────────┐
                               │ 3 retrieve handbook (pgvector)│           ▼
                               │ 4 explain (grounded) / template│  Azure PostgreSQL Flexible Server
                               └─────────────────────────────┘   hr_rag: rag.chunks (vector(1536))
                                                                         hr.employees (PII encrypted)
   Container Apps Job (hourly)                                             ▲            ▲
   employee-sync ── HTTPS/TLS 1.2+, OAuth M2M ──▶ Databricks on AWS        │            │
        │           (static NAT egress IP,        hr_catalog.people.       │            │
        │            Databricks IP access list)   v_employees_pto_sync     │            │
        └──── encrypt name/email (AES-256-GCM) ───────────────────────────┘            │
   Container Apps Job (manual)                                                          │
   handbook-ingest ── ADLS landing/handbook/*.pdf → chunk → embed → load ───────────────┘
```

## Repository layout

| Path | What it is |
|---|---|
| `api/app/main.py` | FastAPI app: `GET /pto/{employee_id}`, `GET /healthz`; serves `web/` |
| `api/app/pto_service.py` | The request flow and guardrail order: validate → lookup → compute → retrieve → explain |
| `api/app/pto.py`, `pto_policy.json` | Deterministic accrual calculation and the policy transcribed from the handbook |
| `api/app/pto_explainer.py` | LLM explanation with a numeric-grounding check, templated fallback |
| `api/app/employees.py`, `rag.py` | Exact-match employee lookup (decrypts PII); pgvector retrieval |
| `api/app/auth.py` | Entra ID sign-in + `PTO.Read` app role (Container Apps auth, or oauth2-proxy on AKS) |
| `common/` | Shared by API and pipelines: PII encryption/masking, PostgreSQL TLS + Entra connections, embeddings |
| `pipelines/postgres/ingest_handbook.py` | **Ingestion pipeline**: handbook → chunk (`chunking.py`) → embed → load into pgvector |
| `pipelines/postgres/sync_employees.py` | **Scheduled sync**: Databricks on AWS → `hr.employees` (encrypted) |
| `pipelines/postgres/schema.sql`, `roles.sql` | Tables, HNSW index, least-privilege Entra roles |
| `pipelines/databricks_aws/employees_setup.sql` | Column masks, the minimal sync view, grants for the sync service principal |
| `web/` | The lookup page |
| `infra/main.bicep` | VNet + NAT gateway, Key Vault, PostgreSQL, Storage, ACR, Container Apps (API, 2 jobs), auth |
| `data/handbook/` | The Employee Handbook PDF; `data/sample_employees.csv` for local testing |

## PTO policy (handbook section V.B "Vacation Benefits")

| Rule | Handbook | In code (`pto_policy.json`) |
|---|---|---|
| Eligibility | All regular full-time employees | `eligible_employment_types: ["FULL_TIME"]`; others get 0 |
| Years 1–5 | 3.077 h biweekly, max 10 days/yr | tier `min_years: 0` |
| Years 6–10 | 4.62 h biweekly, max 15 days/yr | tier `min_years: 5` |
| Year 11+ | 6.15 h biweekly, max 20 days/yr | tier `min_years: 10` |
| Annual maximum | Based on the fiscal year | capped per fiscal year (`fiscal_year_start_month`) |
| Cap | Accrued vacation ≤ 2× annual allotment; accrual stops at the cap | `cap_multiple: 2` |
| Waiting period | 90 calendar days before use | `available = 0` until `start_date + 90` |

Assumptions (listed in the JSON too; HR should confirm them): an 8-hour day, biweekly
periods counted from the start date, fiscal year starting in January (the handbook leaves
`[FISCAL YEAR]` blank), and eligibility starting on the start date.

When the handbook changes, re-run ingestion. It prints the new hash. Update the JSON and
its `handbook_sha256` after HR reviews the PTO section. Until then every `/pto` response
carries a warning that the handbook has changed.

## Guardrails against hallucination

1. **Deterministic ID check first.** The ID is checked against a strict pattern and looked
   up in `hr.employees` by exact primary key. A malformed or unknown ID returns
   `404 {"detail": "Employee not found"}`. Retrieval and the LLM are never called, so
   there's nothing to guess from (`test_unknown_employee_returns_not_found_without_retrieval_or_llm`).
2. **Numbers come only from code.** `pto.calculate` uses exact fractions. Every numeric field
   in the response comes from it, never from model output.
3. **Grounded explanation.** The model receives only the computed facts and the retrieved
   handbook excerpts. It gets no name, email or employee ID. Its text is accepted only if
   every number in it appears in those facts or excerpts and it states the computed
   balance. Otherwise, or on any error or timeout, a deterministic template is used
   (`explanation_source` shows which).
4. **Retrieval is scoped** to the policy's handbook document with a minimum similarity
   score. If retrieval fails, the balance is still returned with a warning.

## Security

| Requirement | How it's met |
|---|---|
| **PII encrypted at rest** | PostgreSQL storage, backups and WAL use AES-256 (CMK optional via `dataEncryption`). Names and emails are also encrypted per field with AES-256-GCM (`common/pii.py`), and the associated data binds each value to its employee and column. The key is in Key Vault. To rotate it, set `PII_ENCRYPTION_KEY_PREVIOUS`; the next sync re-encrypts every row. Databricks on AWS: customer-managed KMS keys for workspace storage and managed services, and SSE-KMS on the Unity Catalog bucket. Storage: infrastructure (double) encryption. |
| **PII masked** | The API returns only `J*** D***` and `j***@domain`, and full values never leave it. Unity Catalog column masks show masked values to everyone outside `hr-pii-readers`. Logs record a hash of the employee ID, never names. `/pto` responses are `Cache-Control: no-store`. |
| **In transit** | Browser → API: HTTPS only (ingress `allowInsecure: false`, TLS 1.2+, HSTS). API/jobs → PostgreSQL: `sslmode=verify-full`, and the server requires TLS 1.2+. → Azure OpenAI and Storage: HTTPS with Entra ID. |
| **AWS ↔ Azure link** | The sync job connects to the Databricks SQL warehouse on AWS over HTTPS (TLS 1.2+, certificate and hostname verified) as an OAuth M2M service principal whose secret is in Key Vault. All Container Apps egress goes through a NAT gateway with one static IP (`egressIpAddress` output). The Databricks workspace IP access list allows only that IP, and so does the PostgreSQL firewall. |
| **Access control** | `/pto` requires Entra ID sign-in and the `PTO.Read` app role. Without auth configured, `/pto` returns 503 rather than being open. PostgreSQL: the API identity can only `SELECT`, the pipelines identity writes, and there are no passwords. Databricks: the sync SP can read only the minimal view (active employees, the needed columns). |
| **Sync safety** | A full snapshot is applied in one transaction. Invalid rows are skipped and counted. The job refuses to delete more than 20% of employees in one run (`SYNC_MAX_DELETE_FRACTION`). |

## Deploy

Prerequisites: Azure CLI, a Foundry/Azure OpenAI resource with `gpt-4.1` and
`text-embedding-3-small` deployments, and a Databricks on AWS workspace with Unity Catalog,
a SQL warehouse and the Employee table.

**1. Generate the PII key** (keep it safe; it's stored in Key Vault):

```bash
python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

**2. Databricks on AWS:**
1. Create a service principal `pto-sync-sp` with an OAuth secret.
2. Add the principal to `hr-pii-readers`, and give it `CAN USE` on the warehouse.
3. Run `pipelines/databricks_aws/employees_setup.sql`, adjusting the view to your table's columns.

**3. Entra app for website sign-in:**
1. Register an app with the redirect URI `https://<apiUrl>/.auth/login/aad/callback`.
2. Add an app role `PTO.Read` and assign it to HR users.
3. Create a client secret.

**4. Create the infrastructure without the apps:**

```bash
az group create -n rg-pto-rag -l eastus
```
```bash
az deployment group create -g rg-pto-rag -f infra/main.bicep -p deployApps=false pgEntraAdminObjectId=<oid> pgEntraAdminName=<upn> pgClientIp=<your-ip> piiEncryptionKey=<key> azureOpenAIEndpoint=<endpoint> databricksAwsHost=<dbc-xxxx.cloud.databricks.com> databricksAwsHttpPath=<path> databricksAwsClientId=<sp-app-id> databricksAwsClientSecret=<secret> entraAuthClientId=<app-id> entraAuthClientSecret=<secret>
```

**5. Restrict Databricks to the Azure egress IP** (the `egressIpAddress` output). Keep your admin IPs in the list too:

```bash
databricks ip-access-lists create --label azure-pto-sync --list-type ALLOW --ip-addresses <egressIpAddress>
```

**6. PostgreSQL schema and roles.** Sign in as the Entra admin, using a token as the password:

```bash
psql "host=<pgHost> dbname=hr_rag user=<upn> sslmode=verify-full sslrootcert=system password=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)" -f pipelines/postgres/schema.sql
```

Then run `roles.sql`: part 1 against database `postgres`, part 2 against `hr_rag`.

**7. Allow the identities to call Azure OpenAI.** Give both identities' principal IDs
the **Cognitive Services OpenAI User** role on the Foundry/Azure OpenAI resource.

**8. Build the images:**

```bash
az acr build -r <acr> -t pto-api:latest -f api/Dockerfile .
```
```bash
az acr build -r <acr> -t pto-pipelines:latest -f pipelines/postgres/Dockerfile .
```

**9. Deploy the apps:** rerun step 4 with `deployApps=true`.

**10. Load data:**
1. Upload the handbook to `landing/handbook/` in the storage account.
2. Start the ingestion job:
   ```bash
   az containerapp job start -n ptorag-handbook-ingest -g rg-pto-rag
   ```
3. Start the first sync (after that it runs hourly at :15):
   ```bash
   az containerapp job start -n ptorag-employee-sync -g rg-pto-rag
   ```

Open the `apiUrl` output, sign in, and look up an employee ID.

## Run locally

Run these from the repo root, with `az login` done and your IP allowed on PostgreSQL:

```bash
cp api/.env.example api/.env
```
```bash
pip install -r api/requirements.txt -r pipelines/postgres/requirements.txt
```
```bash
python pipelines/postgres/ingest_handbook.py --path data/handbook/Employee-Handbook-for-Nonprofits-and-Small-Businesses.pdf
```
```bash
python pipelines/postgres/sync_employees.py --source csv --csv data/sample_employees.csv
```
```bash
cd api && PYTHONPATH=.. uvicorn app.main:app --reload
```

The pipeline scripts read the same `PG_*`, `PII_ENCRYPTION_KEY` and `AZURE_OPENAI_ENDPOINT` environment variables.
Expected results with the sample data, as of 2026-10-03:

| ID | Start | Type | Available | Why |
|---|---|---|---|---|
| E1001 | 2014-03-17 | Full-time | 40.00 days | Year 11+, at the 2 × 20-day cap |
| E1002 | 2019-11-04 | Full-time | 30.00 days | Years 6–10, at the 2 × 15-day cap |
| E1003 | 2023-06-12 | Full-time | 20.00 days | Years 1–5, at the 2 × 10-day cap |
| E1004 | 2026-08-03 | Full-time | 0.00 days | 1.54 days accrued, still in the 90-day waiting period |
| E1005 | 2022-01-10 | Part-time | 0.00 days | Not eligible (regular full-time only) |
| E1006 | 2026-11-02 | Full-time | 0.00 days | Start date is in the future |
| E9999 | — | — | — | `404 Employee not found` |

## Tests

```bash
pip install pytest httpx && pytest -q api/tests pipelines/postgres/test_pipelines.py
```

The tests cover the accrual math (tiers, fiscal-year maximum, cap, waiting period,
eligibility), PII encryption, masking and key rotation, and the not-found guardrail (no
retrieval or LLM call). They also check that malformed IDs never reach the database, that
ungrounded LLM text falls back to the template, that the LLM receives no PII, all auth
modes, and chunking of the real handbook PDF.

## Next steps

- Private networking: VNet-integrated PostgreSQL (private endpoint), and AWS PrivateLink
  for Databricks reached over a site-to-site VPN between Azure and AWS instead of the
  public endpoint with an IP allow-list.
- Subtract leave taken (from the HR system) once that data is available; the calculation
  already returns accrued vs. available separately.
- Track employment-classification changes so eligibility starts on the date someone became
  regular full-time, as the handbook specifies.
- Rate limiting on `/pto`, and an audit log of who looked up which employee.
