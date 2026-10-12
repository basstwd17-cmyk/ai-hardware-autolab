/**
 * The vendored office layout, interpreted without making UI state.
 *
 * The JSON is data exported in P3-A.  This module is deliberately pure: it
 * validates that data, resolves only same-origin vendored assets and computes
 * the crop/seat geometry used by both the renderer and its parity tests.
 */

import { ASSET_BASE, CHAR_FRAME_H, FLOOR_TILE_SIZE, floorUrl, type Direction } from './sprites'

export const DEFAULT_LAYOUT_URL = `${ASSET_BASE}/assets/default-layout-1.json`
export const VOID_TILE = 255

export interface LayoutFurniture {
  uid: string
  type: string
  col: number
  row: number
}

export interface OfficeLayout {
  version: number
  cols: number
  rows: number
  tiles: number[]
  furniture: LayoutFurniture[]
}

export interface LayoutBounds {
  minCol: number
  minRow: number
  maxCol: number
  maxRow: number
  width: number
  height: number
}

export interface OfficeSeat {
  x: number
  y: number
  row: number
  column: number
  agent?: string
  facing?: Direction
}

type FurnitureAsset = { folder: string; file: string }

const FURNITURE_ASSETS: Record<string, FurnitureAsset> = {
  TABLE_FRONT: { folder: 'TABLE_FRONT', file: 'TABLE_FRONT.png' },
  COFFEE_TABLE: { folder: 'COFFEE_TABLE', file: 'COFFEE_TABLE.png' },
  SOFA_SIDE: { folder: 'SOFA', file: 'SOFA_SIDE.png' },
  SOFA_BACK: { folder: 'SOFA', file: 'SOFA_BACK.png' },
  SOFA_FRONT: { folder: 'SOFA', file: 'SOFA_FRONT.png' },
  HANGING_PLANT: { folder: 'HANGING_PLANT', file: 'HANGING_PLANT.png' },
  DOUBLE_BOOKSHELF: { folder: 'DOUBLE_BOOKSHELF', file: 'DOUBLE_BOOKSHELF.png' },
  SMALL_PAINTING: { folder: 'SMALL_PAINTING', file: 'SMALL_PAINTING.png' },
  CLOCK: { folder: 'CLOCK', file: 'CLOCK.png' },
  PLANT: { folder: 'PLANT', file: 'PLANT.png' },
  COFFEE: { folder: 'COFFEE', file: 'COFFEE.png' },
  WOODEN_CHAIR_SIDE: { folder: 'WOODEN_CHAIR', file: 'WOODEN_CHAIR_SIDE.png' },
  DESK_FRONT: { folder: 'DESK', file: 'DESK_FRONT.png' },
  CUSHIONED_BENCH: { folder: 'CUSHIONED_BENCH', file: 'CUSHIONED_BENCH.png' },
  PC_FRONT_OFF: { folder: 'PC', file: 'PC_FRONT_OFF.png' },
  PC_SIDE: { folder: 'PC', file: 'PC_SIDE.png' },
  PLANT_2: { folder: 'PLANT_2', file: 'PLANT_2.png' },
  LARGE_PAINTING: { folder: 'LARGE_PAINTING', file: 'LARGE_PAINTING.png' },
  BIN: { folder: 'BIN', file: 'BIN.png' },
  SMALL_TABLE_FRONT: { folder: 'SMALL_TABLE', file: 'SMALL_TABLE_FRONT.png' },
  SMALL_TABLE_SIDE: { folder: 'SMALL_TABLE', file: 'SMALL_TABLE_SIDE.png' },
  SMALL_PAINTING_2: { folder: 'SMALL_PAINTING_2', file: 'SMALL_PAINTING_2.png' },
}

/** Tile zero is the wall; floor ids 1..9 select floor_0..floor_8. */
export function tileUrl(tile: number): string | null {
  if (tile === VOID_TILE) return null
  if (tile === 0) return `${ASSET_BASE}/assets/walls/wall_0.png`
  if (tile >= 1 && tile <= 9) return floorUrl(tile - 1)
  return null
}

export function furnitureAsset(type: string): { url: string; mirror: boolean } | null {
  const [base, modifier] = type.split(':', 2)
  const asset = FURNITURE_ASSETS[base]
  if (!asset) return null
  return {
    url: `${ASSET_BASE}/assets/furniture/${asset.folder}/${asset.file}`,
    mirror: modifier === 'left',
  }
}

