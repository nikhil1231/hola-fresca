export function canEditOrder(order, now = Date.now()) {
  if (!order) return true
  if (order.editable !== true) return false
  if (!order.edit_until) return true
  const deadline = Date.parse(order.edit_until)
  return Number.isFinite(deadline) && now < deadline
}

// A closed delivery on screen says nothing about edits to a different delivery.
export function queuedEditingClosed(queue, selectedTargetId, order, now = Date.now()) {
  return Boolean(queue?.target_id && queue.target_id === selectedTargetId && order && !canEditOrder(order, now))
}
