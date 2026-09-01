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
import { CHAR_FRAME_H, CHAR_FRAME_W, characterIndexFor, poseFor, type Pose } from './sprites'

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
  fixedSeats: Array<{ x: number; y: number; row: number; column: number }> = [],
): Scene {
  const occupants = [...(snapshot?.occupants ?? [])].sort((a, b) => (a.id < b.id ? -1 : 1))
  const columns = columnsFor(floorWidth)

  const characters = occupants.map((occupant, index) =>
    place(occupant, index, columns, fixedSeats[index]),
  )
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

function place(
  occupant: Occupant,
  index: number,
  columns: number,
  fixedSeat?: { x: number; y: number; row: number; column: number },
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
