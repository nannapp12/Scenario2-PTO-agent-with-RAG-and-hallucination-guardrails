-- Databricks on AWS (Unity Catalog): prepare the Employee table for the PTO sync.
-- Run in a SQL warehouse as the catalog owner. Assumes the source table exists as
-- hr_catalog.people.employees; adjust names/columns in the view if yours differ.
--
-- Encryption at rest (AWS side, configured in the Databricks account console):
--   * Customer-managed KMS key for workspace storage (root S3 bucket, EBS volumes) and
--     for managed services (notebooks, query results, secrets).
--   * Unity Catalog managed storage bucket with SSE-KMS (bucket default encryption
--     + a bucket policy that denies PutObject without aws:kms).
-- In transit: the warehouse only accepts HTTPS (TLS 1.2+). Restrict who can reach it
-- with the workspace IP access list (see README) or AWS PrivateLink.

-- 1) Column masks: anyone outside hr-pii-readers sees masked names and emails,
--    in notebooks, dashboards and queries alike.
CREATE OR REPLACE FUNCTION hr_catalog.people.mask_name(v STRING)
  RETURN CASE WHEN is_account_group_member('hr-pii-readers') THEN v
              WHEN v IS NULL THEN NULL
              ELSE concat(left(v, 1), '***') END;

CREATE OR REPLACE FUNCTION hr_catalog.people.mask_email(v STRING)
  RETURN CASE WHEN is_account_group_member('hr-pii-readers') THEN v
              WHEN v IS NULL OR instr(v, '@') = 0 THEN NULL
              ELSE concat(left(v, 1), '***@', substring_index(v, '@', -1)) END;

ALTER TABLE hr_catalog.people.employees ALTER COLUMN first_name SET MASK hr_catalog.people.mask_name;
ALTER TABLE hr_catalog.people.employees ALTER COLUMN last_name  SET MASK hr_catalog.people.mask_name;
ALTER TABLE hr_catalog.people.employees ALTER COLUMN email      SET MASK hr_catalog.people.mask_email;

-- 2) Minimal view for the sync: active employees, only the columns PTO needs,
--    employment type normalized to FULL_TIME / PART_TIME / TEMPORARY.
CREATE OR REPLACE VIEW hr_catalog.people.v_employees_pto_sync
COMMENT 'Read by the Azure PTO employee sync. Columns limited to what PTO needs.'
AS
SELECT
  CAST(employee_id AS STRING)                    AS employee_id,
  first_name,
  last_name,
  email,
  CAST(start_date AS DATE)                       AS start_date,
  CASE
    WHEN upper(employment_type) IN ('FULL_TIME', 'FULL-TIME', 'FULL TIME', 'FT', 'REGULAR FULL-TIME') THEN 'FULL_TIME'
    WHEN upper(employment_type) IN ('PART_TIME', 'PART-TIME', 'PART TIME', 'PT') THEN 'PART_TIME'
    ELSE 'TEMPORARY'
  END                                            AS employment_type,
  updated_at
FROM hr_catalog.people.employees
WHERE employment_status = 'Active';

-- 3) The sync's service principal (OAuth M2M; its secret is in Azure Key Vault) can
--    read the view only, not the base table. It is the one non-HR principal in
--    hr-pii-readers, because it encrypts names/emails before storing them in Azure.
--    Add it to the group in the account console: Groups > hr-pii-readers > Add members.
GRANT USE CATALOG ON CATALOG hr_catalog TO `pto-sync-sp`;
GRANT USE SCHEMA ON SCHEMA hr_catalog.people TO `pto-sync-sp`;
GRANT SELECT ON VIEW hr_catalog.people.v_employees_pto_sync TO `pto-sync-sp`;
-- Plus: CAN USE on the SQL warehouse referenced by DATABRICKS_AWS_HTTP_PATH.
