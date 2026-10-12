/**
 * The stage the office stands on, drawn from the occupancy projection.
 *
 * The important change from the first cut: this reads `/api/office`, not the
 * graph. The graph holds every agent that ever appeared — drawing from it left
 * a finished job's agents at their desks forever, which looks like work and is
 * not. Occupancy is a separate projection whose only source of "still open" is
 * the control plane.
 *
 * `activity` here is an animation and nothing else. It never says a job
 * succeeded, was approved, or is finished — those are the ledger's answers and
 * they are shown as such elsewhere.
 *
 * The floor is `OfficeCanvas`, which draws the vendored sprite sheets. The
 * projection behind it did not change when the renderer arrived: this file
 * still passes it the same snapshot it passed the placeholder.
 */

import { OfficeCanvas } from '../../office/OfficeCanvas'
import type { Platform } from '../usePlatform'

export function OfficeStage({ platform }: { platform: Platform }) {
  const office = platform.office
  const occupants = office?.occupants ?? []
  const open = office?.openJobs ?? []

  return (
    <div className="office">
      <div className="office-floor">
        <OfficeCanvas snapshot={office} />
      </div>

      <footer className="office-foot">
        <span>
          {open.length} open job{open.length === 1 ? '' : 's'} · {occupants.length} at a desk ·
          cursor {office?.cursor ?? 0}
        </span>
        <span className="muted">
          Occupancy is the control plane&rsquo;s answer; the animation is the runtime&rsquo;s.
          Sprites come from webui/assets, hashed in MANIFEST.json, and from nowhere else.
        </span>
      </footer>
    </div>
  )
}
