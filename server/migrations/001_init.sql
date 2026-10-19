-- SKY-NET initial schema (PostGIS).
--
-- Positions are geography(PointZ, 4326): WGS84 lon/lat in degrees plus the
-- height above the ellipsoid in metres, the same reference the API and the
-- dashboard use. Spatial queries (e.g. "triangulations within 50 km") use the
-- GiST indexes; time-range queries use the t_ns indexes.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS nodes (
    node_id          text PRIMARY KEY,
    label            text,
    registered_ns    bigint,
    last_seen_ns     bigint,
    position         geography(PointZ, 4326),
    lat              double precision,
    lon              double precision,
    alt_m            double precision,
    yaw_deg          double precision,
    pitch_deg        double precision,
    roll_deg         double precision,
    fov_h_deg        double precision,
    fov_v_deg        double precision,
    software_version text,
    status           jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS nodes_position_gix ON nodes USING gist (position);

CREATE TABLE IF NOT EXISTS tracks (
    track_id        text PRIMARY KEY,
    first_ns        bigint NOT NULL,
    last_ns         bigint NOT NULL,
    node_ids        text[] NOT NULL,
    point_count     integer NOT NULL,
    speed_mps       double precision,
    max_speed_mps   double precision NOT NULL DEFAULT 0,
    max_accel_mps2  double precision NOT NULL DEFAULT 0,
    classification  text NOT NULL DEFAULT 'unknown',
    flags           text[] NOT NULL DEFAULT '{}',
    last_point      jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tracks_last_ns_idx ON tracks (last_ns DESC);
CREATE INDEX IF NOT EXISTS tracks_classification_idx ON tracks (classification, last_ns DESC);

CREATE TABLE IF NOT EXISTS triangulations (
    event_id        text PRIMARY KEY,
    track_id        text,
    t_ns            bigint NOT NULL,
    position        geography(PointZ, 4326) NOT NULL,
    lat             double precision NOT NULL,
    lon             double precision NOT NULL,
    alt_m           double precision NOT NULL,
    sigma_m         double precision NOT NULL,
    error_margin_m  double precision NOT NULL,
    parallax_deg    double precision NOT NULL,
    node_ids        text[] NOT NULL,
    rays            jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS triangulations_position_gix ON triangulations USING gist (position);
CREATE INDEX IF NOT EXISTS triangulations_track_t_idx ON triangulations (track_id, t_ns);
CREATE INDEX IF NOT EXISTS triangulations_t_idx ON triangulations (t_ns);

CREATE TABLE IF NOT EXISTS clips (
    request_id    text PRIMARY KEY,
    event_id      text NOT NULL,
    track_id      text NOT NULL,
    node_id       text NOT NULL,
    status        text NOT NULL,
    t_start_ns    bigint NOT NULL,
    t_end_ns      bigint NOT NULL,
    issued_ns     bigint NOT NULL,
    bytes         bigint,
    sha256        text,
    content_type  text,
    frames        integer,
    storage_key   text,
    uploaded_ns   bigint,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS clips_track_idx ON clips (track_id);

-- Raw line-of-sight detections. Off by default (SKYNET_STORE_DETECTIONS):
-- at 10k msg/s this is ~860 M rows per day.
CREATE TABLE IF NOT EXISTS detections (
    node_id   text NOT NULL,
    t_ns      bigint NOT NULL,
    seq       bigint NOT NULL,
    track_id  text,
    lat       double precision NOT NULL,
    lon       double precision NOT NULL,
    alt_m     double precision NOT NULL,
    az_deg    double precision NOT NULL,
    el_deg    double precision NOT NULL,
    PRIMARY KEY (node_id, t_ns, seq)
);
