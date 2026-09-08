# Kubernetes and Docker — ClarityFeed

Read this before an interview about this project. It covers why the cluster
exists at all, what each object is for, the four things you can demonstrate
live, and what was deliberately left out.

## Start here: why this is not the production deployment

ClarityFeed runs on Render (API) + Neon (Postgres) + GitHub Actions (ingestion).
That stack was chosen deliberately and is documented in
[technology_decisions.md](technology_decisions.md) under a hard constraint:
**zero cost, zero devices.** Render needs no credit card, Neon's free tier is
permanent, and Actions provides free cron. One of the stated reasons for
choosing Render was that it needs *no Docker at all*.

Kubernetes contradicts that constraint. A cluster has to run somewhere, and
somewhere is not free.

So this is a **second, parallel deployment target**, not a replacement. Both are
real and both work. Being able to say *"here is why production does not use
Kubernetes, and here is the version that does"* is a stronger answer than
either one alone — it shows the tool was chosen against a requirement rather
than reached for by default.

## Why a cluster helps this particular workload

The argument comes from the architecture that already existed, not from a wish
to use Kubernetes.

`backend/pipeline/runner.py` implements a **publish barrier**. Above it,
everything is deterministic, local and free: fetch, clean, classify, embed. The
moment an article crosses the barrier it is `PUBLISHED` and readers can see it.
Below the barrier is enrichment — LLM summarisation — which is slow,
rate-limited, and dependent on somebody else's uptime. The barrier exists
because an LLM outage once emptied the site.

That split has different scaling needs on each side, and the runner's own
docstring names the consequence:

> "Anything not otherwise resolved stays PENDING and is picked up by a later
> run. **PENDING is a queue, not an error.**"

It was already a queue. Kubernetes gives it consumers:

```
 GitHub Actions cron  ──or──  Kubernetes CronJob
                    │
                    ▼
        scripts/ingest.py --skip-enrichment
        fetch → clean → classify → embed
                    │
        ═══════ PUBLISH BARRIER ═══════   articles are visible from here on
                    │
                    ▼
        raw_articles.enrichment_status = PENDING     ← the queue
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
    worker 1    worker 2    worker N      ← scales independently
        │           │           │
        └───────────┼───────────┘
                    ▼
              LLM summary  →  enrichment_status = DONE
```

Ingestion stays one scheduled batch. Enrichment becomes a pool that scales with
the backlog. Neither can starve the other, and enrichment failing still cannot
un-publish an article.

## Why Postgres `SKIP LOCKED` and not Redis

The obvious move is Redis in the middle. It was not done, and this is the
decision most worth being able to defend.

`raw_articles.enrichment_status` is already the queue *and* already the source
of truth. Adding Redis would create a second place that can disagree with it,
and the disagreement shows up as either double-summarised articles — real money,
against a 200-call/day budget — or silently dropped ones.

`SELECT ... FOR UPDATE SKIP LOCKED` is precisely the primitive a work queue
needs, and Postgres has had it since 9.5:

```sql
SELECT raw_articles.id, source_id, cleaned_articles.id
FROM raw_articles JOIN cleaned_articles ON ...
WHERE enrichment_status = 'PENDING' AND ingest_status = 'PUBLISHED'
ORDER BY raw_articles.id
LIMIT 5
FOR UPDATE OF raw_articles SKIP LOCKED;
```

Each worker claims rows no other worker can even see. Crash recovery is free:
a worker killed mid-batch never commits, its locks die with the connection, and
the rows are `PENDING` again — which is the recovery path the runner already
documented. A Redis list would need acknowledgements and a reaper to match that.

**The honest limit:** every worker polls the same table, so this stops scaling
when one Postgres can no longer serve the claim query. That is thousands of
workers, not the eight this will ever run. Redis or a real broker is the answer
*then*, not now.

## What is deployed

| Object | Kind | Replicas | Why |
|---|---|---|---|
| `clarity-postgres` | StatefulSet | 1 | Stable identity + its own PVC |
| `clarity-api` | Deployment | 3 | Stateless reads, load balanced |
| `clarity-collector` | **CronJob** | — | A scheduled batch, not a service |
| `clarity-worker` | Deployment | 2 → N | The scaling story |
| `clarity` | Ingress | — | Single entry point |
| `clarity-worker` | ScaledObject | — | KEDA, scales workers on queue depth |

Plus a ConfigMap, a Secret, and a PodDisruptionBudget on the API.

### Why the collector is a CronJob and the workers are a Deployment

This contrast is the thing to invite a question about.

The collector **has no work between ticks**. Modelling it as a Deployment would
mean writing a sleep loop — reimplementing cron, badly, inside the app.
`concurrencyPolicy: Forbid` is the load-bearing line: two overlapping runs would
fetch every feed twice and race on the `url_hash` unique constraint, so a run
that overshoots its interval must skip the next tick rather than pile up.

The workers **always have work or are waiting for it**, and the amount is
unbounded and bursty. That is a Deployment, and it is the one thing here that
genuinely benefits from replicas.

