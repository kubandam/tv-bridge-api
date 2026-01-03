#!/usr/bin/env bash
# Only set API_KEY if not already set (e.g., by Render)
if [ -z "$API_KEY" ]; then
    export API_KEY=BX5SXQVhiiRQxoSCWWqV2pE6M1nBF6Pg
fi

# Only set DATABASE_URL if not already set (e.g., by Render)
if [ -z "$DATABASE_URL" ]; then
    export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/tvbridge
fi

# Optional: while prototyping without pairing/sessions.
# Clients can still send X-Device-Id explicitly; this is just a server-side fallback.
if [ -z "$DEFAULT_DEVICE_ID" ]; then
    export DEFAULT_DEVICE_ID=tv-1
fi

uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
