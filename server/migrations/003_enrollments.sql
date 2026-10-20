-- Claim codes for node enrollment (docs/NODE_APPLIANCE.md "Enrollment").
--
-- A coordinator creates codes (POST /api/v1/enrollments or
-- `skynet-server enroll-codes`); a node exchanges one for its identity with
-- POST /api/v1/enroll. Codes are single use: used_ns and node_id record which
-- node a code produced. No IP addresses are stored. `hardware` is what the
-- node reported about itself (model, camera, GPS kind, software version).

CREATE TABLE IF NOT EXISTS enrollment_codes (
    code        text PRIMARY KEY CHECK (code ~ '^[0-9A-HJKMNP-TV-Z]{8}$'),
    label       text,
    created_ns  bigint NOT NULL,
    expires_ns  bigint NOT NULL,
    used_ns     bigint,
    node_id     text,
    hardware    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((used_ns IS NULL) = (node_id IS NULL))
);
CREATE INDEX IF NOT EXISTS enrollment_codes_node_id ON enrollment_codes (node_id);