### Why liveness and readiness check different things

```yaml
livenessProbe:  /health   # process only — touches nothing external
readinessProbe: /ready    # executes SELECT 1
```

If liveness checked the database, a Neon outage would fail the probe on all
three API pods and Kubernetes would respond by restarting all three — turning
one dependency outage into a crash loop while fixing nothing. Restarting a pod
cannot repair someone else's database.

Readiness failing removes a pod from the Service's endpoints *without* killing
it. Traffic goes to healthy replicas; the pod rejoins by itself when the
database returns.

The workers expose liveness only, and on purpose: a worker idling because Groq
is down is **healthy**. It is doing exactly what the barrier designed it to do.

## The four demos

### 1. Replicas and load balancing

```bash
kubectl -n clarity get pods -l app=clarity-api
curl http://clarity.local/health   # run it a few times
```

`instance` in the response is the pod name, injected by the downward API. Watch
it change across calls — that is the Service load balancing, visible from
outside the cluster.

### 2. Self-healing

```bash
kubectl -n clarity delete pod <an-api-pod>
kubectl -n clarity get pods -l app=clarity-api -w
```

The ReplicaSet sees 2 where it wants 3 and replaces it in seconds. With
`minAvailable: 2` in the PDB and readiness gating traffic, **the API never goes
down** — keep curling `/health` throughout and nothing fails. That is the point,
and it is worth saying out loud, because "nothing happened" looks like nothing
happened.

### 3. Horizontal scaling — the one that justifies the architecture

First you need a backlog, and a real database may not have one — see
*"Why the queue is usually empty"* below. Seed a synthetic one:

```bash
kubectl -n clarity exec deploy/clarity-api -- \
    python scripts/seed_demo_backlog.py --count 200
```

Then watch it drain, first with 2 workers and then with 8:

```bash
watch -n2 'curl -s http://clarity.local/stats/pipeline | jq .pending_enrichment'
kubectl -n clarity scale deployment/clarity-worker --replicas=8
```

Clean up afterwards with `--clean`.

**Be ready for the follow-up.** More workers drain a *burst* faster; they do not
raise the daily ceiling. `LLM_DAILY_CALL_BUDGET` is 200 calls/day against a
provider limit of 1,000. Sixteen workers summarise no more per day than four.
Knowing where the real bottleneck is — the quota, not the compute — is the
difference between understanding the system and reciting it.

### 4. Rolling updates

```bash
kubectl -n clarity rollout restart deployment/clarity-api
kubectl -n clarity rollout status deployment/clarity-api
```

`maxUnavailable: 0, maxSurge: 1` means a new pod must pass readiness before an
old one retires, so capacity never dips below three. Curl `/health` in another
terminal during the rollout: no errors.

If a new version is broken it never passes readiness, the rollout stalls instead
of completing, and `kubectl rollout undo` reverts it.

For the workers the interesting part is shutdown: they trap `SIGTERM`, finish
the batch they hold a lock on, and exit within
`terminationGracePeriodSeconds: 45`. A hard kill is survivable — the claim dies
with the transaction — but draining avoids re-spending LLM calls already
deducted from the budget.

## Autoscaling on queue depth

`k8s/40-keda-scaledobject.yaml` scales the workers on the backlog itself:

```sql
SELECT count(*) FROM raw_articles
WHERE enrichment_status = 'PENDING' AND ingest_status = 'PUBLISHED'
```

**Why not a CPU HPA.** It is the default answer and it is wrong here. These
workers spend nearly all their wall time blocked on an HTTP call to Groq, so
CPU stays near idle while the backlog grows without bound — a CPU HPA would sit
at one replica through exactly the burst it was installed to absorb.

Kubernetes cannot scale on a SQL count natively; it is not a resource metric.
KEDA supplies it as an external metric and drives an ordinary HPA underneath,
which is why this is a `ScaledObject` and not an `HorizontalPodAutoscaler`.

`targetQueryValue: 20` is articles *per worker*, not a total: a backlog of 80
asks for 4 replicas. Scale-up is deliberately faster than scale-down (30s
stabilisation up, 300s down) because ingestion delivers work in 15-minute
bursts and scaling in mid-batch abandons a claim that must then be re-made.

It does not scale to zero. A resting worker costs ~100m CPU, and cold-starting
the pool would add a minute of latency to the first article after every quiet
period.

Requires KEDA in the cluster:

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace
```

Without it the manifest applies as an unrecognised resource and does nothing;
the workers stay at their Deployment replica count and `kubectl scale` still
works by hand.

**The ceiling is still the quota.** `LLM_DAILY_CALL_BUDGET` is 200 calls/day
against a provider limit of 1,000. Eight workers clear a burst faster than two;
they do not summarise more per day. `maxReplicaCount: 8` reflects that — scaling
past it buys nothing.

## Why the queue is usually empty

Worth knowing before you demo, because it looks like a bug and is not.

On 2026-09-07 the live database held **9,069 articles, every one of them
`enrichment_status = SKIPPED`, with zero PENDING**, and `cleaned_articles` was
empty. The chain is:

```
no rows in source_permissions
    -> Permissions.restrictive() for every source
    -> can_store_full_text = False
    -> no body is ever fetched
    -> nothing is cleaned
    -> nothing is summarisable
    -> every article is SKIPPED
