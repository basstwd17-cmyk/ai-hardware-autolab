import { json, requireOperator } from '../../../_lib/shared.js'

export async function onRequestGet(context) {
  const refused = await requireOperator(context)
  if (refused) return refused
  const deviceId = new URL(context.request.url).searchParams.get('deviceId')?.trim()
  if (!deviceId) return json({ ok: false, error: 'deviceId is required' }, 422)
  const snapshot = await context.env.RELAY_DB.prepare(
    'SELECT payload, cursor, updated_at FROM snapshots WHERE device_id = ?',
  ).bind(deviceId).first()
  const commands = await context.env.RELAY_DB.prepare(
    `SELECT command_id, kind, payload, state, created_at, claimed_at, completed_at,
            ok, result, error
       FROM commands WHERE device_id = ? ORDER BY created_at DESC LIMIT 30`,
  ).bind(deviceId).all()
  return json({
    ok: true,
    deviceId,
    online: snapshot ? Math.floor(Date.now() / 1000) - snapshot.updated_at < 15 : false,
    snapshot: snapshot ? JSON.parse(snapshot.payload) : null,
    cursor: snapshot?.cursor ?? 0,
    updatedAt: snapshot?.updated_at ?? null,
    commands: (commands.results || []).map((row) => ({
      commandId: row.command_id,
      kind: row.kind,
      payload: JSON.parse(row.payload),
      state: row.state,
      createdAt: row.created_at,
      claimedAt: row.claimed_at,
      completedAt: row.completed_at,
      ok: row.ok === null ? null : row.ok === 1,
      result: row.result ? JSON.parse(row.result) : null,
      error: row.error,
    })),
  })
}
