/**
 * A job's whole history, fetched a page at a time and never guessed at.
 *
 * `/api/jobs/{id}` answers from the ledger with `{events, cursor, latest}`.
 * The page size is a server bound, so a long-running job returns a *prefix* —
 * and a transcript that silently showed a prefix would be the worst kind of
 * wrong: complete-looking and missing the end.
 *
 * So this hook reads pages until `cursor` reaches `latest`, and it reports
 * three things the UI can act on rather than hiding them:
 *
 * * `complete` — whether the tail was actually reached.
 * * `loaded` / `total` — how far it has got, so a long fetch has a number.
 * * `truncated` — the fetch stopped at the page cap. Rare and bounded, but a
 *   ceiling that is never mentioned is a ceiling nobody believes in.
 *
 * **New rows are appended, not re-fetched.** The ledger only appends, so once
 * a page is read it cannot change. When the cursor moves, the next fetch asks
 * for what is after what this hook already holds — a full re-read of a
 * thousand-row job on every event would make the panel unusable exactly when
 * the job is busiest.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Api, LedgerEvent } from '../api/client'

/** Rows per request. Matches the server's default so paging is visible in dev. */
export const PAGE = 500

/** A ceiling on pages per read, so a runaway job cannot hang the panel. */
export const MAX_PAGES = 40

export interface Transcript {
  events: LedgerEvent[]
  job: Record<string, unknown> | null
  loaded: number
  total: number
  complete: boolean
  truncated: boolean
  loading: boolean
  error: string | null
  reload: () => void
}

const EMPTY: Omit<Transcript, 'reload'> = {
  events: [],
  job: null,
  loaded: 0,
  total: 0,
  complete: false,
  truncated: false,
  loading: false,
  error: null,
}

export function useTranscript(api: Api | null, jobId: string | null, cursor: number): Transcript {
  const [state, setState] = useState(EMPTY)

  // What this hook already holds, so a cursor move fetches the tail rather
  // than the whole job again. Reset whenever the job changes.
  const held = useRef<{ jobId: string | null; events: LedgerEvent[]; upTo: number }>({
    jobId: null,
    events: [],
    upTo: 0,
  })
  // A fetch in flight when the cursor moves again must not have its answer
  // overwritten by the older one; the generation drops superseded reads.
  const generation = useRef(0)
  const live = useRef(true)

  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
    }
  }, [])

  const read = useCallback(
    async (fromScratch: boolean) => {
      if (!api || !jobId) {
        held.current = { jobId: null, events: [], upTo: 0 }
        setState(EMPTY)
        return
      }
      const mine = ++generation.current
      if (fromScratch || held.current.jobId !== jobId) {
        held.current = { jobId, events: [], upTo: 0 }
      }
      setState((current) => ({ ...current, loading: true, error: null }))

      try {
        let since = held.current.upTo
        const gathered = [...held.current.events]
        let job: Record<string, unknown> | null = null
        let latest = 0
        let pages = 0
        let complete = false

        while (pages < MAX_PAGES) {
          const answer = await api.job(jobId, since)
          if (mine !== generation.current || !live.current) return
          job = answer.job
          latest = answer.latest
          pages += 1
          gathered.push(...answer.events)
          since = answer.cursor

          // `cursor` is the last row on this page; `latest` is the ledger tip.
          // Equal means the tail was reached. A page that returned nothing new
          // also means there is nothing more to ask for.
          if (answer.events.length === 0 || answer.cursor >= answer.latest) {
            complete = true
            break
          }
        }

        held.current = { jobId, events: gathered, upTo: since }
        setState({
          events: gathered,
          job,
          loaded: gathered.length,
          total: Math.max(latest, since),
          complete,
          truncated: !complete,
          loading: false,
          error: null,
        })
      } catch (exc) {
        if (mine !== generation.current || !live.current) return
        setState((current) => ({ ...current, loading: false, error: String(exc) }))
      }
    },
    [api, jobId],
  )

  // The ledger moved, or the job changed. Either way, ask for what is after
  // what is already held.
  useEffect(() => {
    void read(false)
  }, [read, cursor])

  const reload = useCallback(() => void read(true), [read])
  return { ...state, reload }
}
