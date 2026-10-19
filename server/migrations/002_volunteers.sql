-- Volunteer sign-ups from the public website's "Host a node" form.
--
-- Only what volunteers type in is stored (no IP addresses, no user agents),
-- and approximate coordinates arrive already rounded to 2 decimals (~1 km).
-- Emails are stored lower-cased; a repeat submission updates the row.

CREATE TABLE IF NOT EXISTS volunteers (
    id            bigserial PRIMARY KEY,
    email         text NOT NULL UNIQUE,
    name          text,
    country       text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
    region        text,
    city          text,
    postal_code   text,
    approx_lat    double precision CHECK (approx_lat BETWEEN -90 AND 90),
    approx_lon    double precision CHECK (approx_lon BETWEEN -180 AND 180),
    site_type     text CHECK (site_type IN ('rooftop', 'yard', 'rural_land', 'other')),
    has_hardware  text CHECK (has_hardware IN ('none', 'some', 'full')),
    interests     text[] NOT NULL DEFAULT '{}',
    notes         text,
    consent       boolean NOT NULL CHECK (consent),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
