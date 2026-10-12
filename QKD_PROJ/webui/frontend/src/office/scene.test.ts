/**
 * The office scene: golden, and identical whichever way the history arrived.
 *
 * The backend already guarantees that a full replay and a mid-cursor
 * reconnect produce the same `OfficeSnapshot`. What these tests hold is the
 * half that lives in the browser: that the same snapshot always lays out the
 * same room, and that nothing about the drawing sneaks a clock or a random
 * into the character state.
 */

import { describe, expect, it } from 'vitest'
import type { Occupant, OfficeSnapshot } from '../api/client'
import { buildScene, frameAt, columnsFor, layoutLabelBoxes, motionAt } from './scene'
import { POSE_FRAMES, characterIndexFor, poseFor } from './sprites'

function occupant(agent: string, jobId: string, activity: string, extra: Partial<Occupant> = {}) {
  return {
    id: `${jobId}:${agent}`,
    agent,
    jobId,
    activity,
    detail: activity,
    seenAt: 1,
    turns: 0,
    ...extra,
  } as Occupant
}

function snapshot(occupants: Occupant[], cursor = 10): OfficeSnapshot {
  return {
    cursor,
    openJobs: [...new Set(occupants.map((o) => o.jobId))],
    occupants,
  }
}

const ROOM = snapshot([
  occupant('gate-architect', 'job_1', 'typing', { seenAt: 7, turns: 1 }),
  occupant('design-builder', 'job_1', 'running', { seenAt: 8 }),
  occupant('verifier', 'job_1', 'reading', { seenAt: 9 }),
])

// -- golden -------------------------------------------------------------------

describe('the golden room', () => {
  it('lays out a known snapshot exactly', () => {
    const scene = buildScene(ROOM, 640)
    expect(
      scene.characters.map((c) => ({
        id: c.id,
        character: c.character,
        pose: c.pose,
        x: c.x,
        y: c.y,
        seat: c.seat,
      })),
    ).toEqual([
      {
        id: 'job_1:builder',
        character: characterIndexFor('builder'),
        pose: 'typing',
        x: 32,
        y: 44,
        seat: { row: 0, column: 0 },
      },
      {
        id: 'job_1:lead',
        character: characterIndexFor('lead'),
        pose: 'typing',
        x: 96,
        y: 44,
        seat: { row: 0, column: 1 },
      },
      {
        id: 'job_1:verifier',
        character: characterIndexFor('verifier'),
        pose: 'reading',
        x: 160,
        y: 44,
        seat: { row: 0, column: 2 },
      },
    ])
    expect(scene.cursor).toBe(10)
  })

  it('is a pure function of the snapshot', () => {
    expect(buildScene(ROOM, 640)).toEqual(buildScene(ROOM, 640))
  })

  it('shows one readable character when several internal stages belong to one team', () => {
    const scene = buildScene(snapshot([
      occupant('change-analyst', 'job_1', 'reading', { seenAt: 6, turns: 2 }),
      occupant('architecture-selector', 'job_1', 'typing', { seenAt: 9, turns: 3 }),
    ]), 640)
    expect(scene.characters).toHaveLength(1)
    expect(scene.characters[0]).toMatchObject({
      id: 'job_1:lead',
      agent: 'lead',
      activity: 'typing',
      detail: 'architecture-selector · typing',
    })
  })

  it('gives an agent the same character everywhere, forever', () => {
    // Not random: two people describing the same run must be looking at the
    // same character.
    const first = buildScene(ROOM, 640).characters.find((c) => c.agent === 'verifier')
    const again = buildScene(snapshot([occupant('verifier', 'job_9', 'idle')]), 300).characters[0]
    expect(again.character).toBe(first?.character)
  })
})

// -- parity -------------------------------------------------------------------

