/**
 * The client half of the stream contract, and it is mostly about giving up.
 *
 * The backend's rules (see `backend/channels.py` and `backend/wire.py`) put
 * three obligations on this side, and every one of them is about *not*
 * rendering something that looks current and is not:
 *
 * 1. **`webui.resync_required` means the stream had a hole.** The server has
 *    already closed the socket by the time this matters, but the ordering is
 *    what counts: nothing that arrived after the notice may be applied on top
 *    of what came before it. So the notice sets a flag, the socket is dropped,
 *    and the next connection starts from a freshly fetched snapshot's cursor.
 * 2. **A cursor is the only thing worth resuming from.** Reconnecting with
 *    `after=0` would replay the whole ledger every time a laptop slept.
 *    Reconnecting with a cursor the page never actually applied would skip
 *    history. So the cursor comes from the snapshot the page is holding.
 * 3. **An old frame is not news.** Duplicates are expected on reconnect --
 *    replay is idempotent by design -- so a frame at or below the applied
 *    cursor is dropped here rather than double-counted.
 *
 * Reconnection backoff is deliberately short and capped. The daemon is a local
 * process the user starts and stops; a minute of exponential politeness would
 * just mean the page looks broken after they restart it.
 *
 * **Every connection carries an epoch.** A `WebSocket` that has been replaced
 * still fires `onclose` — closing is asynchronous, and the browser delivers it
 * whenever the handshake finishes, which can be after a new socket is already
 * open. Without an epoch that late `onclose` schedules a *second* reconnect,
 * and from then on two sockets race: both receive the stream, both advance the
 * same cursor, and one of them drops rows the other applied. The epoch makes a
 * handler from a superseded connection a no-op, which is the only version of
 * this that stays correct when the daemon restarts under a slow network.
 */

export type Frame = { type: string; [key: string]: unknown }

export interface SocketHooks {
  /** Every frame that survives the duplicate check, in arrival order. */
  onFrame(frame: Frame): void
  /** Connected, disconnected, or waiting to retry. */
  onOpen(): void
  onClose(reason: string): void
  /** The stream had a hole. Re-read the snapshots before reconnecting. */
  onResync(cursor: number, dropped: number): void
}

const FIRST_RETRY_MS = 250
const MAX_RETRY_MS = 5000

export class PlatformSocket {
  private socket: WebSocket | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private retry = FIRST_RETRY_MS
  private stopped = false
  private applied = 0
  // Bumped for every connection attempt and by `close()`. A handler compares
  // the epoch it was created under against this; anything older is a ghost.
  private epoch = 0
  // Set the instant a hole is declared, and cleared only by an explicit
  // reconnect. `close()` is asynchronous -- messages already buffered can still
  // reach `onmessage` while the closing handshake runs -- so refusing them has
  // to be this side's decision rather than a consequence of the socket
  // shutting. Applying one would put a frame from after the hole on top of
  // state from before it.
  private holed = false

  constructor(
    private readonly url: (after: number) => string,
    private readonly hooks: SocketHooks,
  ) {}

  /**
   * Open, or reopen, from `after`.
   *
   * Called on mount and again after a resync, with the cursor of whatever
   * snapshot the page has just fetched.
   */
  connect(after: number): void {
    this.stopped = false
    this.holed = false
    this.applied = after
    this.open()
  }

  close(): void {
    this.stopped = true
    this.epoch += 1 // orphan every handler still attached to the old socket
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
    this.socket?.close()
    this.socket = null
  }

  private open(): void {
    if (this.stopped) return
    // Close whatever is still attached before opening the next one, so two
    // sockets are never subscribed at once even briefly.
    this.socket?.close()
    const epoch = ++this.epoch
    const socket = new WebSocket(this.url(this.applied))
    this.socket = socket

    socket.onopen = () => {
      if (epoch !== this.epoch) return
      this.retry = FIRST_RETRY_MS
      this.hooks.onOpen()
    }

    socket.onmessage = (message) => {
      if (epoch !== this.epoch) return // a superseded socket applies nothing
      let frame: Frame
      try {
        frame = JSON.parse(String(message.data)) as Frame
      } catch {
        return // a frame this page cannot parse is not a frame it can apply
      }
      this.receive(frame)
    }

    socket.onclose = (event) => {
      // The one that matters: a late close from a replaced socket must not
      // report a disconnection the page has already recovered from, and must
      // not schedule a reconnect on top of the live one.
      if (epoch !== this.epoch) return
      this.socket = null
      this.hooks.onClose(event.reason || `closed (${event.code})`)
      this.schedule()
    }

    socket.onerror = () => {
      // `onclose` always follows, and it carries the code. Nothing to add.
    }
  }

  private receive(frame: Frame): void {
    if (this.holed) return // nothing after the notice may be applied

    if (frame.type === 'webui.resync_required') {
      const cursor = typeof frame.cursor === 'number' ? frame.cursor : this.applied
      const dropped = typeof frame.dropped === 'number' ? frame.dropped : 0
      // Stop applying immediately. The server is closing this socket; the
      // page must not treat what came before and after the notice as
      // consecutive, and the owner will call `connect` again with a cursor
      // from a snapshot it has actually read.
      this.holed = true
      this.stopped = true
      this.epoch += 1 // nothing this socket says afterwards counts
      this.socket?.close()
      this.socket = null
      this.hooks.onResync(cursor, dropped)
      return
    }

    if (frame.type === 'ledger.event') {
      const event = frame.event as { event_id?: number } | undefined
      const id = event?.event_id
      if (typeof id === 'number') {
        if (id <= this.applied) return // replay is idempotent; this is not news
        this.applied = id
      }
    }

    this.hooks.onFrame(frame)
  }

  private schedule(): void {
    if (this.stopped) return
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = setTimeout(() => this.open(), this.retry)
    this.retry = Math.min(this.retry * 2, MAX_RETRY_MS)
  }

  /** What the next connection would resume from. Exposed for tests. */
  get cursor(): number {
    return this.applied
  }

  /** How many connections have been opened or orphaned. For tests. */
  get generation(): number {
    return this.epoch
  }
}
