import {expect, it} from 'vitest'
import {scenarioWithScope} from './discovery'

it('carries the general-development boundary with the actual scenario', () => {
  const scenario = scenarioWithScope('  Implement CSV validation.  ', 'offline')
  expect(scenario).toMatch(/^Execution scope: offline;/)
  expect(scenario).toContain('no hardware access is permitted')
  expect(scenario).toContain('\n\nImplement CSV validation.')
})

it('keeps user requirements intact after the host execution directive', () => {
  const request = 'Implement the parser.\nPreserve quoted fields.\nRun tests.'
  expect(scenarioWithScope(request, 'offline').split('\n\n')[1]).toBe(request)
})

it('limits optional device delegation to the submitted scenario', () => {
  const scenario = scenarioWithScope('Check the selected device.', 'hardware')
  expect(scenario).toContain('within their electrical safety bounds')
  expect(scenario).toContain('Other devices are not authorized')
})
