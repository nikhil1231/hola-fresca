// Keep confirmed items on screen until Ocado confirms a saved basket.
export function basketEditing(state, queue, targetId) {
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
        quantity, pending_quantity: action.quantity, pending_kind: action.quantity === 0 ? 'remove' : quantity === 0 ? 'add' : 'change' })
    }
  }
  return [...lines.values()].filter((line) => line.quantity > 0 || line.pending_quantity > 0)
}
