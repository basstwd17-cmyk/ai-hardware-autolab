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
  furnitureDepth,
  layoutAssetUrls,
  layoutBounds,
  parseOfficeLayout,
  tileUrl,
  type LayoutBounds,
  type OfficeLayout,
} from './layout'
import {
  buildScene,
  characterDepth,
  frameAt,
  layoutLabelBoxes,
  motionAt,
  type CharacterMotion,
  type PlacedCharacter,
  type Scene,
} from './scene'
import {
  CHAR_COUNT,
  FLOOR_TILE_SIZE,
  POSE_FRAMES,
  characterUrl,
  frameRect,
} from './sprites'
import { shortTeamLabel, teamDisplayName } from '../app/agentTeams'
import { buildNavigation, OfficeWalkers } from './navigation'

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
  const animationOrigin = useRef(performance.now())
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
  const walkers = useMemo(() => layout ? new OfficeWalkers(buildNavigation(layout, bounds)) : null, [layout, bounds])
  const paintedMotions = useRef(new Map<string, CharacterMotion>())

  useEffect(() => {
    const surface = canvas.current
    if (!surface) return
    const context = surface.getContext('2d')
    if (!context) return
    let frame = 0
    const started = animationOrigin.current
    const paint = (now: number) => {
      const motions = walkers?.sample(scene.characters, now) ?? new Map<string, CharacterMotion>()
      paintedMotions.current = motions
      draw(context, surface, layout, bounds, scene, images, now - started, motions)
      frame = requestAnimationFrame(paint)
    }
    frame = requestAnimationFrame(paint)
    return () => cancelAnimationFrame(frame)
  }, [layout, bounds, scene, images, walkers])

  const pick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const surface = canvas.current
    if (!surface) return
    const box = surface.getBoundingClientRect()
    const x = ((event.clientX - box.left) / box.width) * bounds.width
    const y = ((event.clientY - box.top) / box.height) * bounds.height
    const elapsedMs = performance.now() - animationOrigin.current
    setHovered(scene.characters.find((character) => {
      const motion = paintedMotions.current.get(character.id) ?? motionAt(character, elapsedMs)
      return (
        x >= motion.x - 8 &&
        x <= motion.x + character.width + 8 &&
        y >= motion.y - 4 &&
        y <= motion.y + character.height + 10
      )
    }) ?? null)
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
          <b className="who" data-agent={hovered.agent}>{teamDisplayName(hovered.agent)}</b>
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
  | {
      kind: 'character'
      z: number
      order: number
      character: PlacedCharacter
      motion: CharacterMotion
    }

function draw(
  context: CanvasRenderingContext2D,
  surface: HTMLCanvasElement,
  layout: OfficeLayout | null,
  bounds: LayoutBounds,
  scene: Scene,
  images: Record<string, HTMLImageElement>,
  elapsedMs: number,
  motions: Map<string, CharacterMotion>,
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
    drawables.push({ kind: 'furniture', z: furnitureDepth(item, bounds, image?.naturalHeight ?? 32), order, item })
  })
  scene.characters.forEach((character, order) => {
    const motion = motions.get(character.id) ?? motionAt(character, elapsedMs)
    drawables.push({
      kind: 'character',
      z: characterDepth(character, motion),
      order,
      character,
      motion,
    })
  })
  drawables.sort(
    (a, b) =>
      a.z - b.z ||
      (a.kind === b.kind ? a.order - b.order : a.kind === 'character' ? -1 : 1),
  )
  for (const drawable of drawables) {
    if (drawable.kind === 'furniture') drawFurniture(context, drawable.item, bounds, images)
    else drawCharacter(context, drawable.character, drawable.motion, images, elapsedMs)
  }
  drawLabels(context, surface, scene.characters, elapsedMs, motions)
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
  motion: CharacterMotion,
  images: Record<string, HTMLImageElement>,
  elapsedMs: number,
): void {
  const sheet = images[characterUrl(character.character)]
  if (!sheet) return
  const frames = POSE_FRAMES[motion.pose]
  const frame = frameAt(motion.pose, elapsedMs, frames)
  const { sx, sy, sw, sh, flip } = frameRect(motion.direction, frame)
  const x = motion.x * SCALE
  const y = motion.y * SCALE
  context.fillStyle = 'rgba(0,0,0,0.28)'
  context.beginPath()
  context.ellipse(x + (sw * SCALE) / 2, y + sh * SCALE - 2, sw * SCALE * 0.4, 4, 0, 0, Math.PI * 2)
  context.fill()
  if (flip) {
    context.save()
    context.translate(x + sw * SCALE, y)
    context.scale(-1, 1)
    context.drawImage(sheet, sx, sy, sw, sh, 0, 0, sw * SCALE, sh * SCALE)
    context.restore()
  } else {
    context.drawImage(sheet, sx, sy, sw, sh, x, y, sw * SCALE, sh * SCALE)
  }
  context.fillStyle = activityColor(character.activity)
  context.fillRect(x + sw * SCALE - 7, y + 3, 5, 5)
}

function drawLabels(
  context: CanvasRenderingContext2D,
  surface: HTMLCanvasElement,
  characters: PlacedCharacter[],
  elapsedMs: number,
  motions: Map<string, CharacterMotion>,
): void {
  context.font = 'bold 8px ui-monospace, monospace'
  context.textAlign = 'center'
  const labels = characters.map((character) => {
    const motion = motions.get(character.id) ?? motionAt(character, elapsedMs)
    const text = shortTeamLabel(character.agent)
    return {
      character,
      motion,
      text,
      id: character.id,
      anchorX: (motion.x + character.width / 2) * SCALE,
      anchorY: motion.y * SCALE,
      width: Math.ceil(context.measureText(text).width) + 10,
      height: 12,
    }
  })
  const boxes = layoutLabelBoxes(labels, surface.width, surface.height)
  for (const [index, box] of boxes.entries()) {
    const source = labels[index]
    const boxCentre = box.x + box.width / 2
    const boxBottom = box.y + box.height
    if (Math.abs(boxCentre - box.anchorX) > 2 || Math.abs(boxBottom - box.anchorY) > 10) {
      context.strokeStyle = 'rgba(242,244,248,0.48)'
      context.lineWidth = 1
      context.beginPath()
      context.moveTo(Math.round(boxCentre), Math.round(boxBottom))
      context.lineTo(Math.round(box.anchorX), Math.round(box.anchorY - 2))
      context.stroke()
    }
    context.fillStyle = 'rgba(7,10,14,0.86)'
    context.fillRect(box.x, box.y, box.width, box.height)
    context.fillStyle = activityColor(source.character.activity)
    context.fillRect(box.x, box.y, 2, box.height)
    context.fillStyle = '#f2f4f8'
    context.fillText(source.text, boxCentre, box.y + 9)
  }
}

function activityColor(activity: string): string {
  if (activity === 'running' || activity === 'typing') return '#47d7a1'
  if (activity === 'waiting') return '#f6c453'
  if (activity === 'reading' || activity === 'delegating') return '#6db6ff'
  return '#8895a7'
}

function fallbackBounds(): LayoutBounds {
  return { minCol: 0, minRow: 0, maxCol: 20, maxRow: 12, width: 320, height: 192 }
}
