CREATE TABLE blob_cleanup (
    key TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX blob_cleanup_created_at_idx ON blob_cleanup (created_at);
CREATE INDEX attachments_blob_uri_idx ON attachments ((proto_jsonb->>'uri'));
