/**
 * The pixel office, drawn from the pinned P3-A layout and sprite snapshot.
 *
 * The backend projection remains the only source of agent state. Layout JSON
 * decides where furniture is, `buildScene` deterministically assigns the
 * projection's occupants to seats, and this component only paints them.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { OfficeSnapshot } from '../api/client'
import {
  DEFAULT_LAYOUT_URL,
  VOID_TILE,
  defaultOfficeSeats,
  furnitureAsset,
  layoutAssetUrls,
  layoutBounds,
  parseOfficeLayout,
  tileUrl,
  type LayoutBounds,
  type OfficeLayout,
} from './layout'
import { buildScene, frameAt, type PlacedCharacter, type Scene } from './scene'
import {
  CHAR_COUNT,
  FLOOR_TILE_SIZE,
  POSE_FRAMES,
  characterUrl,
  frameRect,
} from './sprites'

const SCALE = 2

function useOfficeLayout() {
  const [state, setState] = useState<{ layout: OfficeLayout | null; error: string }>({
    layout: null,
    error: '',
  })
  useEffect(() => {
    const abort = new AbortController()
    fetch(DEFAULT_LAYOUT_URL, { credentials: 'same-origin', signal: abort.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`layout request failed (${response.status})`)
        return response.json()
      })
      .then((value) => setState({ layout: parseOfficeLayout(value), error: '' }))
      .catch((error: unknown) => {
        if (!abort.signal.aborted) {
          setState({ layout: null, error: error instanceof Error ? error.message : 'layout failed' })
        }
      })
    return () => abort.abort()
  }, [])
  return state
}

/** Load each same-origin image once. Missing images remain a testable gap. */
function useImages(urls: string[]) {
  const [images, setImages] = useState<Record<string, HTMLImageElement>>({})
  const key = urls.join('|')
  useEffect(() => {
    let cancelled = false
    const loaded: Record<string, HTMLImageElement> = {}
    let outstanding = urls.length
    if (outstanding === 0) {
      setImages({})
      return
    }
    for (const url of urls) {
      const image = new Image()
      image.onload = () => {
        loaded[url] = image
        outstanding -= 1
        if (outstanding === 0 && !cancelled) setImages({ ...loaded })
      }
      image.onerror = () => {
        outstanding -= 1
        if (outstanding === 0 && !cancelled) setImages({ ...loaded })
      }
      image.src = url
    }
    return () => {
      cancelled = true
    }
  }, [key])
  return images
}

export function OfficeCanvas({ snapshot }: { snapshot: OfficeSnapshot | null }) {
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const [hovered, setHovered] = useState<PlacedCharacter | null>(null)
  const { layout, error } = useOfficeLayout()
  const bounds = useMemo(() => (layout ? layoutBounds(layout) : fallbackBounds()), [layout])
  const seats = useMemo(() => defaultOfficeSeats(bounds), [bounds])
  const scene = useMemo(() => buildScene(snapshot, bounds.width, seats), [snapshot, bounds, seats])
  const urls = useMemo(
    () => [
      ...(layout ? layoutAssetUrls(layout) : []),
      ...Array.from({ length: CHAR_COUNT }, (_, index) => characterUrl(index)),
    ],
    [layout],
  )
  const images = useImages(urls)

  useEffect(() => {
    const surface = canvas.current
    if (!surface) return
    const context = surface.getContext('2d')
    if (!context) return
    let frame = 0
    const started = performance.now()
    const paint = (now: number) => {
      draw(context, surface, layout, bounds, scene, images, now - started)
      frame = requestAnimationFrame(paint)
    }
    frame = requestAnimationFrame(paint)
    return () => cancelAnimationFrame(frame)
  }, [layout, bounds, scene, images])

  const pick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const surface = canvas.current
    if (!surface) return
    const box = surface.getBoundingClientRect()
    const x = ((event.clientX - box.left) / box.width) * bounds.width
    const y = ((event.clientY - box.top) / box.height) * bounds.height
    setHovered(
      scene.characters.find(
        (character) =>
          x >= character.x - 8 &&
          x <= character.x + character.width + 8 &&
          y >= character.y - 4 &&
          y <= character.y + character.height + 10,
      ) ?? null,
    )
  }

  return (
    <div className="office-canvas">
      <canvas
        ref={canvas}
        width={bounds.width * SCALE}
        height={bounds.height * SCALE}
        onMouseMove={pick}
        onMouseLeave={() => setHovered(null)}
        style={{ imageRendering: 'pixelated' }}
      />
      {error && <p className="office-render-error">Office layout unavailable: {error}</p>}
      {!error && scene.characters.length === 0 && (
        <div className="office-empty">
          <p>
            {scene.openJobs.length === 0
              ? 'The office is ready. Start a job to bring the agents to their desks.'
              : 'A job is open, and no agent has acted on it yet.'}
          </p>
        </div>
      )}
      {hovered && (
        <div className="office-tip">
          <b className="who" data-agent={hovered.agent}>{hovered.agent}</b>
          <span>{hovered.activity}</span>
          {hovered.detail && <span className="muted">{hovered.detail}</span>}
          <span className="muted mono">{hovered.jobId}</span>
          <span className="muted">{hovered.turns} turn{hovered.turns === 1 ? '' : 's'}</span>
        </div>
      )}
    </div>
  )
}

