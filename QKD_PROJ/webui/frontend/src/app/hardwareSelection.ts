export type HardwareSelection = { board_id: string; revision: string }
export const HARDWARE_STORAGE_KEY = 'qkd.hardware-selection.v1'
const identifier = /^[a-z0-9][a-z0-9_.-]{0,63}$/

export function parseHardwareSelection(raw: string | null): HardwareSelection | undefined {
  try {
    const value = JSON.parse(raw ?? 'null')
    if (value && typeof value.board_id === 'string' && identifier.test(value.board_id)
      && typeof value.revision === 'string' && identifier.test(value.revision)) {
      return { board_id: value.board_id, revision: value.revision }
    }
  } catch { /* A stale browser preference does not prevent a new submission. */ }
  return undefined
}

export function hardwareKey(value?: HardwareSelection | null): string {
  return value ? `${value.board_id}@${value.revision}` : ''
}

export function withHardwareSelection<T extends Record<string, unknown>>(frame: T, selection?: HardwareSelection) {
  return selection ? { ...frame, hardwareSelection: { ...selection } } : frame
}
