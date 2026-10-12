/**
 * What this page is attached to, and what it is allowed to say about itself.
 *
 * Model preferences apply to new jobs. Diagnostics describe this connection.
 *
 * The token is shown as present or absent, never printed. It authorises
 * `approval.decide`; a screenshot of this tab should not be a credential.
 */

import type { Platform } from '../usePlatform'
import { ModelPicker } from '../ModelPicker'
import { useModelSelection } from '../useModelSelection'

export function SettingsView({ platform, token }: { platform: Platform; token: string | null }) {
  const { daemon, liveness, resyncs, dropped, detail } = platform.connection
  const state = platform.state
  const models = useModelSelection()

  return (
    <div className="pad prose">
      <ModelPicker api={platform.api} selection={models.selection} onChange={models.change} saveError={models.saveError} />
      <h2>This session</h2>
      <dl className="meta wide">
        <Row label="daemon" value={daemon?.address ?? '—'} />
        <Row label="daemon connected" value={String(daemon?.connected ?? false)} />
        <Row label="applied cursor" value={String(daemon?.cursor ?? 0)} />
        <Row label="frames received" value={String(daemon?.frames ?? 0)} />
        <Row
          label="cursor gaps"
          value={`${daemon?.gaps ?? 0} — each one resubscribed rather than interpolated`}
        />
        <Row label="last daemon error" value={daemon?.last_error ?? 'none'} />
        <Row label="socket" value={`${liveness}${detail ? ` — ${detail}` : ''}`} />
        <Row
          label="resyncs"
          value={
            resyncs === 0
              ? 'none — this page has never been behind'
              : `${resyncs}, ${dropped} frame(s) dropped and re-read`
          }
        />
        <Row label="graph cursor" value={String(platform.graph?.cursor ?? 0)} />
        <Row label="graph generated" value={platform.graph?.generatedAt ?? '—'} />
        <Row label="events held" value={`from ${state?.cursor ?? 0}`} />
        <Row
          label="session token"
          value={token ? 'held for this tab (sessionStorage)' : 'missing — reopen the printed URL'}
        />
      </dl>

      <h2>Why this page needs a token at all</h2>
      <p>
        <code>127.0.0.1</code> is not private from a browser&rsquo;s point of view, and the door at
        the end of this one is <code>approval.decide</code>. A cross-origin <code>POST</code> runs
        before CORS refuses to show the reply; a WebSocket is not subject to CORS at all; and DNS
        rebinding makes an attacker&rsquo;s page genuinely same-origin. The backend answers those
        with three separate checks — a bearer token, an <code>Origin</code> allowlist, and a{' '}
        <code>Host</code> allowlist — and refuses to bind anywhere but loopback.
      </p>
      <p className="muted">
        The token is generated per run and dies with the server process. Restarting it logs this tab
        out, which is the intended lifetime.
      </p>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}
