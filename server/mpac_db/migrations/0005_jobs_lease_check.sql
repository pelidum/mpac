ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_check;

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_lease_check;

ALTER TABLE jobs ADD CONSTRAINT jobs_lease_check CHECK (
    CASE
        WHEN status = 'leased' THEN leased_until IS NOT NULL AND leased_by IS NOT NULL
        ELSE leased_until IS NULL AND leased_by IS NULL
    END
);
