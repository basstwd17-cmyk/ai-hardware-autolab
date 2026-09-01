import {
  commandKind,
  json,
  readObject,
  RequestError,
  requestError,
  requireOperator,
  text,
} from '../../../_lib/shared.js'

export async function onRequestPost(context) {
  const refused = await requireOperator(context)
  if (refused) return refused
  try {
    const body = await readObject(context.request)
    const commandId = text(body, 'commandId', 100)
    const deviceId = text(body, 'deviceId', 100)
    const kind = commandKind(body)
    const payload = body.payload
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new RequestError('payload must be an object')
    }
    const expiresIn = Math.max(30, Math.min(3600, Number(body.expiresInSeconds) || 600))
    const now = Math.floor(Date.now() / 1000)
    const inserted = await context.env.RELAY_DB.prepare(
      `INSERT OR IGNORE INTO commands
       (command_id, device_id, kind, payload, state, created_at, expires_at)
       VALUES (?, ?, ?, ?, 'pending', ?, ?)`,
    ).bind(commandId, deviceId, kind, JSON.stringify(payload), now, now + expiresIn).run()
    if (Number(inserted.meta?.changes || 0) !== 1) {
      return json({ ok: false, error: 'commandId already exists' }, 409)
    }
    return json({ ok: true, commandId, state: 'pending', expiresAt: now + expiresIn }, 201)
  } catch (error) {
    return requestError(error)
  }
}