type Drawable =
  | { kind: 'furniture'; z: number; order: number; item: OfficeLayout['furniture'][number] }
  | { kind: 'character'; z: number; order: number; character: PlacedCharacter }

function draw(
  context: CanvasRenderingContext2D,
  surface: HTMLCanvasElement,
  layout: OfficeLayout | null,
  bounds: LayoutBounds,
  scene: Scene,
  images: Record<string, HTMLImageElement>,
  elapsedMs: number,
): void {
  context.imageSmoothingEnabled = false
  context.fillStyle = '#0a0d12'
  context.fillRect(0, 0, surface.width, surface.height)
  if (!layout) return

  drawTiles(context, layout, bounds, images)
  const drawables: Drawable[] = []
  layout.furniture.forEach((item, order) => {
    const asset = furnitureAsset(item.type)
    const image = asset ? images[asset.url] : undefined
    const localY = (item.row - bounds.minRow) * FLOOR_TILE_SIZE
    drawables.push({ kind: 'furniture', z: localY + (image?.naturalHeight ?? 32), order, item })
  })
  scene.characters.forEach((character, order) => {
    drawables.push({ kind: 'character', z: character.y + character.height, order, character })
  })
  drawables.sort(
    (a, b) =>
      a.z - b.z ||
      (a.kind === b.kind ? a.order - b.order : a.kind === 'character' ? -1 : 1),
  )
  for (const drawable of drawables) {
    if (drawable.kind === 'furniture') drawFurniture(context, drawable.item, bounds, images)
    else drawCharacter(context, drawable.character, images, elapsedMs)
  }
  for (const character of scene.characters) drawLabel(context, character)
}

function drawTiles(
  context: CanvasRenderingContext2D,
  layout: OfficeLayout,
  bounds: LayoutBounds,
  images: Record<string, HTMLImageElement>,
): void {
  const size = FLOOR_TILE_SIZE * SCALE
  layout.tiles.forEach((tile, index) => {
    if (tile === VOID_TILE) return
    const url = tileUrl(tile)
    const image = url ? images[url] : undefined
    if (!image) return
    const col = index % layout.cols
    const row = Math.floor(index / layout.cols)
    const x = (col - bounds.minCol) * size
    const y = (row - bounds.minRow) * size
    context.drawImage(image, 0, 0, FLOOR_TILE_SIZE, FLOOR_TILE_SIZE, x, y, size, size)
  })
}

function drawFurniture(
  context: CanvasRenderingContext2D,
  item: OfficeLayout['furniture'][number],
  bounds: LayoutBounds,
  images: Record<string, HTMLImageElement>,
): void {
  const asset = furnitureAsset(item.type)
  const image = asset ? images[asset.url] : undefined
  if (!asset || !image) return
  const x = (item.col - bounds.minCol) * FLOOR_TILE_SIZE * SCALE
  const y = (item.row - bounds.minRow) * FLOOR_TILE_SIZE * SCALE
  const width = image.naturalWidth * SCALE
  const height = image.naturalHeight * SCALE
  if (!asset.mirror) {
    context.drawImage(image, x, y, width, height)
    return
  }
  context.save()
  context.translate(x + width, y)
  context.scale(-1, 1)
  context.drawImage(image, 0, 0, width, height)
  context.restore()
}

function drawCharacter(
  context: CanvasRenderingContext2D,
  character: PlacedCharacter,
  images: Record<string, HTMLImageElement>,
  elapsedMs: number,
): void {
  const sheet = images[characterUrl(character.character)]
  if (!sheet) return
  const frames = POSE_FRAMES[character.pose]
  const frame = frameAt(character.pose, elapsedMs, frames)
  const { sx, sy, sw, sh } = frameRect('down', frame)
  const x = character.x * SCALE
  const y = character.y * SCALE
  context.fillStyle = 'rgba(0,0,0,0.28)'
  context.beginPath()
  context.ellipse(x + (sw * SCALE) / 2, y + sh * SCALE - 2, sw * SCALE * 0.4, 4, 0, 0, Math.PI * 2)
  context.fill()
  context.drawImage(sheet, sx, sy, sw, sh, x, y, sw * SCALE, sh * SCALE)
}

function drawLabel(context: CanvasRenderingContext2D, character: PlacedCharacter): void {
  const centre = (character.x + character.width / 2) * SCALE
  const y = Math.max(11, character.y * SCALE - 5)
  context.font = '10px ui-monospace, monospace'
  context.textAlign = 'center'
  const width = Math.ceil(context.measureText(character.agent).width) + 8
  context.fillStyle = 'rgba(7,10,14,0.84)'
  context.fillRect(Math.round(centre - width / 2), y - 10, width, 13)
  context.fillStyle = '#f2f4f8'
  context.fillText(character.agent, centre, y)
}

function fallbackBounds(): LayoutBounds {
  return { minCol: 0, minRow: 0, maxCol: 20, maxRow: 12, width: 320, height: 192 }
}
