"""Below-the-barrier enrichment, as a scalable pool of workers.

See backend/enrichment/worker.py for the design note.
"""

from backend.enrichment.worker import EnrichmentWorker, WorkerStats, claim_pending

__all__ = ["EnrichmentWorker", "WorkerStats", "claim_pending"]
