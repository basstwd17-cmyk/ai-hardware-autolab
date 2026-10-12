import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ExplorerGraph } from './explorer'
import { buildConstellation, fitStars, hitStar, starColour, zoomAt, type Camera, type Star } from './constellation'
import { searchTopicClaims } from './mindmap'
import { TOPIC_ROOT } from './topics'

export function ConstellationMap({graph, query, selectedId, onSelect}: {
  graph: ExplorerGraph; query: string; selectedId: string | null; onSelect: (node: Star) => void
}) {
  const model = useMemo(() => buildConstellation(graph), [graph])
  const canvas = useRef<HTMLCanvasElement>(null)
  const host = useRef<HTMLDivElement>(null)
  const camera = useRef<Camera>({x: 0, y: 0, scale: 1})
  const size = useRef({width: 0, height: 0})
  const frame = useRef(0)
  const drag = useRef<{pointer: number; x: number; y: number; moved: boolean} | null>(null)
  const hover = useRef<string | null>(null)
  const [hoverId, setHoverId] = useState<string | null>(null)
  const [zoom, setZoom] = useState(100)
  const matches = useMemo(() => new Set(searchTopicClaims(graph, query).map(c => c.claimId)), [graph, query])
  const current = useRef({model, query, selectedId, matches})
  current.current = {model, query, selectedId, matches}
  const hovered = model.byId.get(hoverId ?? '')
  const selection = model.byId.get(selectedId ?? '')
  const roots = model.nodes.filter(n => n.depth === 1 && n.navigation)

  const paint = useCallback(() => {
    const ctx = canvas.current?.getContext('2d')
    if (!ctx) return
    const {width, height} = size.current
    const dpr = window.devicePixelRatio || 1
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)
    const {model: map, query: text, selectedId: selected, matches: found} = current.current
    const cam = camera.current
    const active = map.byId.get(hover.current ?? selected ?? '')
    const related = new Set(active?.claimIds ?? [])
    const searching = Boolean(text.trim())
    const emphasised = (n: Star) => n.id === active?.id
      || (searching ? (n.claimId ? found.has(n.claimId) : n.label.toLocaleLowerCase().includes(text.trim().toLocaleLowerCase()))
        : active ? n.claimIds.some(id => related.has(id)) : false)
    const point = (n: Star) => ({x: n.x * cam.scale + cam.x, y: n.y * cam.scale + cam.y})
    const gradient = ctx.createRadialGradient(width * .52, height * .5, 0, width * .52, height * .5, Math.max(width, height) * .7)
    gradient.addColorStop(0, '#101d2b'); gradient.addColorStop(1, '#09111d')
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, width, height)
    for (const link of map.links) {
      const from = map.byId.get(link.source)!, to = map.byId.get(link.target)!
      const lit = emphasised(from) && emphasised(to)
      if (link.secondary && (!lit || !active)) continue
      const a = point(from), b = point(to)
      if ((a.x < 0 && b.x < 0) || (a.x > width && b.x > width)
          || (a.y < 0 && b.y < 0) || (a.y > height && b.y > height)) continue
      ctx.globalAlpha = lit ? .52 : active || searching ? .035 : to.navigation ? .22 : .15
      ctx.strokeStyle = starColour(to.category)
      ctx.lineWidth = lit ? 1.2 : .65
      ctx.setLineDash(link.secondary ? [3, 4] : [])
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke()
    }
    ctx.setLineDash([])
    for (const n of map.nodes) {
      const p = point(n)
      if (p.x < -40 || p.y < -40 || p.x > width + 40 || p.y > height + 40) continue
      const lit = emphasised(n)
      const root = n.id === TOPIC_ROOT
      const colour = root ? '#ffe9bd' : starColour(n.category)
      const r = root ? 6.5 : n.navigation ? n.depth === 1 ? 3.5 : 2.2 : Math.max(1.1, Math.min(2.3, cam.scale * 1.5))
      if ((n.navigation && (n.depth <= 1 || cam.scale > .5)) || lit) {
        const radius = root ? 65 : n.navigation ? 24 : 12
        const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, radius)
        glow.addColorStop(0, colour + (root ? '60' : '35')); glow.addColorStop(1, colour + '00')
        ctx.globalAlpha = active || searching ? lit || root ? .9 : .28 : .9
        ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, Math.PI * 2); ctx.fill()
      }
      ctx.globalAlpha = lit || root ? 1 : active || searching ? .14 : n.navigation ? .85 : .55
      ctx.fillStyle = colour
      ctx.beginPath(); ctx.arc(p.x, p.y, lit ? r + .8 : r, 0, Math.PI * 2); ctx.fill()
      if (n.id === selected || n.id === hover.current) {
        ctx.globalAlpha = .9; ctx.strokeStyle = '#edf5ff'; ctx.lineWidth = 1
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 4, 0, Math.PI * 2); ctx.stroke()
      }
    }
    // Major topics are always named. Zoom reveals smaller hubs; collision
    // avoidance keeps labels readable instead of turning every point into text.
    const boxes: Array<{x: number; y: number; w: number; h: number}> = []
    const labels = map.nodes.filter(n => n.navigation || n.id === selected || n.id === hover.current)
      .sort((a, b) => Number(b.id === active?.id) - Number(a.id === active?.id) || a.depth - b.depth)
    for (const n of labels) {
      const lit = emphasised(n), root = n.id === TOPIC_ROOT
      if (n.depth > 1 && !lit && cam.scale < .55 && n.clusterRadius * cam.scale < 25) continue
      const p = point(n)
      const label = n.label.length > 33 ? n.label.slice(0, 32) + '…' : n.label
      const fontSize = root ? 15 : n.depth <= 1 || lit ? 13 : 11
      ctx.font = `${root || lit ? 600 : 400} ${fontSize}px system-ui, sans-serif`
      const w = ctx.measureText(label).width + 12, h = lit && n.navigation ? 38 : 22
      let box = {x: p.x - w / 2, y: p.y + 11, w, h}
      if (box.x < 6 || box.x + w > width - 6 || box.y < 4 || box.y + h > height - 28) continue
      const overlaps = (b: typeof box) => boxes.some(a => b.x < a.x + a.w && b.x + b.w > a.x && b.y < a.y + a.h && b.y + b.h > a.y)
      if (overlaps(box)) box = {...box, y: p.y - h - 10}
      if (overlaps(box)) continue
      boxes.push(box)
      ctx.globalAlpha = root || lit ? 1 : active || searching ? .3 : n.depth <= 1 ? .82 : .58
      ctx.textAlign = 'center'; ctx.textBaseline = 'top'
      ctx.shadowColor = '#09111d'; ctx.shadowBlur = 6
      ctx.fillStyle = root ? '#f4e6cd' : lit ? '#e7f3ff' : '#a0b8d0'
      ctx.fillText(label, p.x, box.y + 3)
      if (lit && n.navigation) {
        ctx.font = '10px system-ui, sans-serif'; ctx.fillStyle = '#8fbcdf'
        ctx.fillText(`${n.claimIds.length}개 관련 기록`, p.x, box.y + 21)
      }
      ctx.shadowBlur = 0
    }
    ctx.globalAlpha = 1
  }, [])

  const redraw = useCallback(() => {
    cancelAnimationFrame(frame.current)
    frame.current = requestAnimationFrame(paint)
  }, [paint])
  const fit = useCallback(() => {
    camera.current = fitStars(current.current.model.nodes, size.current.width, size.current.height)
    setZoom(Math.round(camera.current.scale * 100)); redraw()
  }, [redraw])
  const focus = useCallback((node: Star) => {
    const {width, height} = size.current
    const scale = Math.min(5, Math.max(.4, Math.min(width, height) / (2.5 * Math.max(50, node.clusterRadius))))
    camera.current = {x: width / 2 - node.x * scale, y: height / 2 - node.y * scale, scale}
    setZoom(Math.round(scale * 100)); redraw()
  }, [redraw])
  const changeZoom = (factor: number) => {
    camera.current = zoomAt(camera.current, size.current.width / 2, size.current.height / 2, factor)
    setZoom(Math.round(camera.current.scale * 100)); redraw()
  }

  useEffect(() => {
    const element = host.current, target = canvas.current
    if (!element || !target) return
    const resize = new ResizeObserver(([entry]) => {
      const {width, height} = entry.contentRect
      const previous = size.current
      size.current = {width, height}
      const dpr = window.devicePixelRatio || 1
      target.width = Math.round(width * dpr); target.height = Math.round(height * dpr)
      if (!previous.width || !previous.height) fit()
      else {
        camera.current.x += (width - previous.width) / 2
        camera.current.y += (height - previous.height) / 2
        redraw()
      }
    })
    resize.observe(element)
    const wheel = (event: WheelEvent) => {
      event.preventDefault()
      const box = target.getBoundingClientRect()
      camera.current = zoomAt(camera.current, event.clientX - box.left, event.clientY - box.top,
        Math.exp(-Math.max(-120, Math.min(120, event.deltaY)) * .003))
      setZoom(Math.round(camera.current.scale * 100)); redraw()
    }
    target.addEventListener('wheel', wheel, {passive: false})
    return () => { resize.disconnect(); target.removeEventListener('wheel', wheel); cancelAnimationFrame(frame.current) }
  }, [fit, redraw])

  const previousModel = useRef(model)
  useEffect(() => {
    if (previousModel.current !== model) {
      previousModel.current = model
      hover.current = null; setHoverId(null)
      fit()
    } else redraw()
  }, [model, query, selectedId, fit, redraw])

  return <div className="constellation-map" ref={host}>
    <canvas ref={canvas} tabIndex={0} aria-label="전체 지식맵. 휠로 확대, 드래그로 이동. 방향키로 이동, 더하기·빼기로 확대·축소, Home으로 전체 보기."
      onKeyDown={event => {
        if (['+', '=', '-', 'Home', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) event.preventDefault()
        if (event.key === '+' || event.key === '=') changeZoom(1.3)
        else if (event.key === '-') changeZoom(1 / 1.3)
        else if (event.key === 'Home') fit()
        else {
          camera.current.x += event.key === 'ArrowLeft' ? 50 : event.key === 'ArrowRight' ? -50 : 0
          camera.current.y += event.key === 'ArrowUp' ? 50 : event.key === 'ArrowDown' ? -50 : 0
          redraw()
        }
      }}
      onPointerDown={event => {
        if (event.button !== 0 || drag.current) return
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = {pointer: event.pointerId, x: event.clientX, y: event.clientY, moved: false}
      }}
      onPointerMove={event => {
        const d = drag.current
        if (d) {
          if (d.pointer !== event.pointerId) return
          const dx = event.clientX - d.x, dy = event.clientY - d.y
          if (d.moved || Math.hypot(dx, dy) > 3) {
            camera.current.x += dx; camera.current.y += dy
            d.x = event.clientX; d.y = event.clientY; d.moved = true
            hover.current = null; setHoverId(null); redraw()
          }
          return
        }
        const rect = event.currentTarget.getBoundingClientRect()
        const hit = hitStar(model.nodes, camera.current, event.clientX - rect.left, event.clientY - rect.top)
        if (hover.current !== (hit?.id ?? null)) {
          hover.current = hit?.id ?? null; setHoverId(hit?.id ?? null); redraw()
        }
      }}
      onPointerUp={event => {
        const d = drag.current
        if (!d || d.pointer !== event.pointerId) return
        drag.current = null
        event.currentTarget.releasePointerCapture(event.pointerId)
        if (!d.moved) {
          const rect = event.currentTarget.getBoundingClientRect()
          const hit = hitStar(model.nodes, camera.current, event.clientX - rect.left, event.clientY - rect.top)
          if (hit) onSelect(hit)
        }
      }}
      onPointerCancel={() => { drag.current = null }}
      onPointerLeave={() => { hover.current = null; setHoverId(null); redraw() }}
      onDoubleClick={event => {
        const rect = event.currentTarget.getBoundingClientRect()
        const hit = hitStar(model.nodes, camera.current, event.clientX - rect.left, event.clientY - rect.top)
        if (hit) focus(hit)
      }}
      style={{cursor: hoverId ? 'pointer' : 'grab'}} />
    <nav className="constellation-legend" aria-label="지식 주제 바로가기">
      {roots.map(n => <button key={n.id} onClick={() => {onSelect(n); focus(n)}}>
        <i style={{background: starColour(n.category)}} />{n.label}
      </button>)}
    </nav>
    <div className="constellation-controls">
      <button aria-label="지식맵 확대" onClick={() => changeZoom(1.4)}>＋</button>
      <button aria-label="지식맵 축소" onClick={() => changeZoom(1 / 1.4)}>−</button>
      <button onClick={fit}>전체 보기</button>
      <button disabled={!selection} onClick={() => { if (selection) focus(selection) }}>선택 확대</button>
      <span>{zoom}%</span>
    </div>
    {hovered && <div className="constellation-tooltip" role="status">
      <strong>{hovered.label.length > 160 ? hovered.label.slice(0, 159) + '…' : hovered.label}</strong>
      <span>{hovered.navigation ? `${hovered.claimIds.length}개 관련 기록 · 두 번 눌러 확대` : '눌러서 내용·검증 상태·근거 확인'}</span>
    </div>}
  </div>
}
