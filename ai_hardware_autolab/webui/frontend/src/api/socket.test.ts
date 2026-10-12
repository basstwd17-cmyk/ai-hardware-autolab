/**
 * The client half of the stream contract, against a fake WebSocket.
 *
 * These assert the three obligations the backend puts on this side: stop on a
 * resync, resume from a cursor, and drop a replayed row rather than count it
 * twice. Each one, got wrong, produces a page that looks current and is not --
 * which is the only failure mode worth writing tests for here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PlatformSocket, type Frame } from './socket'

class FakeSocket {
  static last: FakeSocket | null = null
  static opened: string[] = []

  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number; reason: string }) => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(readonly url: string) {
    FakeSocket.last = this
    FakeSocket.opened.push(url)
  }

  close(): void {
    this.closed = true
  }

  send(frame: Frame): void {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

function harness() {
  const frames: Frame[] = []
  const resyncs: Array<{ cursor: number; dropped: number }> = []
  const closes: string[] = []
  const socket = new PlatformSocket((after) => `ws://host/ws?after=${after}`, {
    onFrame: (frame) => frames.push(frame),
    onOpen: () => {},
    onClose: (reason) => closes.push(reason),
    onResync: (cursor, dropped) => resyncs.push({ cursor, dropped }),
  })
  return { socket, frames, resyncs, closes }
}

function event(id: number): Frame {
  return { type: 'ledger.event', event: { event_id: id, job_id: 'j' } }
}

beforeEach(() => {
  FakeSocket.last = null
  FakeSocket.opened = []
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('resuming', () => {
  it('asks for what it has not applied, not for everything', () => {
    const { socket } = harness()
    socket.connect(41)
    expect(FakeSocket.opened.at(-1)).toContain('after=41')

    FakeSocket.last!.send(event(42))
    expect(socket.cursor).toBe(42)

    // A drop and a retry resumes from what was applied, not from the start.
    FakeSocket.last!.onclose?.({ code: 1006, reason: 'gone' })
    vi.advanceTimersByTime(300)
    expect(FakeSocket.opened.at(-1)).toContain('after=42')
  })
})

describe('duplicates and omissions', () => {
  it('drops a row it has already applied', () => {
    const { socket, frames } = harness()
    socket.connect(0)
    FakeSocket.last!.send(event(1))
    FakeSocket.last!.send(event(2))
    FakeSocket.last!.send(event(2)) // replay after a reconnect
    FakeSocket.last!.send(event(1))

    expect(frames.map((f) => (f.event as { event_id: number }).event_id)).toEqual([1, 2])
    expect(socket.cursor).toBe(2)
  })

  it('stops applying the moment the stream admits a hole', () => {
    const { socket, frames, resyncs } = harness()
    socket.connect(0)
    const live = FakeSocket.last!
    live.send(event(1))
    live.send({ type: 'webui.resync_required', cursor: 90, dropped: 517 })
    // The server closes too, but the page must not depend on that landing
    // first: anything after the notice is refused here.
    live.send(event(91))

    expect(resyncs).toEqual([{ cursor: 90, dropped: 517 }])
    expect(frames.map((f) => f.type)).toEqual(['ledger.event'])
    expect(live.closed).toBe(true)
  })

  it('does not reconnect on its own after a resync', () => {
    const { socket } = harness()
    socket.connect(0)
    FakeSocket.last!.send({ type: 'webui.resync_required', cursor: 5, dropped: 1 })
    const opened = FakeSocket.opened.length
    vi.advanceTimersByTime(10_000)
    // The owner reconnects, and only once it holds a snapshot to rebase on.
    // An automatic retry here would resume from a cursor nothing had read.
    expect(FakeSocket.opened.length).toBe(opened)
  })
})

describe('surviving the daemon', () => {
  it('backs off, and stops when told to', () => {
    const { socket, closes } = harness()
    socket.connect(0)
    FakeSocket.last!.onclose?.({ code: 1006, reason: 'daemon restarting' })
    expect(closes).toEqual(['daemon restarting'])

    vi.advanceTimersByTime(250)
    expect(FakeSocket.opened.length).toBe(2)

    socket.close()
    vi.advanceTimersByTime(10_000)
    expect(FakeSocket.opened.length).toBe(2)
  })

  it('ignores a frame it cannot parse rather than dying on it', () => {
    const { socket, frames } = harness()
    socket.connect(0)
    FakeSocket.last!.onmessage?.({ data: 'not json' })
    FakeSocket.last!.send(event(1))
    expect(frames).toHaveLength(1)
  })
})

describe('a replaced socket', () => {
  it('does not schedule a second reconnect when its close arrives late', () => {
    const { socket, closes } = harness()
    socket.connect(0)
    const first = FakeSocket.last!

    // The daemon dropped; the retry fires and a second socket opens.
    first.onclose?.({ code: 1006, reason: 'daemon restarting' })
    vi.advanceTimersByTime(300)
    const second = FakeSocket.last!
    expect(second).not.toBe(first)
    expect(FakeSocket.opened.length).toBe(2)

    // Now the *first* socket's close finally lands. Browsers deliver this
    // whenever the closing handshake finishes, which can be after the
    // replacement is already open.
    first.onclose?.({ code: 1006, reason: 'late close of a dead socket' })
    vi.advanceTimersByTime(10_000)

    expect(FakeSocket.opened.length).toBe(2)
    expect(closes).toEqual(['daemon restarting'])
    expect(second.closed).toBe(false)
  })

  it('applies nothing that arrives on a socket it has replaced', () => {
    const { socket, frames } = harness()
    socket.connect(0)
    const first = FakeSocket.last!
    first.onclose?.({ code: 1006, reason: 'gone' })
    vi.advanceTimersByTime(300)
    const second = FakeSocket.last!

    first.send(event(99)) // a straggler from the dead connection
    second.send(event(1))

    expect(frames.map((f) => (f.event as { event_id: number }).event_id)).toEqual([1])
    expect(socket.cursor).toBe(1)
  })

  it('closes the previous socket before opening the next', () => {
    const { socket } = harness()
    socket.connect(0)
    const first = FakeSocket.last!
    socket.connect(5) // what a resync does
    expect(first.closed).toBe(true)
    expect(FakeSocket.opened.at(-1)).toContain('after=5')
  })

  it('orphans its handlers when closed for good', () => {
    const { socket, closes } = harness()
    socket.connect(0)
    const live = FakeSocket.last!
    socket.close()

    live.onclose?.({ code: 1000, reason: 'after close()' })
    vi.advanceTimersByTime(10_000)
    expect(closes).toEqual([])
    expect(FakeSocket.opened.length).toBe(1)
  })
})
