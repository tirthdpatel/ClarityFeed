# ClarityFeed — container and cluster workflows.
# The non-container paths (Render, GitHub Actions) are unchanged; see README.md.
.PHONY: help test images k8s-secrets k8s-load k8s-up k8s-down k8s-status \
        compose-up compose-down compose-collect demo-scale demo-heal demo-rollout

NS   := clarity
TAG  ?= latest
PY   := .venv/bin/python

help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

test: ## Run the test suite
	$(PY) -m pytest tests/ -q

# --- docker compose ---------------------------------------------------------
compose-up: ## Build and start the local stack
	docker compose up --build -d
	@echo "api: http://localhost:8000/health"

compose-collect: ## Run one ingestion cycle against the local stack
	docker compose run --rm collector

compose-down: ## Stop the stack and drop volumes
	docker compose down -v

# --- images -----------------------------------------------------------------
images: ## Build all three service images
	docker build -f docker/Dockerfile.api       -t clarity-api:$(TAG)       .
	docker build -f docker/Dockerfile.worker    -t clarity-worker:$(TAG)    .
	docker build -f docker/Dockerfile.collector -t clarity-collector:$(TAG) .

# --- kubernetes -------------------------------------------------------------
k8s-secrets: ## Create the real Secret from your .env (never committed)
	@test -f .env || { echo "no .env — copy .env.example first"; exit 1; }
	@set -a; . ./.env; set +a; \
	PW="$${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}"; \
	kubectl -n $(NS) create secret generic clarity-secrets \
	  --from-literal=DATABASE_URL="$${DATABASE_URL:-postgresql://clarity@clarity-postgres:5432/clarity}" \
	  --from-literal=PGPASSWORD="$$PW" \
	  --from-literal=POSTGRES_PASSWORD="$$PW" \
	  --from-literal=INTERNAL_SECRET="$${INTERNAL_SECRET:-$$($(PY) -c 'import secrets;print(secrets.token_urlsafe(32))')}" \
	  --from-literal=GROQ_API_KEY="$${GROQ_API_KEY:-}" \
	  --from-literal=GEMINI_API_KEY="$${GEMINI_API_KEY:-}" \
	  --from-literal=HF_API_TOKEN="$${HF_API_TOKEN:-}" \
	  --dry-run=client -o yaml | kubectl apply -f -

k8s-load: images ## Build images and side-load them into minikube
	minikube image load clarity-api:$(TAG)
	minikube image load clarity-worker:$(TAG)
	minikube image load clarity-collector:$(TAG)

k8s-up: ## Apply every manifest
	kubectl apply -k k8s/
	kubectl -n $(NS) rollout status deployment/clarity-api --timeout=180s

k8s-down: ## Delete the namespace and everything in it
	kubectl delete namespace $(NS) --ignore-not-found

k8s-status: ## Show what is running
	kubectl -n $(NS) get pods,svc,cronjob,ingress

# --- demos (see docs/kubernetes.md) -----------------------------------------
demo-scale: ## Scale workers 2 -> 8
	kubectl -n $(NS) scale deployment/clarity-worker --replicas=8
	kubectl -n $(NS) get pods -l app=clarity-worker -w

demo-heal: ## Kill an API pod and watch it come back
	kubectl -n $(NS) delete pod -l app=clarity-api --wait=false | head -1
	kubectl -n $(NS) get pods -l app=clarity-api -w

demo-rollout: ## Rolling restart of the API with no downtime
	kubectl -n $(NS) rollout restart deployment/clarity-api
	kubectl -n $(NS) rollout status deployment/clarity-api
