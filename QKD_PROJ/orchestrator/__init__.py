"""Layer 2 -- Orchestrator.

Ordinary Python that decides what runs next. No AI, and no hardware access of
its own: every board operation goes through a layer 3 tool, which enforces the
contract through its own SafetyGate. Moving a check up here would make it
bypassable by a direct tool call, so nothing safety-bearing lives in this
package.

    bundle.py     ApprovalBundle -- one human signature covering one session
    ledger.py     ConsumptionLedger -- how much of that signature is already spent
    preflight.py  every refusal that can be made before the first command
    sequence.py   Gate1Sequence -- the scan -> program -> measure state machine
"""

from .bundle import (
    ApprovalBundle,
    BundleError,
    BundleStep,
    SignatureVerifier,
    canonical_payload,
    check_against_contract,
    load_bundle,
    verify_artifact,
)
from .ledger import (
    ConsumptionLedger,
    LedgerError,
    Reservation,
    migrate_json_ledger,
)
from .preflight import (
    ADVISORY,
    BLOCKING,
    PreflightCheck,
    PreflightReport,
    preflight,
)
from .sequence import (
    Gate1Sequence,
    Gate1State,
    SAFE_OFF_ACTIONS,
    SequenceError,
    StepTools,
    resume_sequence,
)

__all__ = [
    "ADVISORY",
    "ApprovalBundle",
    "BLOCKING",
    "BundleError",
    "BundleStep",
    "ConsumptionLedger",
    "Gate1Sequence",
    "Gate1State",
    "LedgerError",
    "PreflightCheck",
    "PreflightReport",
    "Reservation",
    "SAFE_OFF_ACTIONS",
    "SequenceError",
    "SignatureVerifier",
    "StepTools",
    "canonical_payload",
    "check_against_contract",
    "load_bundle",
    "migrate_json_ledger",
    "preflight",
    "resume_sequence",
    "verify_artifact",
]
