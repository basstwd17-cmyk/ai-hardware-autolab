import { json, readObject, RequestError, requestError, requireDevice, text } from '../../../_lib/shared.js'

export async function onRequestPost(context) {
  const refused = await requireDevice(context)
  if (refused) return refused
  try {
    const body = await readObject(context.request)
    const deviceId = text(body, 'deviceId', 100)
    const commandId = text(body, 'commandId', 100)
    if (typeof body.ok !== 'boolean') throw new RequestError('ok must be a boolean')
    const now = Math.floor(Date.now() / 1000)
    const updated = await context.env.RELAY_DB.prepare(
      `UPDATE commands SET state = 'completed', completed_at = ?, ok = ?, result = ?, error = ?
       WHERE command_id = ? AND device_id = ? AND state = 'claimed'`,
    ).bind(
      now,
      body.ok ? 1 : 0,
      body.result === undefined ? null : JSON.stringify(body.result),
      body.error === undefined ? null : String(body.error).slice(0, 4000),
      commandId,
      deviceId,
    ).run()
    if (Number(updated.meta?.changes || 0) !== 1) {
      return json({ ok: false, error: 'command is not claimed by this device' }, 409)
    }
    return json({ ok: true, commandId })
  } catch (error) {
    return requestError(error)
  }
}
