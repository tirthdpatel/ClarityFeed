# Neon PostgreSQL Setup Guide

## Step 1 — Create a Neon Account

1. Go to [neon.tech](https://neon.tech)
2. Click **Sign Up** — no credit card required
3. You can sign up with GitHub, Google, or email

## Step 2 — Create a New Project

1. After signing in, click **New Project**
2. Choose a project name (e.g., `clarityfeed`)
3. Select a region (e.g., `US East (Ohio)` for lowest latency to Render)
4. Click **Create Project**

## Step 3 — Copy the Connection String

1. After project creation, Neon shows the connection string immediately
2. It looks like: `postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb`
3. If not already present, append `?sslmode=require` to the end:
   ```
   postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
4. **Keep this string private** — it contains your database password

## Step 4 — Set the Connection String in Render

1. Go to your Render service dashboard
2. Navigate to **Environment** tab
3. Add a new environment variable:
   - **Key**: `DATABASE_URL`
   - **Value**: the full connection string from Step 3 (with `?sslmode=require`)
4. Click **Save Changes**

## Step 5 — Automatic Table Creation

The `init_db()` call in the FastAPI startup event will create all tables automatically when the Render service starts. No manual SQL is needed.

## Step 6 — View Database Contents

1. Go to [console.neon.tech](https://console.neon.tech)
2. Select your project
3. Click **SQL Editor** in the left sidebar
4. Run queries directly:
   ```sql
   SELECT * FROM sources;
   SELECT COUNT(*) FROM raw_articles;
   ```

## Free Tier Limits

| Resource | Limit |
|---|---|
| Storage | 0.5 GB |
| Compute hours | 191.9 hours/month |
| Branches | 10 |
| Projects | 1 |

The `raw_html` field on `raw_articles` is set to `NULL` after content cleaning to stay within the 0.5 GB limit.
