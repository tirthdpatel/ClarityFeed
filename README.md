# ClarityFeed
Open-Source AI-Powered International News Aggregation and Distillation System

## Deployment paths

ClarityFeed has two working deployment targets. Both are real; they exist for
different reasons.

**1. Render + Neon + GitHub Actions — the production path.**
Chosen under a zero-cost, zero-device constraint and documented in
[docs/technology_decisions.md](docs/technology_decisions.md). Ingestion runs as a
scheduled Actions workflow, the read API runs on Render, Postgres is Neon. No
containers involved.

**2. Docker + Kubernetes — the containerised path.**
The same code, orchestrated as a cluster: the API as a replicated Deployment,
ingestion as a CronJob, and below-the-barrier enrichment as a pool of workers
that scales with the backlog. See **[docs/kubernetes.md](docs/kubernetes.md)**.

The second does not replace the first. A cluster has to run somewhere and
somewhere is not free, which is exactly the constraint that ruled it out for
production. It exists because the enrichment stage genuinely benefits from
independent scaling, and because the architecture already had the right seam for
it — `raw_articles.enrichment_status = PENDING` was already a queue.

### Quick start

No container runtime required. `DATABASE_URL` points at Supabase, the same
database production uses, so there is no local stack to bring up.

```bash
cp .env.example .env               # then paste your DATABASE_URL in
python -m venv .venv && .venv/bin/pip install -r requirements.txt
make api                           # read API on :8000
make ingest                        # one ingestion cycle
make worker                        # one enrichment worker
```

```bash
minikube start && minikube addons enable ingress
make k8s-secrets && make k8s-load && make k8s-up
make k8s-status
```

`make help` lists everything, including the four demos in
[docs/kubernetes.md](docs/kubernetes.md): replica load balancing, self-healing,
worker scaling, and zero-downtime rolling updates.

### Tests

```bash
make test
```
