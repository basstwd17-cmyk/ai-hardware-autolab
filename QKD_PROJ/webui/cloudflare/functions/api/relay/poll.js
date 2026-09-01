import { json, readObject, requestError, requireDevice, text } from '../../../_lib/shared.js'

export async function onRequestPost(context) {
  const refused = await requireDevice(context)
  if (refused) return refused
  try {
    const body = await readObject(context.request)
    const deviceId = text(body, 'deviceId', 100)
    const now = Math.floor(Date.now() / 1000)
    await context.env.RELAY_DB.prepare(
      `UPDATE commands SET state = 'expired', completed_at = ?
       WHERE device_id = ? AND state = 'pending' AND expires_at <= ?`,
    ).bind(now, deviceId, now).run()
    const row = await context.env.RELAY_DB.prepare(
      `SELECT command_id, kind, payload FROM commands
       WHERE device_id = ? AND state = 'pending' AND expires_at > ?
       ORDER BY created_at ASC LIMIT 1`,
    ).bind(deviceId, now).first()
    if (!row) return json({ ok: true, command: null })
    const claimed = await context.env.RELAY_DB.prepare(
      `UPDATE commands SET state = 'claimed', claimed_at = ?
       WHERE command_id = ? AND state = 'pending'`,
    ).bind(now, row.command_id).run()
    if (Number(claimed.meta?.changes || 0) !== 1) {
      return json({ ok: true, command: null })
    }
    return json({
      ok: true,
      command: {
        commandId: row.command_id,
        kind: row.kind,
        payload: JSON.parse(row.payload),
      },
    })
  } catch (error) {
    return requestError(error)
  }
}