```

That is `backend/permissions.py` working exactly as designed: an unreviewed
publisher does not get its full text stored. The enrichment stage is dormant
until a source is reviewed and granted `can_store_full_text`, which is a
licensing decision made per source, by a person.

This is why `scripts/seed_demo_backlog.py` creates its own fixture source with
synthetic lorem-ipsum bodies rather than granting real publishers full-text
permission. Real load, no real content, and the compliance posture is untouched.
The script refuses to run when `APP_ENV=production`.

## Images

Three, and they are deliberately not the same size:

| Image | Installs | Roughly | Why |
|---|---|---|---|
| `clarity-api` | `requirements.txt` | ~200 MB | Never embeds anything |
| `clarity-worker` | `requirements.txt` | ~200 MB | Calls an LLM over HTTP |
| `clarity-collector` | `requirements-ingest.txt` | ~2.5 GB | MiniLM + torch |

That split is not a container optimisation invented here — it already exists in
this repo, documented at the top of `requirements-ingest.txt`, because torch is
~2 GB and would OOM Render's 512 MB tier. The Dockerfiles honour it, including
the CPU-only wheel index that file insists on:

```dockerfile
RUN pip install --no-cache-dir -r requirements-ingest.txt \
        --extra-index-url https://download.pytorch.org/whl/cpu
```

The default torch wheel bundles CUDA and is about five times the size, on nodes
with no GPU.

All three run as UID 10001 with `readOnlyRootFilesystem: true` and all
capabilities dropped.

## Running it

```bash
# local, containerised
cp .env.example .env
docker compose up --build
docker compose run --rm collector          # one ingestion cycle
docker compose up -d --scale worker=4      # fan out

# kubernetes
minikube start && minikube addons enable ingress
make k8s-secrets    # from your .env — never commit real values
make k8s-load       # build the three images, side-load into minikube
make k8s-up
```

## Where the database password lives

`DATABASE_URL` carries the host, user and database name but **no password**:

```
postgresql://clarity@clarity-postgres:5432/clarity
```

libpq — which psycopg2 wraps — reads `PGPASSWORD` from the environment when the
connection string has none, so the credential travels as its own Secret key. A
connection string that shows up in a log line, a `kubectl describe`, or a stack
trace is then not a leaked credential.

Every value in `k8s/02-secret.yaml` is empty on purpose. `make k8s-secrets`
builds the real Secret from your gitignored `.env`. Applying the template as-is
gives pods that start and fail readiness — the correct failure, rather than a
silent fallback to a password published in the repository.

## One config change that was needed

`backend/database/session.py` hardcoded `sslmode: require` for every non-SQLite
URL. That is correct for Neon and **fatal** against in-cluster Postgres, which
serves no TLS and rejects the connection rather than downgrading. It is now
`DB_SSLMODE`, still defaulting to `require`, with the k8s ConfigMap setting
`disable`. Traffic there never leaves the cluster network.

Nothing else in the application changed to run on Kubernetes.

## Deliberately not built

Being able to say what you left out, and why, is worth as much as what you built.

- **Prometheus and Grafana.** The worker's `/metrics` returns JSON, not
  exposition format. Nothing currently needs it — KEDA's PostgreSQL scaler
  queries the database directly rather than going through a metrics pipeline —
  so this is for dashboards, not autoscaling.
- **Redis.** Covered above. Not needed until one Postgres cannot serve the claim
  query.
- **Postgres HA.** One replica with a PVC. Real HA is an operator's job, and
  production uses Neon regardless.
- **NetworkPolicies.** Nothing restricts pod-to-pod traffic. On a shared cluster
  Postgres should accept connections only from this namespace.
- **Prometheus.** The worker's `/metrics` returns JSON, not exposition format.
  Converting it is small, and is the prerequisite for the queue-depth HPA above.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ErrImagePull` | Images exist only in the local Docker daemon | `make k8s-load` |
| API pods `0/1 Running` | Readiness failing on Postgres | `kubectl -n clarity logs -l app=clarity-api` |
| `sslmode` / SSL connection errors | `DB_SSLMODE` unset against in-cluster PG | ConfigMap must set `disable` |
| Pod `CreateContainerConfigError` | Secret missing | `make k8s-secrets` |
| API refuses to start, "insecure production configuration" | SQLite `DATABASE_URL` or wildcard `FRONTEND_URL` with `APP_ENV=production` | Working as designed — set them properly |
| Collector OOMKilled | torch + MiniLM exceed the limit | Raise the memory limit above 2 Gi |
| Nothing gets enriched | No LLM key, or nothing `PENDING` | Check worker logs and the status counts |

Note that editing a ConfigMap does **not** restart pods that read it via
`envFrom` — environment variables are injected at container start. A
`kubectl rollout restart` is required.
