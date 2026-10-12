import {json, requireDevice, requireOperator, readObject, text, requestError, RequestError} from '../../../_lib/shared.js'

const validId = value => Number.isSafeInteger(value) && value >= 0

export async function onRequestGet(context) {
  const refused = await requireOperator(context)
  if (refused) return refused
  try {
    const params = new URL(context.request.url).searchParams
    const deviceId = text({deviceId:params.get('deviceId')}, 'deviceId', 100)
    const after = Number(params.get('after') || 0)
    const limit = Math.min(Number(params.get('limit') || 100), 100)
    if (!validId(after) || !validId(limit) || limit < 1) throw new RequestError('invalid cursor or limit')
    const db = context.env.RELAY_DB
    const state = await db.prepare('SELECT * FROM knowledge_sync WHERE device_id = ?').bind(deviceId).first()
    if (!state) return json({ok:false, error:'연구실 PC의 지식 동기화를 기다리고 있습니다.'}, 503)
    const rows = await db.prepare('SELECT payload FROM knowledge_events WHERE device_id = ? AND event_id > ? AND event_id <= ? ORDER BY event_id LIMIT ?')
      .bind(deviceId, after, state.cursor, limit).all()
    const events = (rows.results || []).map(row => JSON.parse(row.payload))
    return json({ok:true, knowledge:{events, cursor:events.at(-1)?.event_id ?? after, latest:state.cursor},
      sync:{latest:state.latest, cursor:state.cursor, updatedAt:state.updated_at,
        online:Math.floor(Date.now()/1000)-state.updated_at < 30}})
  } catch (error) { return requestError(error) }
}

export async function onRequestPost(context) {
  const refused = await requireDevice(context)
  if (refused) return refused
  try {
    const body = await readObject(context.request)
    const deviceId = text(body, 'deviceId', 100)
    const db = context.env.RELAY_DB
    const state = await db.prepare('SELECT cursor, latest FROM knowledge_sync WHERE device_id = ?').bind(deviceId).first()
    const cursor = state?.cursor ?? 0
    if (body.events === undefined) return json({ok:true, cursor})
    if (!Array.isArray(body.events) || body.events.length > 100 || !validId(body.after)
        || !validId(body.latest) || body.latest < cursor || body.after !== cursor)
      throw new RequestError('knowledge cursor mismatch; retry from server cursor')
    let next = cursor
    const writes = []
    for (const event of body.events) {
      if (!event || !validId(event.event_id) || event.event_id <= next || event.event_id > body.latest
          || !['knowledge.proposed','knowledge.verified','knowledge.refuted','knowledge.superseded'].includes(event.kind)
          || !event.payload || typeof event.payload !== 'object' || Array.isArray(event.payload))
        throw new RequestError('invalid knowledge event')
      const payload = JSON.stringify(event)
      if (new TextEncoder().encode(payload).length > 900000) throw new RequestError('knowledge event too large')
      writes.push(db.prepare('INSERT INTO knowledge_events(device_id,event_id,payload) VALUES (?,?,?)')
        .bind(deviceId,event.event_id,payload))
      next = event.event_id
    }
    if (next < body.latest && !body.events.length) throw new RequestError('missing knowledge events')
    writes.push(db.prepare('INSERT INTO knowledge_sync(device_id,cursor,latest,updated_at) VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET cursor=MAX(knowledge_sync.cursor,excluded.cursor),latest=MAX(knowledge_sync.latest,excluded.latest),updated_at=excluded.updated_at')
      .bind(deviceId,next,body.latest,Math.floor(Date.now()/1000)))
    await db.batch(writes) // Events and cursor are committed together.
    return json({ok:true,cursor:next})
  } catch (error) { return requestError(error) }
}
