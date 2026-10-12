/** Visual-only navigation. No ledger, tool, permission or job state is written. */
import type { OfficeLayout, LayoutBounds } from './layout'
import { CHAR_FRAME_H, FLOOR_TILE_SIZE as TILE, type Direction } from './sprites'
import { SITTING_OFFSET, type CharacterMotion, type PlacedCharacter } from './scene'

export interface Tile { col: number; row: number }
export interface RestSpot extends Tile { id: string; facing: Direction }
export interface Navigation {
  bounds: LayoutBounds
  walkable: Set<string>
  seats: RestSpot[]
}
export const tileKey = (tile: Tile) => `${tile.col},${tile.row}`

// Footprints/background rows from the pinned furniture manifests. Decorative
// wall/surface objects do not block the floor; PNG height is NOT a footprint.
const BLOCKS: Record<string, [number, number, number]> = {
  TABLE_FRONT: [3, 4, 1], DESK_FRONT: [3, 2, 1], COFFEE_TABLE: [2, 2, 0],
  CUSHIONED_BENCH: [1, 1, 0], WOODEN_CHAIR_SIDE: [1, 2, 1],
  SOFA_SIDE: [1, 2, 0], SOFA_FRONT: [2, 1, 0], SOFA_BACK: [2, 1, 0],
  PLANT: [1, 2, 1], PLANT_2: [1, 2, 1], BIN: [1, 1, 0],
  SMALL_TABLE_FRONT: [2, 2, 1], SMALL_TABLE_SIDE: [1, 3, 1],
  DOUBLE_BOOKSHELF: [2, 2, 0],
}

export function buildNavigation(layout: OfficeLayout, bounds: LayoutBounds): Navigation {
  const walkable = new Set<string>()
  layout.tiles.forEach((value, index) => {
    if (value > 0 && value < 10) walkable.add(tileKey({ col: index % layout.cols, row: Math.floor(index / layout.cols) }))
  })
  const seats: RestSpot[] = []
  for (const item of layout.furniture) {
    const [type, modifier] = item.type.split(':')
    const footprint = BLOCKS[type]
    if (!footprint) continue
    const [width, height, background] = footprint
    for (let y = background; y < height; y++) {
      for (let x = 0; x < width; x++) walkable.delete(tileKey({ col: item.col + x, row: item.row + y }))
    }
    const chair = type === 'CUSHIONED_BENCH' || type === 'WOODEN_CHAIR_SIDE'
    if (!chair && !type.startsWith('SOFA_')) continue
    const facing: Direction = modifier === 'left' ? 'left'
      : type.endsWith('SIDE') ? 'right' : type === 'SOFA_FRONT' ? 'down' : 'up'
    // One reservation per piece of furniture, including the two-tile sofas.
    // The lower side-sofa tile can be approached from the open aisle.
    seats.push({ id: item.uid, col: item.col, row: item.row + height - 1, facing })
  }
  return { bounds, walkable, seats }
}

/** Cardinal BFS; seats may be endpoints, never shortcuts through furniture. */
export function findPath(nav: Navigation, from: Tile, to: Tile, occupied = new Set<string>(), random = Math.random): Tile[] {
  const start = tileKey(from), end = tileKey(to)
  if (start === end) return []
  const allowedTarget = nav.walkable.has(end) || nav.seats.some((seat) => tileKey(seat) === end)
  if (!allowedTarget || occupied.has(end)) return []
  const directions = shuffle([[0, 1], [1, 0], [0, -1], [-1, 0]], random)
  const queue = [from]
  const parents = new Map<string, Tile | null>([[start, null]])
  for (let head = 0; head < queue.length; head++) {
    const tile = queue[head]
    for (const [dx, dy] of directions) {
      const next = { col: tile.col + dx, row: tile.row + dy }, key = tileKey(next)
      if (parents.has(key) || occupied.has(key) || (!nav.walkable.has(key) && key !== end)) continue
      parents.set(key, tile)
      if (key === end) {
        const path = [next]
        let cursor = tile
        while (tileKey(cursor) !== start) {
          path.unshift(cursor)
          cursor = parents.get(tileKey(cursor))!
        }
        return path
      }
      queue.push(next)
    }
  }
  return []
}

interface Walker {
  at: Tile
  next?: Tile
  progress: number
  path: Tile[]
  target: RestSpot
  working: boolean
  pause: number
  blocked: number
  bag: RestSpot[]
}

/** Ephemeral renderer state survives new snapshots, but never enters a job. */
export class OfficeWalkers {
  private walkers = new Map<string, Walker>()
  private lastTime: number | undefined
  constructor(readonly nav: Navigation, private random = Math.random) {}

