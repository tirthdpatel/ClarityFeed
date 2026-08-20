# Deployment Guide — Zero to Running

This guide takes you from zero to a fully running ClarityFeed system with **no money spent**. Every service used is permanently free with no credit card required.

---

## Step 1 — Prerequisites (All Free, No Credit Card)

Create accounts at each of these services:

| Service | URL | Purpose |
|---|---|---|
| GitHub | [github.com](https://github.com) | Code hosting, CI/CD |
| Neon | [neon.tech](https://neon.tech) | PostgreSQL database |
| Groq | [console.groq.com](https://console.groq.com) | LLM inference |
| HuggingFace | [huggingface.co](https://huggingface.co) | Embedding API |
| Render | [render.com](https://render.com) | Backend hosting |
| Vercel | [vercel.com](https://vercel.com) | Frontend hosting |

> **Tip:** Sign up for Render and Vercel using your GitHub account for seamless repo integration.

---

## Step 2 — Repository Setup

1. Fork or create the ClarityFeed GitHub repository
2. Clone it locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ClarityFeed.git
   cd ClarityFeed
   ```
3. Run the scaffold (if not already done):
   ```bash
   bash scaffold.sh
   ```
4. Commit and push:
   ```bash
   git add .
   git commit -m "Initial project scaffold"
   git push origin main
   ```

---

## Step 3 — Neon Database Setup

See [neon_setup_guide.md](neon_setup_guide.md) for detailed instructions.

1. Create a project at [console.neon.tech](https://console.neon.tech)
2. Copy the connection string
3. Ensure it ends with `?sslmode=require`

---

## Step 4 — Get API Keys

### Groq API Key
1. Go to [console.groq.com](https://console.groq.com)
2. Navigate to **API Keys**
3. Click **Create API Key**
4. Copy the key (starts with `gsk_`)

### HuggingFace Token
1. Go to [huggingface.co](https://huggingface.co)
2. Navigate to **Settings → Access Tokens**
3. Click **New token** with **Read** role
4. Copy the token (starts with `hf_`)

---

## Step 5 — Deploy Backend to Render

1. Go to [dashboard.render.com](https://dashboard.render.com)
2. Click **New → Web Service**
3. Connect your GitHub repository
4. Configure:
   - **Name**: `news-aggregator-backend` (or any name)
   - **Region**: choose closest to your Neon region
   - **Branch**: `main`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn backend.api.main:app --host 0.0.0.0 --port $PORT`
5. Set environment variables in the **Environment** tab:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | Your Neon connection string |
   | `GROQ_API_KEY` | Your Groq API key |
   | `HF_API_TOKEN` | Your HuggingFace token |
   | `INTERNAL_SECRET` | Any random string (e.g., `openssl rand -hex 32`) |
   | `FRONTEND_URL` | `*` (update after Vercel deploy) |

6. Click **Create Web Service** and wait for the first deploy
7. **Copy the Render URL** (e.g., `https://news-aggregator-backend.onrender.com`)

---

## Step 6 — Set GitHub Actions Secrets

1. Go to your repository on GitHub
2. Navigate to **Settings → Secrets and variables → Actions**
3. Add two secrets:

   | Secret Name | Value |
   |---|---|
   | `RENDER_BACKEND_URL` | The Render URL from Step 5 |
   | `INTERNAL_SECRET` | The same secret set in Render |

---

## Step 7 — Deploy Frontend to Vercel

1. Go to [vercel.com](https://vercel.com)
2. Click **New Project → Import** your GitHub repo
3. Set the **Root Directory** to `frontend`
4. Add environment variable:
   - `VITE_API_BASE_URL` = your Render URL from Step 5
5. Click **Deploy**

---

## Step 8 — Update CORS

1. Go back to the Render dashboard
2. Update the `FRONTEND_URL` environment variable to your Vercel URL
   (e.g., `https://clarityfeed.vercel.app`)

---

## Step 9 — Verify

1. **Health check**: Visit `https://your-render-url.onrender.com/health`
   - Expected: `{"status": "ok", "version": "0.1.0", "platform": "render-free-tier"}`
2. **Sources**: Visit `https://your-render-url.onrender.com/sources`
   - Expected: JSON array of 10 seeded RSS sources
3. **Manual trigger**: Go to GitHub → Actions → RSS Collection Trigger → Run workflow
4. **Check logs**: In Render dashboard, check logs for collection cycle output
5. **Frontend**: Visit your Vercel URL

> **Note:** The first request after Render sleeps takes 30–60 seconds (cold start). This is normal for the free tier.
