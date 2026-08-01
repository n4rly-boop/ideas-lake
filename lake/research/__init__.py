"""Reusable deep-research service owned by Ideas Lake.

The package deliberately stops at a language research report with optional
source provenance. It does not create task-local GigaEvo ideas, decide
feasibility, or write evaluator evidence; callers decide how (or whether) to
consume the report.
"""

from .agent import DeepResearchAgent, ResearchError, build_default_agent
from .models import ResearchIngest, ResearchRequest, ResearchResponse, ResearchSource
from .web import SelfHostedResearchClient

__all__ = [
    "DeepResearchAgent",
    "ResearchError",
    "ResearchIngest",
    "ResearchRequest",
    "ResearchResponse",
    "ResearchSource",
    "SelfHostedResearchClient",
    "build_default_agent",
]
