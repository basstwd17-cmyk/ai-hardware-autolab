/**
 * The office floor as data: who stands where, in which pose, on which sheet.
 *
 * This is deliberately split from the drawing. Everything here is a **pure
 * function of the snapshot** — no clock, no random, no canvas — because the
 * property that matters is the one the backend already guarantees and the UI
 * must not break:
 *
 *   the same platform events produce the same character state.
 *
 * The animation *phase* is not part of that. Which of a pose's two frames is
 * showing depends on when you looked, so it lives in the renderer and takes
 * the elapsed time as an argument. If it lived here, two tabs opened a second
 * apart would hold different "state" for an identical history, and the parity
 * test would either fail or have to be weakened until it proved nothing.
 *
 * Seats are assigned by sorted occupant id, so a character does not jump
 * across the room when an unrelated job opens. It moves only when the set of
 * occupants actually changes, and then to a position anybody replaying the
 * same history would compute.
 */

import type { Occupant, OfficeSnapshot } from '../api/client'
import { teamForStage } from '../app/agentTeams'
import {
  CHAR_FRAME_H,
  CHAR_FRAME_W,
  FLOOR_TILE_SIZE,
  characterIndexFor,
  poseFor,
  type Direction,
  type Pose,
} from './sprites'

/** Tile grid for desks, in sprite pixels before scaling. */
export const SEAT_W = 48
export const SEAT_H = 56
export const SEAT_GAP_X = 16
export const SEAT_GAP_Y = 20

export interface PlacedCharacter {
  /** `jobId:agent`, straight from the projection. */
  id: string
  agent: string
  jobId: string
  /** The projection's word for what it is doing. Rendered as text, verbatim. */
  activity: string
  detail: string
  turns: number
  /** Which of the six vendored sheets, chosen stably from the agent's name. */
  character: number
  pose: Pose
  /** Top-left of the character in sprite pixels. */
  x: number
  y: number
  width: number
  height: number
  seat: { row: number; column: number }
  facing?: Direction
}

export interface Scene {
  cursor: number
  openJobs: string[]
  characters: PlacedCharacter[]
  /** Floor extent in sprite pixels, so the renderer can size its canvas. */
  width: number
  height: number
  columns: number
}

export interface CharacterMotion {
  x: number
  y: number
  pose: Pose
  direction: Direction
  moving: boolean
  seated?: boolean
}

export interface LabelBox {
  id: string
  x: number
  y: number
  width: number
  height: number
  anchorX: number
  anchorY: number
}

type FixedSeat = {
  x: number
  y: number
  row: number
  column: number
  agent?: string
  facing?: Direction
}

/** How many desks fit across, given a floor width in sprite pixels. */
export function columnsFor(floorWidth: number): number {
  const usable = Math.max(floorWidth, SEAT_W + SEAT_GAP_X)
  return Math.max(1, Math.floor(usable / (SEAT_W + SEAT_GAP_X)))
}

/**
 * Lay out a snapshot.
 *
 * `floorWidth` is the only input that is not the snapshot, and it only decides
 * how many desks fit on a row — two viewports of different widths show the
 * same characters in the same order, wrapped differently.
 */
export function buildScene(
  snapshot: OfficeSnapshot | null,
  floorWidth = 640,
  fixedSeats: FixedSeat[] = [],
): Scene {
  const occupants = visibleOccupants(snapshot).sort((a, b) => (a.id < b.id ? -1 : 1))
  const columns = columnsFor(floorWidth)
  const claimedSeats = new Set<number>()

  const characters = occupants.map((occupant, index) => {
    let fixedIndex = fixedSeats.findIndex(
      (seat, candidate) => seat.agent === occupant.agent && !claimedSeats.has(candidate),
    )
    if (fixedIndex < 0) {
      fixedIndex = fixedSeats.findIndex(
        (seat, candidate) => !seat.agent && !claimedSeats.has(candidate),
      )
    }
    if (fixedIndex >= 0) claimedSeats.add(fixedIndex)
    return place(occupant, index, columns, fixedSeats[fixedIndex])
  })
  const rows = Math.max(1, Math.ceil(characters.length / columns))

  return {
    cursor: snapshot?.cursor ?? 0,
    openJobs: [...(snapshot?.openJobs ?? [])].sort(),
    characters,
    columns,
    width: columns * (SEAT_W + SEAT_GAP_X) + SEAT_GAP_X,
    height: rows * (SEAT_H + SEAT_GAP_Y) + SEAT_GAP_Y,
  }
}

