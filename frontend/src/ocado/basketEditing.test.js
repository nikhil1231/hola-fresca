import test from 'node:test'
import assert from 'node:assert/strict'
import { basketEditing } from './basketEditing.js'

const confirmed = [{ sku: 'milk', name: 'Milk', quantity: 2, cost: 3, image_url: 'milk.jpg' }]
const state = { confirmed_lines: confirmed, confirmed_quantities: { milk: 2 }, basket: { lines: [] } }
const queue = (quantity, status = 'pending', sku = 'milk') => ({ target_id: 'next', status, actions: [{ kind: 'quantity', sku, name: sku, quantity, original_quantity: sku === 'milk' ? 2 : 0 }] })

test('pending removal retains the confirmed tile and its price with zero as the next quantity', () => {
  const [line] = basketEditing(state, queue(0), 'next')
  assert.equal(line.quantity, 2)
  assert.equal(line.pending_quantity, 0)
  assert.equal(line.pending_kind, 'remove')
  assert.equal(line.cost, 3)
  assert.equal(line.image_url, 'milk.jpg')
})
test('quantity changes preserve the confirmed quantity and price, additions remain visible', () => {
  const [line] = basketEditing(state, queue(3), 'next')
  assert.equal(line.quantity, 2)
  assert.equal(line.pending_quantity, 3)
  assert.equal(line.pending_kind, 'change')
  const addedState = { ...state, basket: { lines: [{ sku: 'bread', quantity: 1, cost: null, image_url: 'bread.jpg' }] } }
  const added = basketEditing(addedState, queue(1, 'pending', 'bread'), 'next').find((item) => item.sku === 'bread')
  assert.equal(added.quantity, 0)
  assert.equal(added.pending_quantity, 1)
  assert.equal(added.pending_kind, 'add')
  assert.equal(added.image_url, 'bread.jpg')
})
test('restoring, undoing or cancelling clears the pending appearance', () => {
  for (const edits of [queue(2), { ...queue(0), status: 'idle', actions: [] }]) {
    const [line] = basketEditing(state, edits, 'next')
    assert.equal(line.quantity, 2)
    assert.equal(line.pending_kind, undefined)
  }
})
test('pending appearance survives saving and is replaced only by the successful fresh basket', () => {
  assert.equal(basketEditing(state, queue(0, 'running'), 'next')[0].pending_kind, 'remove')
  assert.equal(basketEditing({ ...state, queue: queue(0) }, { target_id: 'next', status: 'succeeded', actions: [] }, 'next')[0].pending_kind, 'remove')
  assert.deepEqual(basketEditing({ confirmed_lines: [], basket: { lines: [] }, queue: { status: 'succeeded' } }, { status: 'succeeded' }, 'next'), [])
})
test('another delivery’s changes cannot alter this basket', () => {
  assert.equal(basketEditing(state, queue(0), 'today')[0].pending_kind, undefined)
})
test('optimistic edits show immediately over the confirmed and queued basket', () => {
  const optimistic = { milk: { sku: 'milk', quantity: 4, target: 'next' }, eggs: { sku: 'eggs', name: 'Eggs', image_url: 'eggs.jpg', quantity: 1, target: 'next' } }
  const lines = basketEditing(state, queue(3), 'next', optimistic)
  assert.equal(lines.find((line) => line.sku === 'milk').pending_quantity, 4)
  assert.equal(lines.find((line) => line.sku === 'milk').cost, 3)
  const eggs = lines.find((line) => line.sku === 'eggs')
  assert.deepEqual([eggs.quantity, eggs.pending_quantity, eggs.pending_kind, eggs.image_url], [0, 1, 'add', 'eggs.jpg'])
  assert.equal(basketEditing(state, queue(3), 'next', { milk: { sku: 'milk', quantity: 2, target: 'next' } })[0].pending_kind, undefined)
  assert.equal(basketEditing(state, null, 'today', optimistic).length, 1)
})
