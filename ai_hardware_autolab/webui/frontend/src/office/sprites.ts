/**
 * The geometry of the vendored sprite sheets, and where each number came from.
 *
 * Every constant here is read out of upstream Pixel Agents, not measured off a
 * screenshot and not guessed. A renderer that slices a sheet on the wrong grid
 * produces garbage that still draws, which is the failure that survives review
 * longest — so the sources are named:
 *
 * * `core/src/assets/constants.ts` — `CHAR_FRAME_W = 16`, `CHAR_FRAME_H = 32`,
 *   `CHAR_FRAMES_PER_ROW = 7`, `CHAR_COUNT = 6`, `FLOOR_TILE_SIZE = 16`.
 * * `core/src/assets/pngDecoder.ts` — each `char_N.png` is 112×96: three
 *   direction rows (down, up, right) of seven 16×32 frames.
 * * `webview-ui/src/office/sprites/spriteData.ts` — what those seven frames
 *   *are*: 0–2 the walk cycle, 3–4 typing, 5–6 reading, and LEFT is RIGHT
 *   drawn flipped.
 *
 * Verified against the vendored files: `char_0.png` is 112×96 and
 * `floor_0.png` is 16×16, which is what `MANIFEST.json` hashed.
 */

/** Where the assets are served from. Nothing is fetched from anywhere else. */
export const ASSET_BASE = '/office-assets'

export const CHAR_FRAME_W = 16
export const CHAR_FRAME_H = 32
export const CHAR_FRAMES_PER_ROW = 7
export const CHAR_COUNT = 6
export const FLOOR_TILE_SIZE = 16

/** Row order inside a character sheet. LEFT is not a row; it is RIGHT flipped. */
export const DIRECTION_ROWS = ['down', 'up', 'right'] as const
export type Direction = (typeof DIRECTION_ROWS)[number] | 'left'

/**
 * The three poses the sheet actually contains.
 *
 * There are six activities and three poses, so some activities share one. That
 * is stated rather than papered over: the label under each character carries
 * the exact activity, so nothing the projection said is lost in the drawing.
 */
export type Pose = 'standing' | 'walking' | 'typing' | 'reading'

/** Frame indices per pose, straight from `spriteData.ts`. */
export const POSE_FRAMES: Record<Pose, number[]> = {
  // The walk cycle's first frame, held. A character at a desk is not walking.
  standing: [0],
  walking: [0, 1, 2, 1],
  typing: [3, 4],
  reading: [5, 6],
}

/**
 * Activity (from `OfficeProjection`) to pose.
 *
 * `running` draws as typing because running a command is work at the desk, and
 * `delegating` draws as reading because the agent is attending to another's
 * output. Both are *drawings* of an activity, never a restatement of it — the
 * activity string itself is rendered as text beside the character.
 */
export const POSE_BY_ACTIVITY: Record<string, Pose> = {
  idle: 'standing',
  waiting: 'standing',
  typing: 'typing',
  running: 'typing',
  reading: 'reading',
  delegating: 'reading',
}

export function poseFor(activity: string): Pose {
  return POSE_BY_ACTIVITY[activity] ?? 'standing'
}

/** The sheet for one character index. */
export function characterUrl(index: number): string {
  return `${ASSET_BASE}/assets/characters/char_${index % CHAR_COUNT}.png`
}

export function floorUrl(index: number): string {
  return `${ASSET_BASE}/assets/floors/floor_${index}.png`
}

/**
 * Which of the six sheets an agent gets.
 *
 * A stable hash of the name, so `verifier` is the same character in every
 * session and on every machine. Random assignment would make two people
 * describing the same run disagree about who they were looking at.
 */
export function characterIndexFor(agent: string): number {
  let hash = 0
  for (let i = 0; i < agent.length; i += 1) {
    hash = (hash * 31 + agent.charCodeAt(i)) | 0
  }
  return Math.abs(hash) % CHAR_COUNT
}

/** The source rectangle for one frame of one direction. */
export function frameRect(direction: Direction, frame: number) {
  const row = direction === 'left' ? 'right' : direction
  const rowIndex = DIRECTION_ROWS.indexOf(row as (typeof DIRECTION_ROWS)[number])
  return {
    sx: frame * CHAR_FRAME_W,
    sy: Math.max(rowIndex, 0) * CHAR_FRAME_H,
    sw: CHAR_FRAME_W,
    sh: CHAR_FRAME_H,
    flip: direction === 'left',
  }
}
