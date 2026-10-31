import test from 'node:test'
import assert from 'node:assert/strict'
import { basketCategories } from './basketCategories.js'

test('groups aisle paths into departments, splits fresh food, and puts unknown items last', () => {
  const lines = [
    { sku: 'u', name: 'Unknown' },
    { sku: 'b', name: 'Beans', category: 'Food Cupboard > Tins' },
    { sku: 'p', name: 'Pears', category: 'Fresh & Chilled Food > Fruit > Pears' },
    { sku: 'a', name: 'Apples', category: 'Fresh & Chilled Food > Fruit > Apples' },
    { sku: 's', name: 'Salt', category: 'Food Cupboard > Seasoning' },
    { sku: 'n', name: 'No category', category: '  > ' },
  ]
  const groups = basketCategories(lines)
  assert.deepEqual(groups.map(({ label }) => label), ['Food Cupboard', 'Fruit', 'Other items'])
  assert.deepEqual(groups[1].items.map(({ sku }) => sku), ['a', 'p'])
  assert.equal(lines[0].sku, 'u')
  assert.deepEqual(basketCategories([]), [])
})