/**
 * Presentation-only movement. Durable state still comes exclusively from the
 * ledger snapshot; this function merely gives waiting/reading agents a
 * deterministic walk cycle through furniture-free lanes. Working agents stay
 * at their assigned desks.
 */
export function motionAt(character: PlacedCharacter, elapsedMs: number): CharacterMotion {
  if (['typing', 'running', 'reading', 'delegating'].includes(character.activity)) {
    return {
      x: character.x,
      y: character.y + (character.facing ? SITTING_OFFSET : 0),
      pose: character.pose,
      direction: character.facing ?? 'down',
      moving: false,
      seated: !!character.facing,
    }
  }

  const route = walkingRoute(character)
  if (route.length < 2) {
    return {
      x: character.x,
      y: character.y,
      pose: character.pose,
      direction: 'down',
      moving: false,
    }
  }

  // Pause long enough at every stop to keep the room readable, then walk to
  // the next stop. Each character gets a stable phase offset so they do not
  // move as a synchronized block.
  const pauseMs = 2_400
  const travelMs = 1_300
  const legMs = pauseMs + travelMs
  const offset = stableOffset(character.id, route.length * legMs)
  const cycle = (elapsedMs + offset) % (route.length * legMs)
  const leg = Math.floor(cycle / legMs)
  const within = cycle % legMs
  const from = route[leg]
  const to = route[(leg + 1) % route.length]
  if (within < pauseMs) {
    if (leg === 0 && character.facing) {
      return { x: from.x, y: from.y + SITTING_OFFSET, pose: 'reading', direction: character.facing, moving: false, seated: true }
    }
    return { x: from.x, y: from.y, pose: character.pose, direction: 'down', moving: false }
  }
  const progress = (within - pauseMs) / travelMs
  const x = from.x + (to.x - from.x) * progress
  const y = from.y + (to.y - from.y) * progress
  return { x, y, pose: 'walking', direction: directionFor(from, to), moving: true }
}

// Matched to upstream constants.ts / renderer.ts. Keep depth independent of
// the seated visual offset; same-row chairs must remain behind the character.
export const SITTING_OFFSET = 6
export function characterDepth(character: PlacedCharacter, motion: CharacterMotion): number {
  return motion.y - (motion.seated ? SITTING_OFFSET : 0) + character.height + FLOOR_TILE_SIZE / 2 + 0.5
}

/** Place labels without allowing two opaque name plates to cover each other. */
export function layoutLabelBoxes(
  labels: Array<{
    id: string
    anchorX: number
    anchorY: number
    width: number
    height: number
  }>,
  canvasWidth: number,
  canvasHeight: number,
): LabelBox[] {
  const placed: LabelBox[] = []
  for (const label of labels) {
    const centredX = label.anchorX - label.width / 2
    const desiredY = label.anchorY - label.height - 8
    const candidates = [
      [0, 0],
      [0, -14],
      [0, -28],
      [-(label.width + 7), 0],
      [label.width + 7, 0],
      [-(label.width + 7), -14],
      [label.width + 7, -14],
      [0, 14],
    ]
    let chosen: LabelBox | undefined
    for (const [dx, dy] of candidates) {
      const box: LabelBox = {
        ...label,
        x: clamp(Math.round(centredX + dx), 2, Math.max(2, canvasWidth - label.width - 2)),
        y: clamp(Math.round(desiredY + dy), 2, Math.max(2, canvasHeight - label.height - 2)),
      }
      if (!placed.some((other) => overlaps(box, other, 3))) {
        chosen = box
        break
      }
    }
    if (!chosen) {
      // Three public roles normally fit in the candidates above. This fallback
      // also keeps old multi-job histories readable instead of drawing a pile.
      for (let y = 2; y <= canvasHeight - label.height - 2; y += label.height + 4) {
        const box: LabelBox = {
          ...label,
          x: clamp(Math.round(centredX), 2, Math.max(2, canvasWidth - label.width - 2)),
          y,
        }
        if (!placed.some((other) => overlaps(box, other, 3))) {
          chosen = box
          break
        }
      }
    }
    placed.push(chosen ?? {
      ...label,
      x: clamp(Math.round(centredX), 2, Math.max(2, canvasWidth - label.width - 2)),
      y: 2,
    })
  }
  return placed
}

