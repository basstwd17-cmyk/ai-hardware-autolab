import { json, readObject, RequestError, requestError, requireDevice, text } from '../../../_lib/shared.js'

export async function onRequestPost(context) {
  const refused = await requireDevice(context)
  if (refused) return refused
  try {
    const body = await readObject(context.request)
    const deviceId = text(body, 'deviceId', 100)
    const snapshot = body.snapshot
    if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
      throw new RequestError('snapshot must be an object')
    }
    const cursor = Number.isSafeInteger(body.cursor) && body.cursor >= 0 ? body.cursor : 0
    const now = Math.floor(Date.now() / 1000)
    await context.env.RELAY_DB.prepare(
      `INSERT INTO snapshots (device_id, payload, cursor, updated_at)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(device_id) DO UPDATE SET
         payload = excluded.payload,
         cursor = excluded.cursor,
         updated_at = excluded.updated_at`,
    ).bind(deviceId, JSON.stringify(snapshot), cursor, now).run()
    return json({ ok: true, updatedAt: now })
  } catch (error) {
    return requestError(error)
  }
}
