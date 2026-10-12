import { expect, it } from 'vitest'
import { hardwareKey, parseHardwareSelection, withHardwareSelection } from './hardwareSelection'

it('sends a snapshot of the selected board alongside the scenario and model choice', () => {
  const selection = { board_id: 'other-board', revision: 'v2' }
  const command = withHardwareSelection({ type: 'job.mission', scenario: 'read serial', modelSelection: { default: { model: 'selected' } } }, selection)
  selection.board_id = 'changed-after-submission'
  expect(command).toMatchObject({ hardwareSelection: { board_id: 'other-board', revision: 'v2' }, modelSelection: { default: { model: 'selected' } } })
  expect(hardwareKey(parseHardwareSelection('{"board_id":"other-board","revision":"v2"}'))).toBe('other-board@v2')
})

it('keeps an omitted selection on the daemon default and ignores invalid browser data', () => {
  expect(withHardwareSelection({ type: 'job.mission' })).not.toHaveProperty('hardwareSelection')
  for (const raw of [null, 'invalid', '{"board_id":"../oops","revision":"v1"}', '{"board_id":3,"revision":"v1"}']) {
    expect(parseHardwareSelection(raw)).toBeUndefined()
  }
})
