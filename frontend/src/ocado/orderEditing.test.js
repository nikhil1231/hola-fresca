import assert from 'node:assert/strict'
import test from 'node:test'
import { canEditOrder, queuedEditingClosed } from './orderEditing.js'

const deadline = '2030-01-01T20:00:00Z'
test('an unbooked basket can be edited', () => assert.equal(canEditOrder(null), true))
test('Ocado closing an order blocks edits even before the deadline', () => {
  assert.equal(canEditOrder({ editable: false, edit_until: deadline }, Date.parse(deadline) - 1000), false)
})
test('the deadline closes editing without waiting for an Ocado refresh', () => {
  const order = { editable: true, edit_until: deadline }
  assert.equal(canEditOrder(order, Date.parse(deadline) - 1), true)
  assert.equal(canEditOrder(order, Date.parse(deadline)), false)
  assert.equal(canEditOrder(order, Date.parse(deadline) + 1), false)
})
test('unknown editing permission or an unreadable deadline never enables edits', () => {
  assert.equal(canEditOrder({}), false)
  assert.equal(canEditOrder({ editable: true, edit_until: 'unknown' }), false)
  assert.equal(canEditOrder({ editable: true }), true)
})

test('a closed delivery on screen does not suppress another delivery’s save timer', () => {
  const queue = { target_id: 'next-week', status: 'pending' }
  assert.equal(queuedEditingClosed(queue, 'today', { editable: false }), false)
  assert.equal(queuedEditingClosed(queue, 'next-week', { editable: true, edit_until: deadline }, Date.parse(deadline) - 1000), false)
  assert.equal(queuedEditingClosed(queue, 'next-week', { editable: false }), true)
  assert.equal(queuedEditingClosed(queue, 'next-week', { editable: true, edit_until: deadline }, Date.parse(deadline)), true)
})
