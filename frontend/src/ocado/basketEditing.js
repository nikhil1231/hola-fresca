const known = (fields) => Object.fromEntries(Object.entries(fields).filter(([, value]) => value != null))
const pendingKind = (from, to) => to === 0 ? 'remove' : from === 0 ? 'add' : 'change'

// Keep confirmed items on screen until Ocado confirms a saved basket.
// `optimistic` holds quantity edits not yet accepted by the server, shown at once.
export function basketEditing(state, queue, targetId, optimistic = {}) {
  if (!state) return []
  const metadata = new Map((state.basket?.lines || []).map((line) => [String(line.sku), line]))
  const confirmed = state.confirmed_lines || (state.basket?.lines || []).filter((line) =>
    (state.confirmed_quantities?.[line.sku] ?? line.quantity) > 0
  ).map((line) => ({ ...line, quantity: state.confirmed_quantities?.[line.sku] ?? line.quantity }))
  const lines = new Map(confirmed.map((line) => [String(line.sku), { ...metadata.get(String(line.sku)), ...line }]))
  // Keep the last pending appearance while the successful save's fresh read arrives.
  const edits = queue?.status === 'succeeded' && state.queue?.status === 'pending' ? state.queue : queue
  if (edits?.target_id === targetId && ['pending', 'running', 'failed'].includes(edits?.status)) {
    for (const action of edits.actions || []) {
      if (action.kind !== 'quantity') continue
      const sku = String(action.sku)
      const original = lines.get(sku)
      const quantity = original?.quantity ?? action.original_quantity ?? 0
      if (quantity === action.quantity) continue
      lines.set(sku, { ...metadata.get(sku), ...original, sku, name: action.name || original?.name || metadata.get(sku)?.name || sku,
        quantity, pending_quantity: action.quantity, pending_kind: pendingKind(quantity, action.quantity) })
    }
  }
  for (const [sku, edit] of Object.entries(optimistic)) {
    if (edit.target !== targetId) continue
    const { pending_quantity: _quantity, pending_kind: _kind, ...current } = lines.get(sku) || {}
    const quantity = current.quantity ?? 0
    lines.set(sku, quantity === edit.quantity ? current : { ...known({ name: edit.name, image_url: edit.image_url, pack: edit.pack, offers: edit.offers }), ...known(metadata.get(sku) || {}), ...current, sku,
      quantity, pending_quantity: edit.quantity, pending_kind: pendingKind(quantity, edit.quantity) })
  }
  return [...lines.values()].filter((line) => line.quantity > 0 || line.pending_quantity > 0)
}
