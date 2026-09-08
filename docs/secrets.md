# Secret handling

**Rule: no credential is ever written to a file that git tracks.** Everything
below follows from that.

---

## 1. Where each secret lives

A secret lives in exactly one place per environment. There is no file in this
repository that contains a real credential.

| Secret | Local dev | Ingestion (GitHub Actions) | Read API (Render) | Frontend (Vercel) |
|---|---|---|---|---|
| `DATABASE_URL` | `.env` | Actions secret | Render env var | — |
| `GROQ_API_KEY` | `.env` | Actions secret | — | — |
| `GEMINI_API_KEY` | `.env` | Actions secret | — | — |
| `HF_API_TOKEN` | `.env` | Actions secret | — | — |
| `NEXT_PUBLIC_API_URL` | `.env.local` | — | — | Vercel env var |

`.env` is gitignored. `render.yaml` declares every variable with
`sync: false`, meaning the value is set in the Render dashboard and never
appears in the repo.

**Never give the frontend a database credential.** Anything prefixed
`NEXT_PUBLIC_` is compiled into the JavaScript bundle and is readable by
every visitor. The frontend talks to the read API; only the API talks to
Postgres.

---

## 2. Supabase: which connection string

Supabase offers three, and the difference is load-bearing here.

| Mode | Host | Port | IPv4? | Use for |
|---|---|---|---|---|
| Direct | `db.<ref>.supabase.co` | 5432 | **No** (IPv6 only on free tier) | Local dev on an IPv6 network |
| **Session pooler** | `aws-<region>.pooler.supabase.com` | 5432 | Yes | **Everything in this project** |
| Transaction pooler | `aws-<region>.pooler.supabase.com` | 6543 | Yes | Serverless / edge functions |

**Use the session pooler.** Two independent reasons:

1. **GitHub Actions runners are IPv4-only.** ARCHITECTURE_V2 §1 puts the
   entire ingestion pipeline in Actions. The direct connection cannot be
   resolved from there — it fails at DNS, and the failure looks like a
   generic connection error rather than an address-family problem, so it is
   an unpleasant afternoon to debug.
2. **Alembic needs session mode.** The transaction pooler on 6543 does not
   hold session state and disallows prepared statements, which breaks some
   DDL and some SQLAlchemy behaviour. Session mode on 5432 supports both
   migrations and normal queries.

Note the username differs between modes: the pooler uses
`postgres.<project-ref>`, the direct connection uses plain `postgres`.
Copying the password into the wrong template produces an authentication
error rather than a helpful message.

---

## 3. Rotating a leaked credential

**A credential that has been committed is compromised, even if you delete the
commit.** Git history is immutable in practice — the object survives in
reflogs, on every clone, and in GitHub's API long after a force-push. If the
repository is public, assume automated scrapers found it within minutes.

The only fix is rotation.

| Credential | Rotate at |
|---|---|
| Supabase DB password | Dashboard → Settings → Database → Reset password |
| Supabase service_role key | Dashboard → Settings → API → Rotate |
| Groq | console.groq.com → API Keys → revoke and recreate |
| Gemini | aistudio.google.com → API keys |
| HuggingFace | huggingface.co/settings/tokens |

Rotate first, clean history second. Cleaning history is optional; rotating is
not.

---

## 4. The guardrails in this repo

Three layers, deliberately overlapping, because each one alone is skippable.

**`.gitignore`** covers `.env`, all its variants, key material, and local
backup artefacts. Note that `git add -f` bypasses it entirely, which is why
it is not the only layer.

**Pre-commit hook** (`.githooks/pre-commit`) — install once per clone:

```bash
./scripts/install-hooks.sh
```

It rejects any staged `.env` file and scans staged content for credential
patterns. It lives in `.githooks/` rather than `.git/hooks/` so that it is
version-controlled and survives a fresh clone. It can be skipped with
`git commit --no-verify`.

**CI** (`.github/workflows/secrets.yml`) runs the same scan over all tracked
files *and* the full history on every push and pull request. This is the
layer that cannot be bypassed locally.

Run the scan by hand at any time:

```bash
python scripts/check_secrets.py            # all tracked files
python scripts/check_secrets.py --staged   # what you are about to commit
```

It detects Postgres URLs with inline passwords, Supabase JWTs (anon and
service_role), Groq `gsk_`, HuggingFace `hf_`, Google `AIza`, AWS access
keys, private key blocks, and hardcoded credential assignments.

False positive? Prefer a placeholder value. If the literal is genuinely
needed, append `# noqa: secret` to that line. `tests/unit/test_check_secrets.py`
covers both directions — a scanner that fires on `.env.example` gets disabled
within a week and then protects nothing.

---

## 5. Startup validation

`settings.validate_or_die()` runs on API startup and **refuses to boot** when
`APP_ENV=production` and any of the following hold:

- `DATABASE_URL` is still the SQLite development default
- `FRONTEND_URL` is a wildcard

It raises rather than warns. A warning about an unset secret scrolls past in
a deploy log and the service comes up anyway, exposed; a failed deploy is
louder and much cheaper.

Local development and the test suite are unaffected — the checks only apply
when `APP_ENV` is `production`.

---

## 6. Setting up a new environment

**Local:**

```bash
cp .env.example .env
# paste the Supabase SESSION POOLER string and your keys into .env
./scripts/install-hooks.sh
python scripts/check_secrets.py
```

**GitHub Actions:** Settings → Secrets and variables → Actions. Add
`DATABASE_URL` and `GROQ_API_KEY`. Reference them as
`${{ secrets.NAME }}` — never echo one into a log; Actions masks known secret
values but not strings you have derived from them.

**Render:** dashboard → Environment. Every key in `render.yaml` is declared
`sync: false`, so values are entered there and stay out of the repo. Set
`APP_ENV=production`.
