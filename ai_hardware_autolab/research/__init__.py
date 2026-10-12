"""Read-only source collection for the discovery loop.

The package is deliberately independent from the daemon and web UI.  A later
ResearchProvider may find documents on the web, but every provider must hand
the bytes to :class:`ReferenceStore` before an agent may cite them.
"""

from .references import (
    ExtractionResult,
    PypdfExtractor,
    ReferenceDependencyError,
    ReferenceError,
    ReferenceRecord,
    ReferenceStore,
)
from .models import (
    Citation,
    ResearchPlan,
    ResearchQuestion,
    ResearchValidationError,
    SourceClaim,
    SourceDocument,
    SourcePacket,
    scenario_sha256,
)
from .architecture import (
    ArchitectureDecision,
    ContractDraft,
    ContractReview,
    DesignDecision,
    DraftStep,
    ReviewFinding,
)
from .providers import FetchedDocument, ResearchProvider, SearchResult
from .discovery import DiscoveryError, DiscoveryPipeline, DiscoveryResult
from .native_web import HttpsPdfProvider, NativeWebResearchSession
from .mission import ChangeAnalysis, ChangeFinding, MissionPlan
from .tools import ResearchToolError, ResearchToolPolicy, ResearchTools

__all__ = [
    "PypdfExtractor",
    "ExtractionResult",
    "ReferenceDependencyError",
    "ReferenceError",
    "ReferenceRecord",
    "ReferenceStore",
    "Citation",
    "ArchitectureDecision",
    "ContractDraft",
    "ContractReview",
    "DesignDecision",
    "DraftStep",
    "FetchedDocument",
    "DiscoveryError",
    "DiscoveryPipeline",
    "DiscoveryResult",
    "HttpsPdfProvider",
    "NativeWebResearchSession",
    "MissionPlan",
    "ChangeAnalysis",
    "ChangeFinding",
    "ResearchPlan",
    "ResearchProvider",
    "ResearchQuestion",
    "ResearchToolError",
    "ResearchToolPolicy",
    "ResearchTools",
    "ResearchValidationError",
    "ReviewFinding",
    "SearchResult",
    "SourceClaim",
    "SourceDocument",
    "SourcePacket",
    "scenario_sha256",
]
