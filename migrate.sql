-- Run this once after deploying the new code.
-- Safe to run multiple times (uses IF NOT EXISTS / column existence checks are not standard SQL,
-- so just run each ALTER once on your Render PostgreSQL instance).

-- AdResultDB: add channel column
ALTER TABLE ad_results ADD COLUMN IF NOT EXISTS channel VARCHAR;

-- RpiStatusDB: add temperature
ALTER TABLE rpi_status ADD COLUMN IF NOT EXISTS temperature_celsius FLOAT;

-- FrameHistoryDB: CLIP debug info
ALTER TABLE frame_history ADD COLUMN IF NOT EXISTS threshold FLOAT;
ALTER TABLE frame_history ADD COLUMN IF NOT EXISTS p_program FLOAT;
ALTER TABLE frame_history ADD COLUMN IF NOT EXISTS detect_time_ms INTEGER;
ALTER TABLE frame_history ADD COLUMN IF NOT EXISTS top_ad_prompt VARCHAR;
ALTER TABLE frame_history ADD COLUMN IF NOT EXISTS top_nonad_prompt VARCHAR;

-- AdEventDB: new table (also created automatically on startup, but just in case)
CREATE TABLE IF NOT EXISTS ad_events (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    channel VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    duration_seconds FLOAT,
    avg_confidence FLOAT,
    switch_triggered BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_ad_events_device_id ON ad_events(device_id);
CREATE INDEX IF NOT EXISTS ix_ad_events_event_type ON ad_events(event_type);
CREATE INDEX IF NOT EXISTS ix_ad_events_created_at ON ad_events(created_at);
CREATE INDEX IF NOT EXISTS ix_ad_events_channel ON ad_events(channel);
