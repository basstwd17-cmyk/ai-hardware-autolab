import { describe, expect, it } from 'vitest'
import { extractToken, resolveToken, stripToken } from './token'

function store(initial: Record<string, string> = {}) {
  const held = { ...initial }
  return {
    held,
    getItem: (key: string) => held[key] ?? null,
    setItem: (key: string, value: string) => {
      held[key] = value
    },
  }
}

describe('the token in the URL', () => {
  it('is read, and nothing else is mistaken for it', () => {
    expect(extractToken('?token=abc')).toBe('abc')
    expect(extractToken('?after=1&token=abc&x=2')).toBe('abc')
    expect(extractToken('?token=%20')).toBeNull()
    expect(extractToken('?tokens=abc')).toBeNull()
    expect(extractToken('')).toBeNull()
  })

  it('is removed from the query without taking anything with it', () => {
    expect(stripToken('?token=abc')).toBe('')
    expect(stripToken('?after=1&token=abc')).toBe('?after=1')
    expect(stripToken('?after=1')).toBe('?after=1')
  })
})

describe('resolving a token for this page', () => {
  it('prefers the URL and remembers it for the reload', () => {
    const kept = store()
    const { token, cleanedSearch } = resolveToken('?token=abc', kept)
    expect(token).toBe('abc')
    expect(cleanedSearch).toBe('')
    expect(Object.values(kept.held)).toContain('abc')
  })

  it('falls back to the tab, so F5 does not log the user out', () => {
    const kept = store()
    resolveToken('?token=abc', kept)
    const again = resolveToken('', kept)
    expect(again.token).toBe('abc')
    // Nothing to clean: the URL never had it this time.
    expect(again.cleanedSearch).toBeNull()
  })

  it('reports nothing rather than throwing when storage is unavailable', () => {
    const broken = {
      getItem: () => {
        throw new Error('storage disabled')
      },
      setItem: () => {
        throw new Error('storage disabled')
      },
    }
    // Private mode: the URL token still works for this load.
    expect(resolveToken('?token=abc', broken).token).toBe('abc')
    expect(resolveToken('', broken).token).toBeNull()
    expect(resolveToken('', null).token).toBeNull()
  })
})