function overlaps(a: LabelBox, b: LabelBox, gap: number): boolean {
  return !(
    a.x + a.width + gap <= b.x ||
    b.x + b.width + gap <= a.x ||
    a.y + a.height + gap <= b.y ||
    b.y + b.height + gap <= a.y
  )
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value))
}

function walkingRoute(character: PlacedCharacter): Array<{ x: number; y: number }> {
  const home = { x: character.x, y: character.y }
  if (character.agent === 'builder') {
    return [home, { x: home.x, y: home.y + 16 }, { x: home.x - 32, y: home.y + 16 }, { x: home.x, y: home.y + 16 }]
  }
  if (character.agent === 'lead') {
    return [home, { x: home.x, y: home.y + 16 }, { x: home.x + 32, y: home.y + 16 }, { x: home.x, y: home.y + 16 }]
  }
  if (character.agent === 'verifier') {
    return [home, { x: home.x + 16, y: home.y }, { x: home.x + 32, y: home.y }, { x: home.x + 32, y: home.y - 32 }, { x: home.x + 16, y: home.y - 32 }, { x: home.x + 16, y: home.y }]
  }
  return [home]
}

function directionFor(from: { x: number; y: number }, to: { x: number; y: number }): Direction {
  const dx = to.x - from.x
  const dy = to.y - from.y
  if (Math.abs(dx) > Math.abs(dy)) return dx < 0 ? 'left' : 'right'
  return dy < 0 ? 'up' : 'down'
}

function stableOffset(value: string, modulus: number): number {
  let hash = 0
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0
  }
  return Math.abs(hash) % Math.max(1, modulus)
}

/**
 * Several narrow stages can belong to one team.  The floor shows that team
 * once, using its newest activity; the exact stage remains in `detail` for
 * the hover card and in the ledger transcript.  This is presentation-only:
 * it never changes which jobs are open or what the control plane decided.
 */
function visibleOccupants(snapshot: OfficeSnapshot | null): Occupant[] {
  const groups = new Map<string, Occupant>()
  for (const source of snapshot?.occupants ?? []) {
    const team = teamForStage(source.agent)
    const id = `${source.jobId}:${team}`
    const previous = groups.get(id)
    if (previous && previous.seenAt > source.seenAt) continue
    groups.set(id, {
      ...source,
      id,
      agent: team,
      detail: `${source.agent}${source.detail ? ` · ${source.detail}` : ''}`,
      turns: previous ? previous.turns + source.turns : source.turns,
    })
  }
  return [...groups.values()]
}

function place(
  occupant: Occupant,
  index: number,
  columns: number,
  fixedSeat?: FixedSeat,
): PlacedCharacter {
  const row = Math.floor(index / columns)
  const column = index % columns
  const seatX = fixedSeat?.x ?? SEAT_GAP_X + column * (SEAT_W + SEAT_GAP_X)
  const seatY = fixedSeat?.y ?? SEAT_GAP_Y + row * (SEAT_H + SEAT_GAP_Y)

  return {
    id: occupant.id,
    agent: occupant.agent,
    jobId: occupant.jobId,
    activity: occupant.activity,
    detail: occupant.detail,
    turns: occupant.turns,
    character: characterIndexFor(occupant.agent),
    pose: poseFor(occupant.activity),
    // Fixed room seats are already character top-left coordinates.  The
    // responsive fallback remains centred on its generated desk cell.
    x: fixedSeat ? seatX : seatX + Math.floor((SEAT_W - CHAR_FRAME_W) / 2),
    y: fixedSeat ? seatY : seatY + (SEAT_H - CHAR_FRAME_H),
    width: CHAR_FRAME_W,
    height: CHAR_FRAME_H,
    seat: fixedSeat ? { row: fixedSeat.row, column: fixedSeat.column } : { row, column },
    facing: fixedSeat?.facing,
  }
}

/**
 * Which frame of a pose is showing, given elapsed time.
 *
 * Not part of the scene: it is the one thing about the office that legitimately
 * depends on when you looked. Kept pure and passed a clock so it can still be
 * tested without one.
 */
export function frameAt(pose: Pose, elapsedMs: number, frames: number[]): number {
  if (frames.length <= 1) return frames[0] ?? 0
  const period = pose === 'typing' ? 220 : 420
  return frames[Math.floor(elapsedMs / period) % frames.length]
}
