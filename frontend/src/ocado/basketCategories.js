// Ocado's full aisle paths are too detailed for basket headings.
const collator = new Intl.Collator('en-GB', { sensitivity: 'base', numeric: true })

function categoryLabel(category) {
  const parts = typeof category === 'string' ? category.split('>').map((part) => part.trim()).filter(Boolean) : []
  if (!parts.length) return 'Other items'
  // Split the broad fresh department into useful groups such as Fruit or Dairy.
  return parts[0] === 'Fresh & Chilled Food' && parts[1] ? parts[1] : parts[0]
}

export function basketCategories(lines) {
  const groups = new Map()
  for (const line of lines) {
    const label = categoryLabel(line.category)
    if (!groups.has(label)) groups.set(label, [])
    groups.get(label).push(line)
  }
  return [...groups].sort(([a], [b]) => a === b ? 0 : a === 'Other items' ? 1 : b === 'Other items' ? -1 : collator.compare(a, b))
    .map(([label, items]) => ({ label, items: [...items].sort((a, b) => collator.compare(a.name || a.sku, b.name || b.sku)) }))
}
