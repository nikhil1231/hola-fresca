import { useEffect, useMemo, useRef, useState } from 'react'
import { ActionIcon, Badge, Button, Group, Image, Text } from '@mantine/core'
import { IconArrowLeft, IconArrowRight, IconCheck, IconClock, IconX } from '@tabler/icons-react'

import classes from './CookMapView.module.css'

const COLORS = ['#4ade80', '#f0a54a', '#5da9e9', '#d76b57', '#7fd3bf', '#b892e8']
const COLS = [52, 156, 264, 368]
const ROW_START = 62
const ROW_PITCH = 92
const NODE_RADIUS = 18

function formatTime(seconds) {
  const value = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`
}

function rounded(value) {
  if (value >= 20) return Math.round(value / 5) * 5
  if (value >= 1) return Math.round(value * 2) / 2
  return Math.round(value * 4) / 4
}

function ingredientLabel(ingredient, factor) {
  if (!ingredient) return null
  if (ingredient.amount != null) {
    const amount = rounded(ingredient.amount * factor)
    return `${amount} ${ingredient.unit ?? ''} ${ingredient.name}`.replace(/\s+/g, ' ').trim()
  }
  if (ingredient.amount_g != null) {
    return `${rounded(ingredient.amount_g * factor)}${ingredient.canonical_unit ?? 'g'} ${ingredient.name}`
  }
  return ingredient.name
}

function descendantsOf(nodeId, edges) {
  const outgoing = new Map()
  for (const edge of edges) {
    if (!outgoing.has(edge.source)) outgoing.set(edge.source, [])
    outgoing.get(edge.source).push(edge.target)
  }
  const descendants = new Set([nodeId])
  const queue = [nodeId]
  while (queue.length) {
    const current = queue.shift()
    for (const target of outgoing.get(current) ?? []) {
      if (descendants.has(target)) continue
      descendants.add(target)
      queue.push(target)
    }
  }
  return descendants
}

function loadProgress(storageKey, nodeIds) {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey) ?? '{}')
    return {
      done: new Set((raw.done ?? []).filter((id) => nodeIds.has(id))),
      timers: Object.fromEntries(
        Object.entries(raw.timers ?? {}).filter(([id, timer]) => (
          nodeIds.has(id) && Number.isFinite(timer?.deadline) && Number.isFinite(timer?.total)
        )),
      ),
    }
  } catch {
    return { done: new Set(), timers: {} }
  }
}

function nodePosition(node) {
  return {
    x: COLS[node.col] - (node.collapsed ? node.chip_index * 12 : 0),
    y: ROW_START + node.row * ROW_PITCH + (node.collapsed ? node.chip_index * 24 : 0),
  }
}

function edgePath(source, target) {
  const a = nodePosition(source)
  const b = nodePosition(target)
  if (Math.abs(a.x - b.x) < 2) {
    return `M ${a.x} ${a.y + NODE_RADIUS + 5} L ${b.x} ${b.y - NODE_RADIUS - 5}`
  }
  const direction = b.x > a.x ? 1 : -1
  const corner = 16
  const endX = b.x - direction * (NODE_RADIUS + 6)
  return `M ${a.x} ${a.y + NODE_RADIUS + 5} L ${a.x} ${b.y - corner} Q ${a.x} ${b.y} ${a.x + direction * corner} ${b.y} L ${endX} ${b.y}`
}

export default function CookMapView({ graph, recipe, ingredients, servings, modifierKey }) {
  const laneMap = useMemo(
    () => new Map(graph.lanes.map((lane, index) => [lane.id, { ...lane, color: COLORS[index % COLORS.length] }])),
    [graph.lanes],
  )
  const nodeMap = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph.nodes])
  const nodeIds = useMemo(() => new Set(graph.nodes.map((node) => node.id)), [graph.nodes])
  const incoming = useMemo(() => {
    const result = new Map(graph.nodes.map((node) => [node.id, []]))
    for (const edge of graph.edges) result.get(edge.target)?.push(edge.source)
    return result
  }, [graph.edges, graph.nodes])
  const ingredientMap = useMemo(
    () => new Map(ingredients.map((ingredient) => [ingredient.recipe_ingredient_id, ingredient])),
    [ingredients],
  )
  const factor = servings / (recipe.base_yield || 2)
  const storageKey = `hola-fresca:cook-map:v1:${recipe.id}:${graph.source_fingerprint ?? 'graph'}:${modifierKey}`
  const initial = useMemo(() => loadProgress(storageKey, nodeIds), [storageKey, nodeIds])
  const [done, setDone] = useState(initial.done)
  const [timers, setTimers] = useState(initial.timers)
  const [selectedId, setSelectedId] = useState(null)
  const [now, setNow] = useState(Date.now())
  const touchStart = useRef(null)

  useEffect(() => {
    setDone(initial.done)
    setTimers(initial.timers)
  }, [initial])

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify({ done: [...done], timers }))
  }, [done, storageKey, timers])

  useEffect(() => {
    if (!Object.keys(timers).length) return undefined
    const interval = window.setInterval(() => setNow(Date.now()), 500)
    return () => window.clearInterval(interval)
  }, [timers])

  useEffect(() => {
    const finishedPassive = Object.entries(timers)
      .filter(([id, timer]) => timer.deadline <= now && nodeMap.get(id)?.kind === 'passive')
      .map(([id]) => id)
    if (!finishedPassive.length) return
    setDone((current) => new Set([...current, ...finishedPassive]))
    setTimers((current) => {
      const next = { ...current }
      for (const id of finishedPassive) delete next[id]
      return next
    })
    if (navigator.vibrate) navigator.vibrate(120)
  }, [nodeMap, now, timers])

  useEffect(() => {
    const finishedActive = Object.entries(timers)
      .filter(([id, timer]) => (
        timer.deadline <= now && !timer.notified && nodeMap.get(id)?.kind === 'active'
      ))
      .map(([id]) => id)
    if (!finishedActive.length) return
    setTimers((current) => Object.fromEntries(
      Object.entries(current).map(([id, timer]) => [
        id,
        finishedActive.includes(id) ? { ...timer, notified: true } : timer,
      ]),
    ))
    if (navigator.vibrate) navigator.vibrate([120, 80, 120])
  }, [nodeMap, now, timers])

  const blockers = (nodeId) => (incoming.get(nodeId) ?? []).filter((id) => !done.has(id))
  const isAvailable = (nodeId) => !done.has(nodeId) && !timers[nodeId] && blockers(nodeId).length === 0

  const markDone = (nodeId) => {
    if (blockers(nodeId).length) return
    setDone((current) => new Set([...current, nodeId]))
    setTimers((current) => {
      const next = { ...current }
      delete next[nodeId]
      return next
    })
  }

  const undo = (nodeId) => {
    const affected = descendantsOf(nodeId, graph.edges)
    setDone((current) => new Set([...current].filter((id) => !affected.has(id))))
    setTimers((current) => Object.fromEntries(
      Object.entries(current).filter(([id]) => !affected.has(id)),
    ))
  }

  const startTimer = (node) => {
    if (!node.duration_seconds || blockers(node.id).length) return
    setTimers((current) => ({
      ...current,
      [node.id]: {
        deadline: Date.now() + node.duration_seconds * 1000,
        total: node.duration_seconds,
        notified: false,
      },
    }))
  }

  const selected = selectedId ? nodeMap.get(selectedId) : null
  const selectedLane = selected ? laneMap.get(selected.lane_id) : null
  const selectedTimer = selected ? timers[selected.id] : null
  const selectedBlockers = selected ? blockers(selected.id) : []
  const laneSequence = selected
    ? graph.nodes.filter((node) => node.lane_id === selected.lane_id).sort((a, b) => a.row - b.row)
    : []
  const lanePosition = selected ? laneSequence.findIndex((node) => node.id === selected.id) : -1

  const moveSheet = (direction) => {
    const next = laneSequence[lanePosition + direction]
    if (next) setSelectedId(next.id)
  }

  const handleTouchEnd = (event) => {
    if (touchStart.current == null) return
    const delta = event.changedTouches[0].clientX - touchStart.current
    if (Math.abs(delta) > 55) moveSheet(delta < 0 ? 1 : -1)
    touchStart.current = null
  }

  const height = ROW_START + Math.max(1, graph.row_count) * ROW_PITCH + 58

  return (
    <section className={classes.shell} aria-label="Cook map">
      <div className={classes.mapHeader}>
        <div className={classes.progressRow}>
          <div className={classes.progress}><i style={{ width: `${done.size / graph.nodes.length * 100}%` }} /></div>
          <Text size="xs" c="dimmed" ff="monospace">{done.size}/{graph.nodes.length}</Text>
        </div>
        <div className={classes.legend}>
          {graph.lanes.map((lane) => (
            <span key={lane.id}><i style={{ background: laneMap.get(lane.id).color }} />{lane.name}</span>
          ))}
        </div>
      </div>

      {Object.keys(timers).length > 0 && (
        <div className={classes.timerStrip} aria-label="Running timers">
          {Object.entries(timers).map(([id, timer]) => {
            const node = nodeMap.get(id)
            const remaining = Math.max(0, (timer.deadline - now) / 1000)
            return (
              <button key={id} type="button" onClick={() => setSelectedId(id)} data-ready={remaining <= 0 || undefined}>
                <i style={{ background: laneMap.get(node.lane_id).color }} />
                {node.title} <b>{remaining <= 0 ? 'ready' : formatTime(remaining)}</b>
              </button>
            )
          })}
        </div>
      )}

      <div className={classes.scroller}>
        <svg className={classes.map} viewBox={`0 0 420 ${height}`} role="img" aria-label={`${recipe.name} cooking flow`}>
          {Array.from({ length: graph.row_count }, (_, row) => {
            const y = ROW_START + row * ROW_PITCH
            return (
              <g key={`row-${row}`}>
                <line x1="14" x2="406" y1={y} y2={y} className={classes.rowLine} />
                <text x="15" y={y - 9} className={classes.rowNumber}>{String(row + 1).padStart(2, '0')}</text>
              </g>
            )
          })}
          {graph.edges.map((edge) => {
            const source = nodeMap.get(edge.source)
            const target = nodeMap.get(edge.target)
            if (!source || !target) return null
            return (
              <path
                key={`${edge.source}-${edge.target}`}
                d={edgePath(source, target)}
                className={edge.style === 'hold' ? classes.hold : classes.lane}
                stroke={laneMap.get(source.lane_id).color}
              />
            )
          })}
          {graph.nodes.map((node) => {
            const { x, y } = nodePosition(node)
            const lane = laneMap.get(node.lane_id)
            const complete = done.has(node.id)
            const running = timers[node.id]
            const timerCircumference = 2 * Math.PI * 25
            const timerRemaining = running ? Math.max(0, (running.deadline - now) / 1000) : 0
            const available = isAvailable(node.id)
            const locked = !complete && !running && !available
            const labelX = node.col === 0 ? x + 30 : node.col === 3 ? x - 30 : x
            const labelAnchor = node.col === 0 ? 'start' : node.col === 3 ? 'end' : 'middle'
            // Cross-lane elbows arrive on the node's row. Lift side labels above
            // that rail so an incoming connector never strikes through the text.
            const labelY = node.col === 1 || node.col === 2 ? y + 38 : y - 22
            if (node.collapsed) {
              return (
                <g
                  key={node.id}
                  role="button"
                  tabIndex="0"
                  aria-label={`${node.ref}, ${node.title}`}
                  className={`${classes.node} ${locked ? classes.locked : ''}`}
                  onClick={() => setSelectedId(node.id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') setSelectedId(node.id)
                  }}
                >
                  <rect x={x - 47} y={y - 15} width="94" height="30" rx="15" fill="var(--hf-surface)" stroke={lane.color} strokeWidth="2" />
                  <text x={x} y={y + 4} textAnchor="middle" className={classes.chipText}>{complete ? '✓' : node.ref} · {node.title}</text>
                </g>
              )
            }
            return (
              <g
                key={node.id}
                role="button"
                tabIndex="0"
                aria-label={`${node.ref}, ${node.title}`}
                className={`${classes.node} ${locked ? classes.locked : ''}`}
                onClick={() => setSelectedId(node.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') setSelectedId(node.id)
                }}
              >
                {available && <circle cx={x} cy={y} r="29" fill={lane.color} className={classes.pulse} />}
                {running && (
                  <circle
                    cx={x}
                    cy={y}
                    r="25"
                    fill="none"
                    stroke={lane.color}
                    strokeWidth="3"
                    strokeLinecap="round"
                    strokeDasharray={timerCircumference}
                    strokeDashoffset={timerCircumference * (1 - timerRemaining / running.total)}
                    className={classes.runningRing}
                  />
                )}
                <circle cx={x} cy={y} r={NODE_RADIUS} fill={complete ? lane.color : 'var(--hf-surface)'} stroke={lane.color} strokeWidth="3" />
                <text x={x} y={y + 4} textAnchor="middle" className={classes.nodeRef} fill={complete ? '#0d110f' : lane.color}>{complete ? '✓' : node.ref}</text>
                <text x={labelX} y={labelY} textAnchor={labelAnchor} className={classes.nodeLabel}>{node.title}</text>
              </g>
            )
          })}
        </svg>
      </div>

      {selected && (
        <>
          <button className={classes.scrim} type="button" aria-label="Close step" onClick={() => setSelectedId(null)} />
          <div
            className={classes.sheet}
            role="dialog"
            aria-modal="true"
            aria-labelledby="cook-map-sheet-title"
            onTouchStart={(event) => { touchStart.current = event.touches[0].clientX }}
            onTouchEnd={handleTouchEnd}
          >
            <div className={classes.grab} />
            <Group justify="space-between" align="flex-start" wrap="nowrap">
              <div>
                <Text size="xs" tt="uppercase" ff="monospace" c="dimmed">
                  <span className={classes.sheetDot} style={{ background: selectedLane.color }} />
                  {selectedLane.name} · step {selected.ref}
                </Text>
                <Text id="cook-map-sheet-title" fw={800} fz="xl" mt={4}>{selected.title}</Text>
              </div>
              <ActionIcon variant="subtle" color="gray" radius="xl" aria-label="Close step" onClick={() => setSelectedId(null)}>
                <IconX size={19} />
              </ActionIcon>
            </Group>
            {selected.image_url && <Image src={selected.image_url} className={classes.sheetImage} radius="md" mt="sm" />}
            <Text className={classes.detail}>{selected.detail}</Text>
            <Group gap={6} mt="sm">
              {selected.ingredient_ids.map((id) => {
                const label = ingredientLabel(ingredientMap.get(id), factor)
                return label ? <Badge key={id} variant="light" color="gray" radius="xl">{label}</Badge> : null
              })}
            </Group>
            {selectedBlockers.length > 0 && (
              <div className={classes.blockers}>
                <Text size="sm" c="dimmed">Waiting on:</Text>
                <Group gap={6} mt={6}>
                  {selectedBlockers.map((id) => (
                    <Button key={id} size="compact-xs" variant="outline" color="gray" onClick={() => setSelectedId(id)}>
                      {nodeMap.get(id).ref} · {nodeMap.get(id).title}
                    </Button>
                  ))}
                </Group>
              </div>
            )}
            <Group justify="space-between" mt="md">
              <ActionIcon variant="light" color="gray" radius="xl" disabled={lanePosition <= 0} aria-label="Previous lane step" onClick={() => moveSheet(-1)}>
                <IconArrowLeft size={18} />
              </ActionIcon>
              <Text size="xs" c="dimmed">{lanePosition + 1} of {laneSequence.length} in this lane</Text>
              <ActionIcon variant="light" color="gray" radius="xl" disabled={lanePosition >= laneSequence.length - 1} aria-label="Next lane step" onClick={() => moveSheet(1)}>
                <IconArrowRight size={18} />
              </ActionIcon>
            </Group>
            <div className={classes.actions}>
              {done.has(selected.id) ? (
                <Button fullWidth variant="outline" color="gray" onClick={() => undo(selected.id)}>Undo this step</Button>
              ) : selectedBlockers.length ? (
                <Button fullWidth disabled>Locked until lanes catch up</Button>
              ) : selectedTimer ? (
                selected.kind === 'active' ? (
                  <Button fullWidth color="fresh" leftSection={<IconCheck size={18} />} onClick={() => markDone(selected.id)}>
                    {selectedTimer.deadline <= now ? 'Timer ready · mark done' : `Mark done · ${formatTime((selectedTimer.deadline - now) / 1000)}`}
                  </Button>
                ) : (
                  <Button fullWidth disabled leftSection={<IconClock size={18} />}>
                    Running · {formatTime((selectedTimer.deadline - now) / 1000)}
                  </Button>
                )
              ) : selected.duration_seconds ? (
                <Button fullWidth color="fresh" leftSection={<IconClock size={18} />} onClick={() => startTimer(selected)}>
                  Start {formatTime(selected.duration_seconds)} timer
                </Button>
              ) : (
                <Button fullWidth color="fresh" leftSection={<IconCheck size={18} />} onClick={() => markDone(selected.id)}>Mark done</Button>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  )
}
