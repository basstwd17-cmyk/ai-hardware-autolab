import { describe, it, expect, vi } from 'vitest'
import { Api, ApiError } from '../api/client'
import { knowledgeSnapshot, readKnowledge, knowledgeFailure } from './feed'

const event = (id: number) => ({event_id: id, kind: 'knowledge.proposed', payload: {}})
const page = (ids: number[], latest: number, cursor = ids.at(-1) ?? latest) =>
  ({ok: true, knowledge: {events: ids.map(event), cursor, latest}})

describe('durable incremental reader', () => {
  it('paginates and resumes from the last received cursor on tab revisit', async () => {
    const api = new Api('test')
    const command = vi.spyOn(api, 'command').mockResolvedValueOnce(page([1, 2], 3))
      .mockResolvedValueOnce(page([3], 3)).mockResolvedValueOnce(page([4], 4))
    const signal = new AbortController().signal
    expect((await readKnowledge(api, signal)).events).toHaveLength(3)
    expect((await readKnowledge(api, signal)).events).toHaveLength(4)
    expect(command.mock.calls.map(([frame]) => frame.after)).toEqual([0, 2, 3])
  })
  it('preserves valid partial history on failure and isolates sessions', async () => {
    const api = new Api('a')
    vi.spyOn(api, 'command').mockResolvedValueOnce(page([1], 2)).mockRejectedValue(new TypeError('Failed to fetch'))
    await expect(readKnowledge(api, new AbortController().signal)).rejects.toThrow()
    expect(knowledgeSnapshot(api).cursor).toBe(1)
    expect(knowledgeSnapshot(new Api('b')).events).toHaveLength(0)
  })
  it('does not commit a stale aborted response or skip missing pages', async () => {
    const api = new Api('a')
    vi.spyOn(api, 'command').mockResolvedValue(page([], 3, 3))
    await expect(readKnowledge(api, new AbortController().signal)).rejects.toThrow('끝까지')
    const controller = new AbortController()
    controller.abort()
    await expect(readKnowledge(api, controller.signal)).rejects.toThrow('aborted')
    expect(knowledgeSnapshot(api).cursor).toBe(0)
  })
  it('explains offline and expired sessions instead of saying knowledge was deleted', () => {
    expect(knowledgeFailure(new TypeError('Failed to fetch'))).toContain('삭제된 것은 아닙니다')
    expect(knowledgeFailure(new ApiError(403, 'Forbidden'))).toContain('세션 인증')
  })
})
