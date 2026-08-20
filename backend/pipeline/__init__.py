"""Pipeline orchestration — the publish barrier lives here.

ARCHITECTURE_V2 §A3. Composition only: every stage below is implemented in
its own module and this package decides the order and what is allowed to
fail.
"""

from backend.pipeline.runner import (
    PipelineConfig,
    PipelineRunner,
    RunReport,
    StageTimings,
)

__all__ = ["PipelineConfig", "PipelineRunner", "RunReport", "StageTimings"]
