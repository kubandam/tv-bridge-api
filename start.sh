#!/usr/bin/env bash
export API_KEY=BX5SXQVhiiRQxoSCWWqV2pE6M1nBF6Pg
export DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/tvbridge
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
