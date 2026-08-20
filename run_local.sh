#!/bin/bash
# Local development runner. Requires .env file with all environment variables.
# The Neon database works identically in local dev and production.
# Groq and HuggingFace APIs work identically in local dev and production.
echo "Starting local development server..."
echo "Make sure .env file is present with DATABASE_URL, GROQ_API_KEY, HF_API_TOKEN, INTERNAL_SECRET"
uvicorn backend.api.main:app --host 127.0.0.1 --port 8000 --reload