export function parseOfficeLayout(value: unknown): OfficeLayout {
  if (!value || typeof value !== 'object') throw new Error('office layout must be an object')
  const raw = value as Record<string, unknown>
  const cols = integer(raw.cols, 'cols')
  const rows = integer(raw.rows, 'rows')
  if (cols < 1 || rows < 1 || cols > 64 || rows > 64) {
    throw new Error('office layout dimensions are outside 1..64')
  }
  if (!Array.isArray(raw.tiles) || raw.tiles.length !== cols * rows) {
    throw new Error(`office layout needs exactly ${cols * rows} tiles`)
  }
  const tiles = raw.tiles.map((tile, index) => {
    const number = integer(tile, `tiles[${index}]`)
    if (number !== VOID_TILE && (number < 0 || number > 9)) {
      throw new Error(`tiles[${index}] has unsupported id ${number}`)
    }
    return number
  })
  if (!Array.isArray(raw.furniture)) throw new Error('office layout furniture must be an array')
  const furniture = raw.furniture.map((item, index) => {
    if (!item || typeof item !== 'object') throw new Error(`furniture[${index}] must be an object`)
    const row = item as Record<string, unknown>
    const uid = string(row.uid, `furniture[${index}].uid`)
    const type = string(row.type, `furniture[${index}].type`)
    const col = integer(row.col, `furniture[${index}].col`)
    const furnitureRow = integer(row.row, `furniture[${index}].row`)
    if (col < 0 || col >= cols || furnitureRow < 0 || furnitureRow >= rows) {
      throw new Error(`furniture[${index}] is outside the room`)
    }
    if (!furnitureAsset(type)) throw new Error(`furniture[${index}] uses unknown type ${type}`)
    return { uid, type, col, row: furnitureRow }
  })
  return { version: integer(raw.version, 'version'), cols, rows, tiles, furniture }
}

/** Crop the ten empty rows in the upstream canvas without changing coordinates. */
export function layoutBounds(layout: OfficeLayout): LayoutBounds {
  let minCol = layout.cols
  let minRow = layout.rows
  let maxCol = 0
  let maxRow = 0
  layout.tiles.forEach((tile, index) => {
    if (tile === VOID_TILE) return
    const col = index % layout.cols
    const row = Math.floor(index / layout.cols)
    minCol = Math.min(minCol, col)
    minRow = Math.min(minRow, row)
    maxCol = Math.max(maxCol, col + 1)
    maxRow = Math.max(maxRow, row + 1)
  })
  for (const item of layout.furniture) {
    minCol = Math.min(minCol, item.col)
    minRow = Math.min(minRow, item.row)
    maxCol = Math.max(maxCol, item.col + 1)
    maxRow = Math.max(maxRow, item.row + 1)
  }
  if (minCol >= maxCol || minRow >= maxRow) return emptyBounds()
  return {
    minCol,
    minRow,
    maxCol,
    maxRow,
    width: (maxCol - minCol) * FLOOR_TILE_SIZE,
    height: (maxRow - minRow) * FLOOR_TILE_SIZE,
  }
}

/**
 * Seats use the upstream tile-centre foot anchor, not a standing sprite placed
 * in front of the furniture. Wooden chairs have one background tile: their
 * seat is the next row. The cushions face the adjacent desks (up).
 */
export function defaultOfficeSeats(bounds: LayoutBounds): OfficeSeat[] {
  const coordinates = [
    { agent: 'builder', column: 3, row: 14, facing: 'up' as const },
    { agent: 'lead', column: 7, row: 14, facing: 'up' as const },
    { agent: 'verifier', column: 7, row: 19, facing: 'left' as const },
  ]
  return coordinates.map(({ agent, column, row, facing }) => ({
    agent,
    x: (column - bounds.minCol) * FLOOR_TILE_SIZE,
    y: (row - bounds.minRow) * FLOOR_TILE_SIZE + FLOOR_TILE_SIZE / 2 - CHAR_FRAME_H,
    row,
    column,
    facing,
  }))
}

/** Pixel Agents' non-back-facing chairs sort at the FIRST row boundary.
 * Using full sprite height puts a tall chair in front of its occupant.
 * Other furniture keeps ordinary depth sorting; characters are not an overlay.
 */
export function furnitureDepth(item: LayoutFurniture, bounds: LayoutBounds, imageHeight: number): number {
  const y = (item.row - bounds.minRow) * FLOOR_TILE_SIZE
  if (item.type === 'SOFA_BACK') return y + imageHeight + 1
  return y + (/^(CUSHIONED_BENCH|WOODEN_CHAIR_SIDE|SOFA_SIDE|SOFA_FRONT)(:|$)/.test(item.type) ? FLOOR_TILE_SIZE : imageHeight)
}

export function layoutAssetUrls(layout: OfficeLayout): string[] {
  const urls = new Set<string>()
  for (const tile of layout.tiles) {
    const url = tileUrl(tile)
    if (url) urls.add(url)
  }
  for (const item of layout.furniture) {
    const asset = furnitureAsset(item.type)
    if (asset) urls.add(asset.url)
  }
  return [...urls].sort()
}

function emptyBounds(): LayoutBounds {
  return { minCol: 0, minRow: 0, maxCol: 1, maxRow: 1, width: FLOOR_TILE_SIZE, height: FLOOR_TILE_SIZE }
}

function integer(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) throw new Error(`${name} must be an integer`)
  return value
}

function string(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${name} must be a non-empty string`)
  return value
}