describe('however the history arrived', () => {
  it('a full replay and a mid-cursor reconnect lay out the same room', () => {
    // The two snapshots the backend produces for the same history: one built
    // from cursor 0, one from a reconnect that replayed an overlap. The
    // backend asserts they are equal; this asserts the browser agrees.
    const whole = snapshot(
      [
        occupant('gate-architect', 'job_1', 'waiting', { seenAt: 5, turns: 1 }),
        occupant('design-builder', 'job_1', 'running', { seenAt: 8 }),
      ],
      8,
    )
    const resumed = snapshot(
      [
        // Same rows, delivered in the other order by a resubscribe.
        occupant('design-builder', 'job_1', 'running', { seenAt: 8 }),
        occupant('gate-architect', 'job_1', 'waiting', { seenAt: 5, turns: 1 }),
      ],
      8,
    )
    expect(buildScene(resumed, 640)).toEqual(buildScene(whole, 640))
  })

  it('seats by sorted id, so an unrelated job does not shuffle the room', () => {
    const before = buildScene(snapshot([occupant('verifier', 'job_2', 'idle')]), 640)
    const after = buildScene(
      snapshot([occupant('verifier', 'job_2', 'idle'), occupant('verifier', 'job_9', 'idle')]),
      640,
    )
    const kept = after.characters.find((c) => c.id === 'job_2:verifier')
    expect(kept?.seat).toEqual(before.characters[0].seat)
  })

  it('wraps rather than reorders on a narrower panel', () => {
    const wide = buildScene(ROOM, 640)
    const narrow = buildScene(ROOM, 140)
    expect(narrow.characters.map((c) => c.id)).toEqual(wide.characters.map((c) => c.id))
    expect(narrow.columns).toBeLessThan(wide.columns)
    expect(narrow.height).toBeGreaterThan(wide.height)
  })

  it('uses fixed room seats when the renderer supplies them', () => {
    const seats = [
      { x: 10, y: 20, row: 12, column: 3 },
      { x: 30, y: 40, row: 12, column: 7 },
    ]
    const scene = buildScene(ROOM, 640, seats)
    expect(scene.characters[0]).toMatchObject({ x: 10, y: 20, seat: { row: 12, column: 3 } })
    expect(scene.characters[1]).toMatchObject({ x: 30, y: 40, seat: { row: 12, column: 7 } })
    // More agents than fixed seats still get a deterministic fallback.
    expect(scene.characters[2]).toMatchObject({ x: 160, y: 44, seat: { row: 0, column: 2 } })
  })

  it('assigns tagged room seats by public role rather than arrival order', () => {
    const seats = [
      { agent: 'builder', x: 10, y: 20, row: 13, column: 3 },
      { agent: 'lead', x: 30, y: 40, row: 13, column: 7 },
      { agent: 'verifier', x: 50, y: 60, row: 17, column: 11 },
    ]
    const leadOnly = buildScene(snapshot([occupant('lead-agent', 'job_1', 'typing')]), 640, seats)
    expect(leadOnly.characters[0]).toMatchObject({ agent: 'lead', x: 30, y: 40 })
  })
})

// -- what the drawing may not decide ------------------------------------------

describe('the drawing decides nothing', () => {
  it('keeps the projection’s own word for the activity', () => {
    const scene = buildScene(snapshot([occupant('board-operator', 'job_1', 'delegating')]), 640)
    // The pose is a drawing of the activity; the activity is rendered verbatim
    // beside it, so sharing a pose loses nothing the projection said.
    expect(scene.characters[0].activity).toBe('delegating')
    expect(scene.characters[0].pose).toBe('reading')
  })

  it('holds no animation phase', () => {
    const scene = buildScene(ROOM, 640)
    const serialised = JSON.stringify(scene)
    expect(serialised).not.toContain('frame')
    // Which frame is showing depends on when you looked, so it is not state.
    expect(frameAt('typing', 0, POSE_FRAMES.typing)).toBe(3)
    expect(frameAt('typing', 230, POSE_FRAMES.typing)).toBe(4)
    expect(frameAt('typing', 460, POSE_FRAMES.typing)).toBe(3)
    expect(frameAt('standing', 99_999, POSE_FRAMES.standing)).toBe(0)
  })

  it('keeps active work visible at the desk and walks only presentation state', () => {
    const working = buildScene(snapshot([occupant('builder', 'job_1', 'running')]), 640).characters[0]
    expect(motionAt(working, 99_999)).toMatchObject({
      x: working.x,
      y: working.y,
      pose: 'typing',
      moving: false,
    })

    const waiting = buildScene(snapshot([occupant('verifier', 'job_1', 'waiting')]), 640).characters[0]
    const samples = [0, 3_000, 6_000, 9_000, 12_000].map((time) => motionAt(waiting, time))
    expect(samples.some((sample) => sample.moving)).toBe(true)
    expect(samples.some((sample) => sample.pose === 'walking')).toBe(true)
  })

  it('moves colliding name plates and keeps them inside the canvas', () => {
    const boxes = layoutLabelBoxes([
      { id: 'lead', anchorX: 50, anchorY: 50, width: 36, height: 12 },
      { id: 'builder', anchorX: 52, anchorY: 51, width: 40, height: 12 },
      { id: 'verifier', anchorX: 49, anchorY: 49, width: 44, height: 12 },
    ], 100, 80)
    expect(new Set(boxes.map((box) => `${box.x}:${box.y}`))).toHaveLength(3)
    for (const box of boxes) {
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.y).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(100)
      expect(box.y + box.height).toBeLessThanOrEqual(80)
    }
  })

  it('draws an empty floor for an empty room rather than inventing anybody', () => {
    const scene = buildScene(snapshot([], 4), 640)
    expect(scene.characters).toEqual([])
    expect(scene.cursor).toBe(4)
    expect(buildScene(null).characters).toEqual([])
  })

  it('never asks for a sheet outside the vendored six', () => {
    for (const agent of ['a', 'verifier', 'contract-drafter', '', 'x'.repeat(200)]) {
      const index = characterIndexFor(agent)
      expect(index).toBeGreaterThanOrEqual(0)
      expect(index).toBeLessThan(6)
    }
  })

  it('falls back to standing for an activity it has never seen', () => {
    expect(poseFor('somethingNew')).toBe('standing')
    expect(columnsFor(0)).toBe(1)
  })
})
