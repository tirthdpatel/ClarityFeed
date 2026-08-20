"""Rule-based classification — country and category.

Zero API calls by design (ARCHITECTURE_V3 §A1/§A2). The LLM sits behind the
publish barrier for enrichment only, so a vendor deprecation degrades the
product rather than stopping it.
"""

from backend.classifier.category import (
    CategoryClassifier, CategoryResult, CategoryRule, load_rules_from_yaml,
)
from backend.classifier.assigner import AssignmentStats, ClassificationStage
from backend.classifier.country import CountryClassifier, CountryResult
from backend.classifier.gazetteer import Alias, Gazetteer, load_from_db, normalize

__all__ = [
    "AssignmentStats", "ClassificationStage",
    "Alias", "Gazetteer", "load_from_db", "normalize",
    "CountryClassifier", "CountryResult",
    "CategoryClassifier", "CategoryResult", "CategoryRule", "load_rules_from_yaml",
]
