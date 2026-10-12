import { ApiError, type Api } from '../api/client'
import type { KnowledgeEvent } from './model'

export interface KnowledgeSnapshot { events: KnowledgeEvent[]; cursor: number; latest: number }
// Survives tab unmounts, but never leaks another API/session's data or writes
// research contents to unauthenticated persistent browser storage.
const snapshots = new WeakMap<Api, KnowledgeSnapshot>()
const EMPTY: KnowledgeSnapshot = {events: [], cursor: 0, latest: 0}
export function knowledgeSnapshot(api: Api | null): KnowledgeSnapshot {
  return api ? snapshots.get(api) ?? EMPTY : EMPTY
}

export async function readKnowledge(api: Api, signal: AbortSignal): Promise<KnowledgeSnapshot> {
  const previous = knowledgeSnapshot(api)
  let snapshot = previous
  for (let page = 0; page < 20; page++) {
    const answer = await api.command({type: 'knowledge.events', commandId: crypto.randomUUID(),
      after: snapshot.cursor, limit: 500}, signal)
    if (signal.aborted) throw new Error('knowledge request aborted')
    if (answer.ok !== true) throw new Error(String(answer.error ?? '지식 원장 조회가 거부되었습니다.'))
    const value = answer.knowledge as Partial<KnowledgeSnapshot> | undefined
    if (!value || !Array.isArray(value.events) || !Number.isSafeInteger(value.cursor)
        || !Number.isSafeInteger(value.latest)) throw new Error('지식 응답 형식이 올바르지 않습니다.')
    const cursor = value.cursor!, latest = value.latest!
    if (latest < previous.cursor) throw new Error('지식 원장 커서가 바뀌었습니다. 페이지를 새로 열어 확인하세요.')
    if (cursor < snapshot.cursor || cursor > latest) throw new Error('지식 원장 커서가 올바르지 않습니다.')
    const batch = value.events.filter(e => e.event_id > snapshot.cursor)
    let last = snapshot.cursor
    for (const event of batch) {
      if (!Number.isSafeInteger(event.event_id) || event.event_id <= last || event.event_id > cursor
          || !event.payload || typeof event.payload !== 'object') throw new Error('지식 이벤트 순서가 올바르지 않습니다.')
      last = event.event_id
    }
    if (last !== cursor || (cursor < latest && batch.length === 0)) throw new Error('지식 이력을 끝까지 받지 못했습니다.')
    snapshot = {events: batch.length ? [...snapshot.events, ...batch] : snapshot.events, cursor, latest}
    snapshots.set(api, snapshot) // Valid partial pages also survive a later network failure.
    if (cursor >= latest) return snapshot
  }
  return snapshot // UI explicitly displays cursor/latest; next poll continues.
}

export function knowledgeFailure(error: unknown): string {
  if (error instanceof ApiError && [401, 403].includes(error.status)) return '세션 인증이 만료되었습니다. 웹 서버가 이번 실행에 출력한 전체 주소로 다시 접속하세요.'
  if (error instanceof Error && error.name === 'AbortError') return '지식 조회 시간이 초과되었습니다. 웹 서버·데몬 연결을 확인하세요.'
  if (error instanceof TypeError) return '웹 서버에 연결할 수 없습니다. 서버 실행 여부와 현재 세션 주소를 확인하세요. 저장된 지식이 삭제된 것은 아닙니다.'
  return error instanceof Error ? error.message : '지식 원장 조회에 실패했습니다.'
}
