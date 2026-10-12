/**
 * Where the page's session token comes from, and where it does not stay.
 *
 * The backend prints a URL with `?token=` in it because that is the only way
 * to hand a credential to a page that has not loaded yet. Three things follow,
 * and each one is a deliberate choice rather than an omission:
 *
 * **It is taken out of the address bar.** A token left in `location.search`
 * ends up in the tab title, in a screenshot, in whatever the user pastes into
 * chat when asking for help, and in the `Referer` of any request the page makes
 * off-origin. `history.replaceState` removes it without adding a history entry,
 * so Back still does what the user expects.
 *
 * **It is kept in `sessionStorage`, not `localStorage`.** The token dies with
 * the server process, so storing it past the tab that was given it would leave
 * a credential nobody remembers is dead. `sessionStorage` is per-tab and is
 * cleared when the tab closes, which is the same lifetime.
 *
 * **A reload without the token still works.** That is the whole reason for
 * storing it: F5 on a URL the page itself cleaned would otherwise log the user
 * out of a page they never left.
 */

const KEY = 'qkd.webui.token'

/** The token in a query string, or null. Pure, so it can be tested alone. */
export function extractToken(search: string): string | null {
  const value = new URLSearchParams(search).get('token')
  return value && value.trim() ? value.trim() : null
}

/** The query string with `token` removed, keeping everything else. */
export function stripToken(search: string): string {
  const params = new URLSearchParams(search)
  params.delete('token')
  const rest = params.toString()
  return rest ? `?${rest}` : ''
}

export interface TokenStore {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/**
 * The token for this page: from the URL if it is there, else from the tab.
 *
 * Returns null when there is neither, which the shell renders as an
 * instruction rather than as an error -- a page opened without a token is
 * usually a bookmark from a previous run, not a mistake.
 */
export function resolveToken(
  search: string,
  store: TokenStore | null,
): { token: string | null; cleanedSearch: string | null } {
  const fromUrl = extractToken(search)
  if (fromUrl) {
    try {
      store?.setItem(KEY, fromUrl)
    } catch {
      // Private mode, or storage disabled. The token still works for this
      // page load; only the reload-survives-F5 convenience is lost.
    }
    return { token: fromUrl, cleanedSearch: stripToken(search) }
  }
  let stored: string | null = null
  try {
    stored = store?.getItem(KEY) ?? null
  } catch {
    stored = null
  }
  return { token: stored, cleanedSearch: null }
}

/** Read the token and clean the address bar. Called once, at startup. */
export function installToken(): string | null {
  if (typeof window === 'undefined') return null
  const { token, cleanedSearch } = resolveToken(window.location.search, window.sessionStorage)
  if (cleanedSearch !== null) {
    window.history.replaceState(null, '', `${window.location.pathname}${cleanedSearch}`)
  }
  return token
}
