const COMMAND_KINDS = new Set([
  'mission.start',
  'approval.decide',
  'job.steer',
  'job.cancel',
])

export function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    },
  })
}

export async function requireOperator(context) {
  return requireBearer(context.request, context.env.OPERATOR_TOKEN_SHA256, 'operator')
}

export async function requireDevice(context) {
  return requireBearer(context.request, context.env.DEVICE_TOKEN_SHA256, 'device')
}

async function requireBearer(request, expected, label) {
  if (typeof expected !== 'string' || !/^[0-9a-f]{64}$/.test(expected)) {
    return json({ ok: false, error: `${label} authentication is not configured` }, 503)
  }
  const header = request.headers.get('authorization') || ''
  const token = header.toLowerCase().startsWith('bearer ') ? header.slice(7).trim() : ''
  if (!token) return json({ ok: false, error: `${label} token required` }, 401)
  const digest = await sha256(token)
  if (!constantTimeHexEqual(digest, expected)) {
    return json({ ok: false, error: `${label} token refused` }, 403)
  }
  return null
}

export async function readObject(request) {
  let body
  try {
    body = await request.json()
  } catch {
    throw new RequestError('request body must be JSON')
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new RequestError('request body must be an object')
  }
  return body
}

export function text(body, key, max = 4000) {
  const value = body[key]
  if (typeof value !== 'string' || !value.trim()) {
    throw new RequestError(`${key} must be a non-empty string`)
  }
  if (value.length > max) throw new RequestError(`${key} is too long`)
  return value.trim()
}

export function commandKind(body) {
  const kind = text(body, 'kind', 100)
  if (!COMMAND_KINDS.has(kind)) {
    throw new RequestError(`command kind is not allowed: ${kind}`)
  }
  return kind
}

export function requestError(error) {
  return error instanceof RequestError
    ? json({ ok: false, error: error.message }, 422)
    : json({ ok: false, error: String(error) }, 500)
}

export class RequestError extends Error {}

async function sha256(value) {
  const bytes = new TextEncoder().encode(value)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function constantTimeHexEqual(left, right) {
  if (left.length !== right.length) return false
  let different = 0
  for (let index = 0; index < left.length; index += 1) {
    different |= left.charCodeAt(index) ^ right.charCodeAt(index)
  }
  return different === 0
}
