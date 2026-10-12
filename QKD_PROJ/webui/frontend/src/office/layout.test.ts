import { describe, expect, it } from 'vitest'
import {
  defaultOfficeSeats,
  furnitureAsset,
  layoutAssetUrls,
  layoutBounds,
  parseOfficeLayout,
  tileUrl,
} from './layout'

const FIXTURE = {
  version: 1,
  cols: 4,
  rows: 4,
  tiles: [255, 255, 255, 255, 0, 1, 7, 255, 0, 9, 1, 255, 255, 255, 255, 255],
  furniture: [{ uid: 'desk', type: 'DESK_FRONT', col: 1, row: 1 }],
}

describe('vendored office layout', () => {
  it('maps wall and floor ids to pinned same-origin assets', () => {
    expect(tileUrl(255)).toBeNull()
    expect(tileUrl(0)).toBe('/office-assets/assets/walls/wall_0.png')
    expect(tileUrl(1)).toBe('/office-assets/assets/floors/floor_0.png')
    expect(tileUrl(9)).toBe('/office-assets/assets/floors/floor_8.png')
    expect(tileUrl(10)).toBeNull()
  })

  it('resolves mirrored furniture without inventing another file', () => {
    expect(furnitureAsset('WOODEN_CHAIR_SIDE:left')).toEqual({
      url: '/office-assets/assets/furniture/WOODEN_CHAIR/WOODEN_CHAIR_SIDE.png',
      mirror: true,
    })
    expect(furnitureAsset('DESK_FRONT')?.mirror).toBe(false)
    expect(furnitureAsset('unknown')).toBeNull()
  })

  it('validates and crops void rows deterministically', () => {
    const layout = parseOfficeLayout(FIXTURE)
    expect(layoutBounds(layout)).toEqual({
      minCol: 0,
      minRow: 1,
      maxCol: 3,
      maxRow: 3,
      width: 48,
      height: 32,
    })
    expect(layoutAssetUrls(layout)).toEqual([
      '/office-assets/assets/floors/floor_0.png',
      '/office-assets/assets/floors/floor_6.png',
      '/office-assets/assets/floors/floor_8.png',
      '/office-assets/assets/furniture/DESK/DESK_FRONT.png',
      '/office-assets/assets/walls/wall_0.png',
    ])
  })

  it('rejects malformed dimensions, unknown tiles and furniture', () => {
    expect(() => parseOfficeLayout({ ...FIXTURE, tiles: [] })).toThrow(/exactly 16/)
    expect(() => parseOfficeLayout({ ...FIXTURE, tiles: [10, ...FIXTURE.tiles.slice(1)] })).toThrow(
      /unsupported id/,
    )
    expect(() =>
      parseOfficeLayout({
        ...FIXTURE,
        furniture: [{ uid: 'x', type: 'NOT_REAL', col: 1, row: 1 }],
      }),
    ).toThrow(/unknown type/)
  })

  it('pins the three public roles to visible room coordinates', () => {
    const seats = defaultOfficeSeats({
      minCol: 0,
      minRow: 9,
      maxCol: 20,
      maxRow: 21,
      width: 320,
      height: 192,
    })
    expect(seats).toEqual([
      { agent: 'builder', x: 48, y: 56, row: 14, column: 3, facing: 'up' },
      { agent: 'lead', x: 112, y: 56, row: 14, column: 7, facing: 'up' },
      { agent: 'verifier', x: 112, y: 136, row: 19, column: 7, facing: 'left' },
    ])
  })
})
