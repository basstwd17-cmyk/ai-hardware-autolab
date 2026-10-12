import {test} from 'node:test'
import assert from 'node:assert/strict'
import {DatabaseSync} from 'node:sqlite'
import {createHash} from 'node:crypto'
import {readFileSync} from 'node:fs'
import {onRequestGet, onRequestPost} from '../webui/cloudflare/functions/api/relay/knowledge.js'

function fixture() {
  const sqlite = new DatabaseSync(':memory:')
  sqlite.exec(readFileSync(new URL('../webui/cloudflare/schema.sql',import.meta.url),'utf8'))
  const db = {prepare(sql) {return {bind(...values) {return {
    first:async()=>sqlite.prepare(sql).get(...values),
    all:async()=>({results:sqlite.prepare(sql).all(...values)}),
    run:async()=>sqlite.prepare(sql).run(...values),
  }}}}, async batch(writes) {
    sqlite.exec('BEGIN')
    try {for (const write of writes) await write.run();sqlite.exec('COMMIT')}
    catch(error) {sqlite.exec('ROLLBACK');throw error}
  }}
  const hash = t=>createHash('sha256').update(t).digest('hex')
  const env = {RELAY_DB:db,OPERATOR_TOKEN_SHA256:hash('operator'),DEVICE_TOKEN_SHA256:hash('device')}
  return {sqlite, async post(body,token='device') {
    return onRequestPost({env,request:new Request('https://test/api/relay/knowledge',{
      method:'POST',headers:{authorization:`Bearer ${token}`,'content-type':'application/json'},body:JSON.stringify(body)})})
  },async get(query='deviceId=pc',token='operator') {
    return onRequestGet({env,request:new Request(`https://test/api/relay/knowledge?${query}`,{headers:{authorization:`Bearer ${token}`}})})
  }}
}
const event = n=>({event_id:n,kind:'knowledge.proposed',payload:{candidate:{statement:`Note ${n}`}}})

test('separate device-write and operator-read authentication',async()=>{
  const f=fixture()
  assert.equal((await f.post({deviceId:'pc'},'operator')).status,403)
  assert.equal((await f.get('deviceId=pc','device')).status,403)
  assert.equal((await f.get()).status,503)
  f.sqlite.close()
})
test('incremental replica, device isolation, restart cursor and duplicate retry',async()=>{
  const f=fixture()
  assert.deepEqual(await (await f.post({deviceId:'pc'})).json(),{ok:true,cursor:0})
  const batch={deviceId:'pc',after:0,events:[event(1),event(2)],latest:3}
  assert.equal((await f.post(batch)).status,200)
  assert.equal((await f.post(batch)).status,422)
  assert.equal((await f.get('deviceId=another')).status,503)
  const page=await (await f.get('deviceId=pc&after=0&limit=1')).json()
  assert.equal(page.knowledge.events.length,1)
  assert.equal(page.knowledge.cursor,1)
  assert.equal(page.knowledge.latest,2)
  assert.equal(page.sync.latest,3)
  assert.equal((await f.post({deviceId:'pc',after:2,events:[event(3)],latest:3})).status,200)
  assert.equal((await (await f.post({deviceId:'pc'})).json()).cursor,3)
  f.sqlite.close()
})
test('invalid batch cannot partially advance or skip unavailable results',async()=>{
  const f=fixture()
  assert.equal((await f.post({deviceId:'pc',after:0,events:[event(1),event(1)],latest:2})).status,422)
  assert.equal(f.sqlite.prepare('select count(*) n from knowledge_events').get().n,0)
  assert.equal((await f.post({deviceId:'pc',after:0,events:[],latest:1})).status,422)
  assert.equal((await f.get('deviceId=pc&after=-1')).status,422)
  f.sqlite.close()
})