  sample(characters: PlacedCharacter[], now: number): Map<string, CharacterMotion> {
    // Do not fast-forward through furniture when the browser tab was hidden.
    const dt = this.lastTime === undefined ? 0 : Math.min(100, Math.max(0, now - this.lastTime))
    this.lastTime = now
    const ids = new Set(characters.map((c) => c.id))
    for (const id of this.walkers.keys()) if (!ids.has(id)) this.walkers.delete(id)
    const homes = new Set(characters.map((c) => tileKey({ col: c.seat.column, row: c.seat.row })))
    const output = new Map<string, CharacterMotion>()
    for (const character of characters) {
      const home = { id: `home:${character.id}`, col: character.seat.column, row: character.seat.row, facing: character.facing ?? 'up' }
      const working = ['typing', 'running', 'reading', 'delegating'].includes(character.activity)
      let walker = this.walkers.get(character.id)
      if (!walker) {
        walker = { at: home, progress: 0, path: [], target: home, working, pause: 1000 + this.random() * 3000, blocked: 0, bag: [] }
        this.walkers.set(character.id, walker)
      }
      const others = [...this.walkers.entries()].filter(([id]) => id !== character.id).map(([, w]) => w)
      const occupied = new Set(others.flatMap((w) => [tileKey(w.at), ...(w.next ? [tileKey(w.next)] : [])]))
      if (working !== walker.working) {
        walker.working = working
        walker.path = [] // finish the current tile before changing direction
        walker.pause = working ? 0 : 1200 + this.random() * 3000
        if (working) walker.target = home
      }
      if (walker.next) {
        walker.progress += dt / 420
        if (walker.progress >= 1) {
          walker.at = walker.next
          walker.next = undefined
          walker.progress = 0
          if (tileKey(walker.at) === tileKey(walker.target)) walker.pause = 3500 + this.random() * 6500
        }
      } else if (tileKey(walker.at) !== tileKey(walker.target)) {
        if (!walker.path.length) walker.path = findPath(this.nav, walker.at, walker.target, occupied, this.random)
        const next = walker.path[0]
        if (next && !occupied.has(tileKey(next))) {
          walker.next = walker.path.shift()
          walker.blocked = 0
        } else {
          walker.blocked += dt
          if (walker.blocked > 1000) {
            walker.path = []
            walker.blocked = 0
            if (!working) { walker.target = { ...walker.at, id: 'aisle', facing: 'down' }; walker.pause = 0 }
          }
        }
      } else if (!working) {
        walker.pause -= dt
        if (walker.pause <= 0) {
          const reserved = new Set(others.map((w) => w.target.id))
          if (!walker.bag.length) walker.bag = shuffle(this.nav.seats.filter((s) => !homes.has(tileKey(s))), this.random)
          while (walker.bag.length) {
            const candidate = walker.bag.pop()!
            if (reserved.has(candidate.id) || tileKey(candidate) === tileKey(walker.at)) continue
            const path = findPath(this.nav, walker.at, candidate, occupied, this.random)
            if (!path.length) continue
            walker.target = candidate
            walker.path = path
            break
          }
          walker.pause = 1000 // bounded retry if all seats are busy/unreachable
        }
      }
      const tile = walker.next ? {
        col: walker.at.col + (walker.next.col - walker.at.col) * walker.progress,
        row: walker.at.row + (walker.next.row - walker.at.row) * walker.progress,
      } : walker.at
      const seated = !walker.next && tileKey(walker.at) === tileKey(walker.target) && walker.target.id !== 'aisle'
      const direction = walker.next ? directionTo(walker.at, walker.next) : walker.target.facing
      output.set(character.id, {
        x: (tile.col - this.nav.bounds.minCol) * TILE,
        y: (tile.row - this.nav.bounds.minRow) * TILE + TILE / 2 - CHAR_FRAME_H + (seated ? SITTING_OFFSET : 0),
        pose: walker.next ? 'walking' : seated ? working ? character.pose : 'reading' : 'standing',
        direction, moving: !!walker.next, seated,
      })
    }
    return output
  }
}

function directionTo(a: Tile, b: Tile): Direction {
  return b.col < a.col ? 'left' : b.col > a.col ? 'right' : b.row < a.row ? 'up' : 'down'
}
function shuffle<T>(values: T[], random: () => number): T[] {
  const result = [...values]
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1))
    ;[result[i], result[j]] = [result[j], result[i]]
  }
  return result
}
