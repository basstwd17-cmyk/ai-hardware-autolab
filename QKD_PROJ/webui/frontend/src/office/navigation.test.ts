import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { defaultOfficeSeats, furnitureDepth, layoutBounds, parseOfficeLayout } from './layout'
import { buildNavigation, findPath, OfficeWalkers, tileKey } from './navigation'
import { buildScene, characterDepth } from './scene'

const layout = parseOfficeLayout(JSON.parse(readFileSync(new URL('../../../assets/assets/default-layout-1.json', import.meta.url), 'utf8')))
const bounds = layoutBounds(layout)
const nav = buildNavigation(layout, bounds)
function random(seed = 42) { return () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296 } }
function characters(activity = 'waiting') {
  return buildScene({ cursor: 1, openJobs: ['job1'], occupants: ['lead-agent', 'builder', 'verifier'].map((agent) => ({
    id: `job1:${agent}`, agent, jobId: 'job1', activity, detail: '', seenAt: 1, turns: 1,
  })) }, bounds.width, defaultOfficeSeats(bounds)).characters
}

describe('layout-aware visual navigation', () => {
  it('finds all four sofas and the meeting chairs without walking through desks', () => {
    expect(nav.seats).toHaveLength(10)
    for (const target of nav.seats) {
      const start = { col: 3, row: 14 }
      if (tileKey(start) === tileKey(target)) continue
      const path = findPath(nav, start, target, new Set(), random())
      expect(path.length, target.id).toBeGreaterThan(0)
      expect(path.at(-1)).toMatchObject({ col: target.col, row: target.row })
      let previous = start
      path.forEach((tile, index) => {
        expect(Math.abs(tile.col - previous.col) + Math.abs(tile.row - previous.row)).toBe(1)
        if (index !== path.length - 1) expect(nav.walkable.has(tileKey(tile))).toBe(true)
        previous = tile
      })
    }
    expect(nav.walkable.has('4,17')).toBe(false) // meeting table
    expect(nav.walkable.has('14,15')).toBe(false) // coffee table
    expect(findPath(nav, { col: 3, row: 14 }, { col: 4, row: 17 })).toEqual([])
  })

  it('uses the original seat direction, seated offset and front-chair sorting', () => {
    const chars = characters('running')
    const motions = new OfficeWalkers(nav, random()).sample(chars, 0)
    for (const character of chars) {
      const motion = motions.get(character.id)!
      expect(motion).toMatchObject({ direction: character.facing, seated: true, moving: false, y: character.y + 6 })
      const chair = layout.furniture.find((f) => f.col === character.seat.column && (f.row === character.seat.row || f.row === character.seat.row - 1) && /CHAIR|BENCH/.test(f.type))!
      expect(characterDepth(character, motion)).toBeGreaterThan(furnitureDepth(chair, bounds, 32))
    }
  })

  it('visits different furniture, does not share seats, and never mutates the snapshot', () => {
    const chars = characters(), original = JSON.stringify(chars)
    const walkers = new OfficeWalkers(nav, random())
    const visits = new Map(chars.map((c) => [c.id, new Set<string>()]))
    for (let time = 0; time < 240_000; time += 50) {
      const motions = walkers.sample(chars, time)
      const seated = new Set<string>()
      for (const [id, motion] of motions) {
        expect(motion.x).toBeGreaterThanOrEqual(0)
        expect(motion.y).toBeGreaterThanOrEqual(0)
        expect(motion.x + 16).toBeLessThanOrEqual(bounds.width)
        expect(motion.y + 32).toBeLessThanOrEqual(bounds.height + 6)
        if (motion.seated) {
          const key = `${motion.x},${motion.y}`
          expect(seated.has(key)).toBe(false)
          seated.add(key)
          visits.get(id)!.add(key)
        }
      }
    }
    for (const places of visits.values()) expect(places.size).toBeGreaterThan(3)
    const sofaXs = nav.seats.filter((s) => s.col >= 13).map((s) => (s.col - bounds.minCol) * 16)
    expect([...visits.values()].every((places) => [...places].some((p) => sofaXs.includes(Number(p.split(',')[0]))))).toBe(true)
    expect(JSON.stringify(chars)).toBe(original)
  })

  it('returns smoothly from the lounge when work starts, and removes departed agents', () => {
    const chars = characters().slice(0, 1)
    const walkers = new OfficeWalkers(nav, random(12))
    for (let time = 0; time < 16000; time += 50) walkers.sample(chars, time)
    let previous = walkers.sample(chars, 16000).get(chars[0].id)!
    chars[0] = { ...chars[0], activity: 'running', pose: 'typing' }
    for (let time = 16050; time < 50000; time += 50) {
      const current = walkers.sample(chars, time).get(chars[0].id)!
      expect(Math.abs(current.x - previous.x) + Math.abs(current.y - previous.y)).toBeLessThan(10)
      previous = current
    }
    expect(previous).toMatchObject({ x: chars[0].x, y: chars[0].y + 6, moving: false, seated: true, pose: 'typing' })
    expect(walkers.sample([], 50100).size).toBe(0)
  })

  it('respects occupied endpoints and a long-hidden browser does not teleport', () => {
    expect(findPath(nav, { col: 3, row: 14 }, nav.seats[0], new Set([tileKey(nav.seats[0])]))).toEqual([])
    const chars = characters().slice(0, 1), walkers = new OfficeWalkers(nav, random())
    for (let time = 0; time < 6000; time += 50) walkers.sample(chars, time)
    const before = walkers.sample(chars, 6000).get(chars[0].id)!
    const after = walkers.sample(chars, 1e9).get(chars[0].id)!
    expect(Math.abs(after.x - before.x) + Math.abs(after.y - before.y)).toBeLessThan(12)
  })
})
