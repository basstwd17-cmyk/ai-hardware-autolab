/**
 * Reading a contract proposal without promoting it.
 *
 * `results.py` copies `reasonCode` from the review's own `outcome`. The
 * reviewer defines the strongest of those, `ready_for_human_review`, as: *"no
 * review error or required change is known. It is not approval and it never
 * makes the draft executable."*
 *
 * So this module exists to make the UI say exactly that and nothing stronger.
 * The temptation is real — a green badge saying "ready" reads as "go", and an
 * operator who reads it that way will look for the run button. Every label
 * below is chosen so that reading it quickly still leaves the right
 * impression, and `approved` / `executable` appear nowhere.
 *
 * The other half: a draft is **not a file artifact**. It is a handoff envelope
 * the daemon reduced into the result. There is nothing on disk to reveal, so
 * the "open" affordance opens a panel showing the envelope, its digest, the
 * findings and the required changes — not the file manager.
 */

export interface Finding {
  code?: string
  severity?: string
  detail?: string
}

export interface ContractReview {
  kind?: string
  agent?: string
  outcome?: string
  scenario_sha256?: string
  draft_sha256?: string
  findings?: Finding[]
  required_changes?: unknown[]
}

export interface ContractDraft {
  kind?: string
  agent?: string
  contract_name?: string
  architecture_id?: string
  execution_mode?: string
  objective?: string
  roles?: string[]
  skills?: string[]
  constraints?: string[]
  steps?: unknown[]
  pass_criteria?: string[]
  evidence_claim_ids?: string[]
  scenario_sha256?: string
}

export interface Proposal {
  draft: ContractDraft | null
  draftSha256: string | null
  review: ContractReview | null
  reviewSha256: string | null
  reasonCode: string | null
}

/** A proposal, or null when this result is an ordinary verdict. */
export function proposalOf(result: Record<string, unknown> | undefined): Proposal | null {
  if (!result) return null
  const draft = (result.contractDraft as ContractDraft | undefined) ?? null
  const review = (result.contractReview as ContractReview | undefined) ?? null
  if (!draft && !review) return null
  return {
    draft,
    draftSha256: (result.contractDraftSha256 as string | undefined) ?? null,
    review,
    reviewSha256: (result.contractReviewSha256 as string | undefined) ?? null,
    reasonCode: (result.reasonCode as string | undefined) ?? null,
  }
}

export interface Standing {
  /** What the badge says. Never "approved", never "executable". */
  label: string
  tone: 'open' | 'revise' | 'rejected' | 'unclear'
  /** One sentence under it, saying what the state does *not* mean. */
  caveat: string
}

const STANDINGS: Record<string, Standing> = {
  ready_for_human_review: {
    label: '검토 준비됨',
    tone: 'open',
    caveat:
      'No review error or required change is known. That is not approval, and it does not make ' +
      'the draft executable — a person still has to read and sign it.',
  },
  revise: {
    label: '수정 필요',
    tone: 'revise',
    caveat: 'The reviewer listed changes the draft needs before it can be read for signing.',
  },
  rejected: {
    label: '반려됨',
    tone: 'rejected',
    caveat: 'The reviewer rejected the draft. Nothing here promotes it.',
  },
}

export function standingFor(reasonCode: string | null): Standing {
  if (!reasonCode) {
    return {
      label: '상태 없음',
      tone: 'unclear',
      caveat: 'The result carried no reason code, so nothing is claimed about this draft.',
    }
  }
  return (
    STANDINGS[reasonCode] ?? {
      label: reasonCode,
      tone: 'unclear',
      caveat:
        'This reason code is not one this build knows. It is shown verbatim rather than ' +
        'mapped onto a state it might not mean.',
    }
  )
}

/** Findings grouped by severity, in the order a reader wants them. */
export function findingsBySeverity(review: ContractReview | null): Array<[string, Finding[]]> {
  const order = ['error', 'warning', 'info']
  const groups = new Map<string, Finding[]>()
  for (const finding of review?.findings ?? []) {
    const severity = (finding.severity ?? 'info').toLowerCase()
    groups.set(severity, [...(groups.get(severity) ?? []), finding])
  }
  return [...groups.entries()].sort(
    (a, b) => indexOf(order, a[0]) - indexOf(order, b[0]) || (a[0] < b[0] ? -1 : 1),
  )
}

function indexOf(order: string[], value: string): number {
  const at = order.indexOf(value)
  return at === -1 ? order.length : at
}

export function requiredChanges(review: ContractReview | null): string[] {
  return (review?.required_changes ?? []).map((change) =>
    typeof change === 'string' ? change : JSON.stringify(change),
  )
}
