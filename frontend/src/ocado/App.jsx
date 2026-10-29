import { useEffect, useId, useRef, useState } from 'react'
import { IconArrowRight, IconBasket, IconCalendarWeek, IconCheck, IconClock, IconLeaf, IconMinus, IconPlus, IconSearch, IconShoppingBag, IconTruckDelivery, IconUserCircle, IconX } from '@tabler/icons-react'
import { request } from './api.js'
import { isReady, useSharedShop } from './useSharedShop.js'

const money = (amount) => amount == null || amount === '' || !Number.isFinite(Number(amount)) ? 'Price unavailable' : new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' }).format(Number(amount))
const formatDate = (value, options) => {
  if (!value || Number.isNaN(Date.parse(value))) return 'Not available'
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', ...options }).format(new Date(value))
}
const day = (value) => formatDate(value, { weekday: 'long', day: 'numeric', month: 'long' })
const time = (value) => formatDate(value, { hour: '2-digit', minute: '2-digit' })
const slotTime = (start, end) => `${time(start)}${end ? ` – ${time(end)}` : ''}`
const tabs = [
  { id: 'basket', label: 'Basket', icon: IconBasket },
  { id: 'search', label: 'Search', icon: IconSearch },
  { id: 'slots', label: 'Delivery', icon: IconCalendarWeek },
  { id: 'account', label: 'Settings', icon: IconUserCircle },
]

function Empty({ icon: Icon = IconShoppingBag, title, children }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={30} stroke={1.5} /></span><h3>{title}</h3><p>{children}</p></div>
}

function Quantity({ name, quantity, disabled, onChange, unavailable = false }) {
  return <div className="quantity" aria-label={`Quantity for ${name}`}>
    <button type="button" aria-label={`Remove one ${name}`} disabled={disabled || quantity === 0} onClick={() => onChange(quantity - 1)}><IconMinus size={17} /></button>
    <output aria-label={`${name} quantity`}>{quantity}</output>
    <button type="button" aria-label={`Add one ${name}`} disabled={disabled || quantity >= 50 || unavailable} onClick={() => onChange(quantity + 1)}><IconPlus size={17} /></button>
  </div>
}

function DeliveryCard({ order, queue, ready, loading, onSlots, total }) {
  const moved = queue?.actions?.findLast((action) => action.kind === 'slot')
  return <section className="delivery-card" aria-label="Current delivery">
    <IconTruckDelivery size={25} stroke={1.6} />
    <div className="delivery-information">
      <p className="delivery-label">Next delivery</p>
      <h1>{order ? day(order.delivery_start) : loading ? 'Checking delivery…' : ready ? 'No delivery booked' : 'Waiting for Ocado'}</h1>
      {order && <p className="delivery-time">{slotTime(order.delivery_start, order.delivery_end)}<span className="delivery-cost">{money(order.total ?? total)}<small>order total</small></span></p>}
      {moved && <p className="queued-slot">New delivery time: {day(moved.start)}, {slotTime(moved.start, moved.end)}</p>}
    </div>
    {ready && <button type="button" className="delivery-link" onClick={onSlots}>{order ? 'Change' : 'Choose'}</button>}
    {order && <details className="delivery-details"><summary>Delivery details{order.editable === false ? ' · Editing closed' : ''}</summary><p>{order.address}</p><p>{order.edit_until ? `Edit until ${day(order.edit_until)}, ${time(order.edit_until)}` : 'Edit deadline unavailable'}</p></details>}
  </section>
}

function EditingStatus({ queue, busy, cancel, retry, check, now }) {
  const held = queue?.status === 'pending' && !queue?.due_at && Boolean(queue?.error)
  const [open, setOpen] = useState(false)
  const [toast, setToast] = useState(null)
  const previous = useRef(null)
  const active = ['pending', 'running', 'failed'].includes(queue?.status) && queue?.actions?.length > 0
  useEffect(() => { if (!active) setOpen(false) }, [active])
  useEffect(() => {
    const before = previous.current
    previous.current = queue?.status
    let message = null
    if (queue?.status === 'pending' && queue.actions?.length) message = held ? 'Ocado didn’t take your changes. Nothing changed, so you can try again.' : 'Changes noted. You can keep shopping.'
    if (queue?.status === 'succeeded' && before && before !== 'succeeded') message = 'Your changes have been saved.'
    if (queue?.status === 'failed' && before !== 'failed') message = 'Please review your changes.'
    setToast(message)
    const timeout = window.setTimeout(() => setToast(null), 4500)
    return () => window.clearTimeout(timeout)
  }, [queue?.status, queue?.updated_at, queue?.actions?.length])
  const remaining = queue?.due_at ? Math.max(0, Math.min(1, (Date.parse(queue.due_at) - now) / 180000)) : 0
  const title = queue?.status === 'running' ? 'Saving your changes' : queue?.status === 'failed' ? 'Review your changes' : held ? 'Changes not sent yet' : 'Editing your shop'
  const progress = <span className="save-progress" role={queue?.status === 'pending' ? 'progressbar' : undefined} aria-label="Time remaining before automatic saving" aria-valuemin={queue?.status === 'pending' ? 0 : undefined} aria-valuemax={queue?.status === 'pending' ? 100 : undefined} aria-valuenow={queue?.status === 'pending' ? Math.round(remaining * 100) : undefined}>
    <svg viewBox="0 0 28 28" aria-hidden="true"><circle className="progress-track" cx="14" cy="14" r="11" /><circle className="progress-value" cx="14" cy="14" r="11" pathLength="100" strokeDasharray={`${remaining * 100} 100`} /></svg>
    {queue?.status === 'failed' ? <span aria-hidden="true">!</span> : queue?.status === 'running' ? <IconClock size={12} aria-hidden="true" /> : <IconCheck size={12} aria-hidden="true" />}
  </span>
  return <>
    {active && <div className="editing-bar"><button type="button" className={`editing-pill editing-${queue.status}`} onClick={() => setOpen(true)} aria-haspopup="dialog">{progress}<span>{title}</span><span className="pill-details">View changes<IconArrowRight size={14} /></span></button></div>}
    <div className="toast-region" role="status" aria-live="polite" aria-atomic="true">{toast && <div className="shop-toast"><IconCheck size={20} aria-hidden="true" /><span>{toast}</span><button type="button" className="icon-button" aria-label="Dismiss notification" onClick={() => setToast(null)}><IconX size={18} /></button></div>}</div>
    {open && active && <Confirm title="Your changes" closeLabel="Keep shopping" onClose={() => setOpen(false)}>
      <p className="muted">{held ? 'Ocado didn’t accept these changes. We checked your order and nothing was changed, so they’re kept here. Send them again now, or keep editing and they’ll be sent together.' : queue.status === 'pending' ? 'Take your time. We’ll save these automatically when you’ve finished making changes. The little circle shows the time left.' : queue.status === 'running' ? 'We’re saving these with Ocado. Please wait a moment.' : 'We couldn’t confirm that everything saved. Check with Ocado to see what it has, or look at your order on ocado.com.'}</p>
      <ul className="change-list">{queue.actions.map((action) => <li key={action.id}>
        <span className="change-symbol" aria-hidden="true">{action.kind === 'quantity' ? action.quantity === 0 ? <IconMinus size={18} /> : <IconBasket size={18} /> : action.kind === 'slot' ? <IconTruckDelivery size={18} /> : <IconCheck size={18} />}</span>
        <div><strong>{action.kind === 'quantity' ? action.name || action.sku : action.kind === 'slot' ? 'Delivery time' : 'Place your order'}</strong><p>{action.kind === 'quantity' ? action.quantity === 0 ? 'Remove from basket' : `${action.quantity} ${action.quantity === 1 ? 'pack' : 'packs'}${action.original_quantity != null ? ` · previously ${action.original_quantity}` : ''}` : action.kind === 'slot' ? `${day(action.start)}, ${slotTime(action.start, action.end)}` : 'Using your saved payment method'}</p></div>
      </li>)}</ul>
      {held && <button type="button" className="button button-primary" disabled={busy} onClick={retry}>{busy ? 'Please wait…' : 'Send changes now'}</button>}
      {queue.status === 'failed' && <button type="button" className="button button-primary" disabled={busy} onClick={check}>{busy ? 'Please wait…' : 'Check with Ocado'}</button>}
      {queue.error && <details className="save-error"><summary>More information</summary><p>{queue.error}</p></details>}
      {queue.status !== 'running' && <><p className="cancel-explanation">{queue.status === 'failed' ? 'Clearing this list won’t undo any changes Ocado has already saved.' : 'Cancel changes removes everyone’s unsaved edits. Your booked delivery stays in place.'}</p><button type="button" className="button button-outline cancel-changes" disabled={busy} onClick={async () => { if (await cancel()) setOpen(false) }}>{busy ? 'Please wait…' : queue.status === 'failed' ? 'Clear reviewed changes' : 'Cancel changes'}</button></>}
    </Confirm>}
  </>
}

function Basket({ lines, basket, order, queue, locked, action, onSearch, onCheckout, onOffer }) {
  const pending = queue?.status === 'pending' || queue?.status === 'running' || queue?.status === 'failed'
  const quantity = lines.reduce((sum, line) => sum + line.quantity, 0)
  return <>
    <div className="section-heading"><h2>Your basket <span className="count">{quantity}</span></h2><div className="basket-tools"><button type="button" className="button button-outline button-small" onClick={onSearch}>Add items<IconPlus size={18} /></button></div></div>
    {pending && <p className="section-copy">Your changes are shown below. We’ll save them for you automatically.</p>}
    {!lines.length ? <Empty title="Room for something delicious">Search for your favourites and add them to your basket.</Empty> : <div className="product-list">{lines.map((line) => <article className="basket-line" key={line.sku}>
      <ProductImage src={line.image_url} />
      <div className="product-information"><h3>{line.name || line.sku}</h3>{line.pack && <p className="pack">{line.pack}</p>}
        <Offers offers={line.offers} onOffer={onOffer} />
        <div className="line-bottom"><p className="line-price"><strong>{line.cost == null ? 'Price to be confirmed' : money(line.cost)}</strong>{line.original_cost != null && <span className="was-price">was {money(line.original_cost)}</span>}<small>{line.quantity > 1 ? `for ${line.quantity} packs` : 'per pack'}</small></p>
          <Quantity name={line.name || line.sku} quantity={line.quantity} disabled={locked} onChange={(next) => action({ kind: 'quantity', sku: line.sku, name: line.name || line.sku, quantity: next })} />
        </div>
      </div>
    </article>)}</div>}

    <section className="basket-summary">
      <div><span>{pending ? 'Last confirmed total' : order ? 'Booked total' : 'Basket total'}</span><strong>{money(basket?.total)}</strong></div>
      {basket?.minimum != null && <p>Minimum order {money(basket.minimum)}. Delivery and final prices are confirmed by Ocado.</p>}
      {basket?.restrictions?.length > 0 && <ul className="restrictions">{basket.restrictions.map((restriction, index) => <li key={index}>{restriction}</li>)}</ul>}
      {!order && basket?.can_checkout === false && <p>Your basket isn’t ready to order yet. Add your items and choose a delivery time, then review your order.</p>}
      {!order && <button type="button" className="button button-primary" disabled={locked || !lines.length || queue?.actions?.some((item) => item.kind === 'checkout')} onClick={onCheckout}>{queue?.actions?.some((item) => item.kind === 'checkout') ? 'Order ready to place' : 'Review your order'}<IconArrowRight size={18} /></button>}
      <p className="checkout-note">{order ? 'Your delivery is booked. We’ll save your changes automatically.' : 'Checkout uses the shared account’s saved payment method. Review before confirming.'}</p>
    </section>
  </>
}

function ProductImage({ src }) {
  const [failed, setFailed] = useState(null)
  if (!src || failed === src) return <span className="product-placeholder"><IconShoppingBag size={26} stroke={1.3} /></span>
  return <img className="product-image" src={src} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(src)} />
}

function Offers({ offers, onOffer }) {
  if (!offers?.length) return null
  return <div className="offers">{offers.map((offer) => <button type="button" className="offer-link" key={offer.id} onClick={() => onOffer(offer)}>{offer.label}<IconArrowRight size={15} /></button>)}</div>
}

function OfferPicker({ offer, lines, locked, selection, targetId, addOffer, addSingle, reportError, onClose }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [chosen, setChosen] = useState({})
  const [filter, setFilter] = useState('')
  const [originalTarget] = useState(targetId)
  const [sent, setSent] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    request(`/offers/${encodeURIComponent(offer.id)}`, { signal: controller.signal }).then((next) => {
      if (controller.signal.aborted) return
      if (next.account_id !== selection.account_id || next.account_version !== selection.account_version) throw new Error('The shared account changed. Reopen this offer.')
      setData(next)
    }).catch((failure) => {
      if (!controller.signal.aborted) { setError(failure.message); if ([401, 403].includes(failure.status)) reportError(failure) }
    })
    return () => controller.abort()
  }, [offer.id, selection.account_id, selection.account_version, reportError])
  const groups = data?.groups || []
  const skus = groups.flatMap((group) => group.products.map((product) => product.sku))
  const canChooseSet = groups.length > 0 && groups.every((group) => Number.isInteger(group.quantity) && group.quantity > 0 && group.quantity <= 50) && new Set(skus).size === skus.length
  const count = Object.values(chosen).reduce((sum, quantity) => sum + quantity, 0)
  const complete = canChooseSet && groups.every((group) => group.products.reduce((sum, product) => sum + (chosen[product.sku] || 0), 0) === group.quantity)
  const quantities = new Map(lines.map((line) => [String(line.sku), line.quantity]))
  const totalRequired = groups.reduce((sum, group) => sum + (group.quantity || 0), 0)
  return <Confirm title={data?.label || offer.label} onClose={onClose}>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!data && !error && <p role="status">Loading the products in this offer…</p>}
    {data && <>
      {data.description && <details className="offer-terms"><summary>Offer details</summary><p>{data.description}</p></details>}
      {canChooseSet && <div className="offer-selection"><p aria-live="polite"><strong>{count} of {totalRequired} chosen</strong> · These are extra packs to add.</p><button type="button" className="button button-primary" disabled={locked || !complete || originalTarget !== targetId || sent} onClick={async () => {
        setSent(true)
        const items = Object.entries(chosen).filter(([, quantity]) => quantity > 0).map(([sku, quantity]) => ({ sku, quantity, existing_quantity: quantities.get(sku) || 0 }))
        const accepted = await addOffer({ ...selection, target_id: originalTarget, offer_id: offer.id, items })
        if (accepted) onClose()
      }}>Add {totalRequired} {totalRequired === 1 ? 'item' : 'items'} to basket</button>{sent && <p role="status">If the request failed, close and reopen this offer to review before trying again.</p>}</div>}
      {originalTarget !== targetId && <p className="inline-error" role="alert">Your delivery changed. Reopen this offer before adding items.</p>}
      <label className="search-field"><IconSearch size={20} /><input type="search" aria-label="Find within this offer" placeholder="Find within this offer…" value={filter} onChange={(event) => setFilter(event.target.value)} /></label>
      {groups.map((group, index) => {
        const selected = group.products.reduce((sum, product) => sum + (chosen[product.sku] || 0), 0)
        const visible = group.products.filter((product) => product.name.toLowerCase().includes(filter.toLowerCase()))
        return <section className="offer-group" key={index}>
          {canChooseSet && <h3>Choose {group.quantity}{groups.length > 1 ? ` from group ${index + 1}` : ''} <span className="muted">· {selected} chosen</span></h3>}
          {!visible.length && <p>No matching products in this group.</p>}
          <div className="offer-products">{visible.map((product) => <article className="basket-line" key={product.sku}><ProductImage src={product.image_url} /><div className="product-information"><h3>{product.name}</h3><p className="pack">{product.pack}{product.in_stock === false ? ' · Out of stock' : ''}</p><div className="line-bottom"><p className="line-price"><strong>{money(product.price)}</strong><small>{quantities.get(product.sku) ? `${quantities.get(product.sku)} already in basket` : 'before multibuy savings'}</small></p><Quantity name={product.name} quantity={canChooseSet ? chosen[product.sku] || 0 : quantities.get(product.sku) || 0} disabled={locked || sent || originalTarget !== targetId} unavailable={product.in_stock === false || (canChooseSet && selected >= group.quantity)} onChange={(quantity) => canChooseSet ? setChosen((previous) => ({ ...previous, [product.sku]: quantity })) : addSingle({ kind: 'quantity', sku: product.sku, name: product.name, quantity })} /></div></div></article>)}</div>
        </section>
      })}
      <p className="muted">Ocado confirms availability, offer eligibility and the final price when your changes are sent.</p>
    </>}
  </Confirm>
}

function Search({ lines, locked, action, reportError, onOffer }) {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  useEffect(() => {
    const controller = new AbortController()
    const value = query.trim()
    setResult(null)
    setError(null)
    setLoading(Boolean(value))
    if (!value) return () => controller.abort()
    const timer = window.setTimeout(async () => {
      try {
        const next = await request(`/search?q=${encodeURIComponent(value)}`, { signal: controller.signal })
        if (!controller.signal.aborted) setResult(next)
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(failure.message)
          if (failure.status === 401 || failure.status === 403) reportError(failure)
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }, 350)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [query, reportError])
  const quantities = new Map(lines.map((line) => [String(line.sku), line.quantity]))
  return <>
    <div className="section-heading"><h2>Search groceries</h2></div>
    <label className="search-field"><IconSearch size={22} /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search for milk, bread, fish…" aria-label="Search Ocado products" autoComplete="off" />{query && <button type="button" aria-label="Clear search" onClick={() => setQuery('')}><IconX size={18} /></button>}</label>
    <p className="section-copy">Tap + to add an item. We’ll save your changes for you.</p>
    {loading && <p className="loading" role="status">Searching the shelves…</p>}
    {error && <div className="inline-error" role="alert">{error}</div>}
    {!query.trim() && <Empty icon={IconSearch} title="What’s on your list?">Find your favourites, then tap + to add them.</Empty>}
    {result && !result.products?.length && <Empty icon={IconSearch} title="Nothing on this shelf">Try another name or a simpler search.</Empty>}
    {result?.products?.length > 0 && <><p className="results-label" aria-live="polite">{result.products.length} results for “{result.query}”</p><div className="search-grid">{result.products.map((product) => <article className="search-product" key={product.sku}><div className="product-topline"><ProductImage src={product.image_url} />{product.in_stock === false && <span className="stock-badge">Out of stock</span>}</div><h3>{product.name || product.sku}</h3><p className="pack">{product.pack}</p><Offers offers={product.offers} onOffer={onOffer} /><div className="product-bottom"><div><strong>{money(product.price)}</strong>{product.base_price != null && <span className="was-price">was {money(product.base_price)}</span>}{product.unit_price != null && <small>{money(product.unit_price)}{product.unit_basis ? ` / ${product.unit_basis}` : ''}</small>}</div><Quantity name={product.name || product.sku} quantity={quantities.get(String(product.sku)) || 0} disabled={locked} unavailable={product.in_stock === false} onChange={(quantity) => action({ kind: 'quantity', sku: product.sku, name: product.name, quantity })} /></div></article>)}</div></>}
  </>
}

function Slots({ queue, locked, action, reportError }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    setSelected(null)
    request('/slots', { signal: controller.signal }).then((next) => {
      if (!controller.signal.aborted) setItems(next.items || [])
    }).catch((failure) => {
      if (!controller.signal.aborted) {
        setError(failure.message)
        if (failure.status === 401 || failure.status === 403) reportError(failure)
      }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [queue?.target_id, reload, reportError])
  const [openDay, setOpenDay] = useState(null)
  const [openPeriod, setOpenPeriod] = useState(null)
  const groups = new Map()
  for (const slot of items || []) {
    const key = slot.day || (slot.start ? formatDate(slot.start, { year: 'numeric', month: '2-digit', day: '2-digit' }) : 'Date unavailable')
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(slot)
  }
  const queued = queue?.actions?.findLast((item) => item.kind === 'slot')
  return <>
    <div className="section-heading"><h2>Delivery times</h2><button type="button" className="text-button" disabled={loading} onClick={() => setReload((value) => value + 1)}>Refresh</button></div>
    <p className="section-copy">Choose a day, then a time that suits you. We’ll save your choice automatically after you confirm.</p>
    {loading && <p className="loading" role="status">Finding delivery times…</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!loading && !error && !items?.length && <Empty icon={IconCalendarWeek} title="No delivery slots available">Try refreshing later. Your current booking has not changed.</Empty>}
    {!loading && !error && [...groups].map(([date, slots]) => {
      const available = slots.filter((slot) => slot.available)
      const periods = ['Morning', 'Afternoon', 'Evening', 'Other times'].map((label) => ({ label, slots: slots.filter((slot) => {
        const hour = slot.start ? Number(formatDate(slot.start, { hour: '2-digit', hourCycle: 'h23' })) : NaN
        return label === (Number.isNaN(hour) ? 'Other times' : hour < 12 ? 'Morning' : hour < 17 ? 'Afternoon' : 'Evening')
      }) })).filter((period) => period.slots.length)
      const label = slots[0].day ? day(`${slots[0].day}T12:00:00`) : day(slots[0].start)
      const prices = available.map((slot) => slot.price).filter((price) => price != null)
      return <section className="slot-day" key={date}>
        <button type="button" className="day-toggle" aria-expanded={openDay === date} aria-controls={`day-${date.replaceAll(/[^a-zA-Z0-9]/g, '')}`} onClick={() => { setOpenDay(openDay === date ? null : date); setOpenPeriod(null); setSelected(null) }}><span><strong>{label}</strong><small>{available.length ? `${available.length} times${prices.length ? ` · from ${money(Math.min(...prices))}` : ''}` : 'No available times'}</small></span><span aria-hidden="true">{openDay === date ? '−' : '+'}</span></button>
        <div id={`day-${date.replaceAll(/[^a-zA-Z0-9]/g, '')}`} hidden={openDay !== date}>{periods.map((period) => <section className="slot-period" key={period.label}>
          <button type="button" className="period-toggle" aria-expanded={openPeriod === period.label} onClick={() => { setOpenPeriod(openPeriod === period.label ? null : period.label); setSelected(null) }}><span>{period.label}<small>{period.label === 'Morning' ? 'Before 12 noon' : period.label === 'Afternoon' ? '12 noon – 5pm' : period.label === 'Evening' ? 'From 5pm' : ''}</small></span><span aria-hidden="true">{openPeriod === period.label ? '−' : '+'}</span></button>
          <div className="slot-grid" hidden={openPeriod !== period.label}>{period.slots.map((slot) => <button type="button" className={`slot-option ${selected?.slot_id === slot.slot_id ? 'selected' : ''}`} key={slot.slot_id} aria-pressed={selected?.slot_id === slot.slot_id} disabled={!slot.available || locked} onClick={() => setSelected({ ...slot, target_id: queue.target_id })}><strong>{slotTime(slot.start, slot.end)}</strong><span>{slot.available ? money(slot.price) : 'Unavailable'}</span>{slot.eco && <small className="eco"><IconLeaf size={14} />Greener slot</small>}{queued?.slot_id === slot.slot_id && <small>Your new choice</small>}</button>)}</div>
        </section>)}</div>
      </section>
    })}
    {selected && <section className="slot-confirm"><div><span className="eyebrow">Your selection</span><h3>{day(selected.start)}</h3><p>{slotTime(selected.start, selected.end)} · {money(selected.price)}</p></div><button type="button" className="button button-primary" disabled={locked || loading || Boolean(error) || selected.target_id !== queue.target_id} onClick={async () => { const accepted = await action({ kind: 'slot', target_id: selected.target_id, slot_id: selected.slot_id, start: selected.start, end: selected.end }); if (accepted) setSelected(null) }}>Choose this time<IconArrowRight size={18} /></button></section>}
  </>
}

function Account({ auth, busy, authenticate, accessExpired, selectAccount, queueBlocked }) {
  const [email, setEmail] = useState(auth?.email || '')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [confirmLogout, setConfirmLogout] = useState(false)
  const [accountId, setAccountId] = useState(auth?.account_id ?? '')
  useEffect(() => { setAccountId(auth?.account_id ?? ''); setConfirmLogout(false) }, [auth?.account_id, auth?.account_version])
  useEffect(() => { setEmail(auth?.email || ''); setPassword(''); setCode('') }, [auth?.email, auth?.account_id])
  const otp = auth?.status === 'awaiting_otp' || auth?.status === 'otp_required'
  return <>
    <div className="section-heading"><div><span className="eyebrow">One account, together</span><h2>Shared account</h2></div></div>
    {auth?.accounts?.length > 0 && <form className="account-form account-choice" onSubmit={async (event) => { event.preventDefault(); await selectAccount(accountId) }}>
      <label>Ocado account for everyone<select value={accountId} onChange={(event) => setAccountId(event.target.value)} disabled={busy || accessExpired || queueBlocked}>
        {!auth.account_id && <option value="">Choose an account</option>}
        {auth.accounts.map((account) => <option key={account.id} value={account.id}>{account.label}{account.email ? ` · ${account.email}` : ''}</option>)}
      </select></label>
      <p>Everyone uses this selection for the basket, delivery and shared changes.</p>
      {queueBlocked && <p role="status">Save or cancel the changes before switching accounts.</p>}
      <button type="submit" className="button button-primary" disabled={busy || accessExpired || queueBlocked || !accountId || Number(accountId) === auth.account_id}>{busy ? 'Changing account…' : 'Use this account for everyone'}</button>
    </form>}
    <div className="account-status"><span className={`status-dot ${isReady(auth) ? 'connected' : ''}`} /><div><strong>{isReady(auth) ? 'Ocado is connected' : otp ? 'One-time code needed' : 'Connect to Ocado'}</strong><p>{auth?.email || 'The household’s shared Ocado account'}</p></div><span className="status-pill">{auth?.status?.replaceAll('_', ' ') || 'Checking'}</span></div>
    <p className="section-copy">Everyone authorised for this app uses the same Ocado login, delivery, saved payment method and changes. Signing out disconnects it for everyone.</p>
    {auth?.stage && auth.stage !== 'idle' && <p className="stage" role="status">Authentication: {auth.stage.replaceAll('_', ' ')}</p>}
    {!isReady(auth) && !otp && <form className="account-form" onSubmit={async (event) => { event.preventDefault(); const secret = password; setPassword(''); await authenticate('/login', { email: email.trim(), password: secret }) }}><label>Email address<input type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} disabled={busy || accessExpired} /></label><label>Password<input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy || accessExpired} /></label><button className="button button-primary" type="submit" disabled={busy || accessExpired}>{busy ? 'Connecting…' : 'Connect shared account'}<IconArrowRight size={18} /></button></form>}
    {otp && <form className="account-form" onSubmit={async (event) => { event.preventDefault(); const accepted = await authenticate('/otp', { code: code.trim() }); if (accepted) setCode('') }}><p>Enter the one-time code sent by Ocado to finish connecting the shared account.</p><label>One-time code<input type="text" inputMode="numeric" autoComplete="one-time-code" required value={code} onChange={(event) => setCode(event.target.value)} disabled={busy || accessExpired} /></label><button type="submit" className="button button-primary" disabled={busy || accessExpired}>{busy ? 'Verifying…' : 'Verify code'}</button></form>}
    <div className="account-actions"><button type="button" className="button button-outline" disabled={busy || accessExpired} onClick={() => authenticate('/session/refresh')}>{busy ? 'Please wait…' : 'Refresh Ocado session'}</button>{auth?.status && auth.status !== 'logged_out' && <button type="button" className="text-button danger-text" disabled={busy || accessExpired} onClick={() => setConfirmLogout(true)}>Disconnect shared account</button>}</div>
    {confirmLogout && <Confirm title="Disconnect for everyone?" onClose={() => setConfirmLogout(false)}><p>This logs the shared Ocado account out for every authorised user. It does not cancel a booked delivery.</p><button type="button" className="button button-danger" disabled={busy} onClick={async () => { const accepted = await authenticate('/logout'); if (accepted) setConfirmLogout(false) }}>Disconnect shared account</button></Confirm>}
  </>
}

function Confirm({ title, children, onClose, closeLabel = 'Go back' }) {
  const ref = useRef(null)
  const titleId = useId()
  useEffect(() => { ref.current.showModal() }, [])
  return <dialog ref={ref} className="confirm-dialog" aria-labelledby={titleId} onCancel={onClose} onClose={onClose}><div className="dialog-heading"><h2 id={titleId}>{title}</h2><button type="button" className="icon-button" aria-label="Close confirmation" onClick={onClose}><IconX size={22} /></button></div>{children}<button type="button" className="button button-outline" onClick={onClose}>{closeLabel}</button></dialog>
}

export default function App() {
  const shop = useSharedShop()
  const [tab, setTab] = useState('basket')
  const [now, setNow] = useState(Date.now())
  const [checkout, setCheckout] = useState(false)
  const [offer, setOffer] = useState(null)
  const [online, setOnline] = useState(navigator.onLine)
  useEffect(() => { setCheckout(false); setOffer(null) }, [shop.auth?.account_id, shop.auth?.account_version])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    const update = () => setOnline(navigator.onLine)
    window.addEventListener('online', update)
    window.addEventListener('offline', update)
    return () => { window.clearInterval(timer); window.removeEventListener('online', update); window.removeEventListener('offline', update) }
  }, [])
  const ready = isReady(shop.auth)
  const canManageSettings = shop.auth?.can_manage_settings === true
  const currentTab = tab === 'account' && !canManageSettings ? 'basket' : tab
  const locked = shop.locked || !online
  // Quantity actions are absolute, so overlaying the queue is idempotent even
  // when the server basket already includes those projected quantities.
  const projected = new Map((shop.state?.basket?.lines || []).map((line) => [String(line.sku), { ...line }]))
  if (shop.state?.queue?.target_id === shop.queue?.target_id) {
    // Rebuild from confirmed quantities before overlaying the latest edits.
    for (const line of projected.values()) {
      if (shop.queue.status !== 'succeeded') line.quantity = shop.state.confirmed_quantities?.[String(line.sku)] ?? 0
    }
    for (const action of shop.queue?.actions || []) {
      if (action.kind !== 'quantity') continue
      const key = String(action.sku)
      const existing = projected.get(key)
      projected.set(key, { ...existing, cost: existing?.quantity === action.quantity ? existing.cost : null, original_cost: existing?.quantity === action.quantity ? existing.original_cost : null, sku: action.sku, name: action.name || existing?.name || action.sku, quantity: action.quantity })
    }
  }
  const lines = [...projected.values()].filter((line) => line.quantity > 0)
  const basket = shop.state?.basket
  const order = shop.state?.order
  const totalItems = lines.reduce((sum, line) => sum + line.quantity, 0)
  const navigate = (next) => { setTab(next); document.getElementById('shop-content')?.focus({ preventScroll: true }) }
  const basketProps = { lines, basket, order, queue: shop.queue, locked, action: shop.action, onSearch: () => navigate('search'), onOffer: setOffer, onCheckout: () => setCheckout({ ...shop.selection, target_id: shop.state.queue.target_id }) }
  return <div className="ocado-app">
    <a className="skip-link" href="#shop-content">Skip to shop</a>
    <header className="app-header"><div className="header-inner"><a className="brand" href="#" onClick={(event) => { event.preventDefault(); navigate('basket') }} aria-label="Shared shop basket"><span className="brand-mark"><IconLeaf size={24} stroke={1.6} /></span><span>ocado<span className="brand-subtitle">the shared shop</span></span></a><button type="button" className="text-button" disabled={shop.busy || shop.syncing || !online || !ready} onClick={shop.refresh}>{shop.syncing ? 'Updating…' : 'Refresh'}</button></div></header>
    <div className="app-layout"><EditingStatus key={`${shop.auth?.account_id}:${shop.auth?.account_version}`} queue={shop.queue} now={now} busy={shop.busy || !online || shop.accessExpired} cancel={() => shop.cancel({ ...shop.selection, target_id: shop.queue.target_id })} retry={() => shop.retry({ ...shop.selection, target_id: shop.queue.target_id })} check={() => shop.check({ ...shop.selection, target_id: shop.queue.target_id })} /><DeliveryCard order={order} queue={shop.queue} ready={ready} loading={shop.loading} onSlots={() => navigate('slots')} total={basket?.total} />
    <div className="workspace">
      {!online && <div className="notice notice-error" role="alert"><strong>You’re offline.</strong> Reconnect before making changes. Nothing is automatically retried.</div>}
      {shop.error && <div className="notice notice-error" role="alert"><div><strong>{shop.accessExpired ? 'Sign in again to continue' : 'We couldn’t complete that request'}</strong><p>{shop.error}</p></div>{shop.accessExpired ? <a className="button button-outline button-small" href={window.location.pathname + window.location.search}>Sign in again</a> : <button type="button" className="button button-outline button-small" disabled={shop.busy || shop.syncing} onClick={() => { shop.clearError(); void shop.refresh() }}>Refresh shop</button>}</div>}
      <main id="shop-content" className="content-panel" tabIndex={-1} aria-busy={shop.loading || shop.syncing}>
        {shop.loading && !shop.auth ? <div className="loading-block" role="status"><span className="spinner" /><h2>Opening the shared shop</h2><p>Finding your next delivery and everyone’s latest changes.</p></div> : <>
          {ready && shop.stale && <div className="notice notice-error" role="status">Refresh the basket before making changes.<button type="button" className="text-button" onClick={shop.refresh} disabled={shop.busy || shop.syncing || !online}>Refresh</button></div>}
          {(canManageSettings && (!ready || currentTab === 'account')) ? <Account key={`${shop.auth?.account_id}:${shop.auth?.account_version}`} selectAccount={shop.selectAccount} queueBlocked={['pending', 'running', 'failed'].includes(shop.queue?.status)} auth={shop.auth} busy={shop.busy} authenticate={shop.authenticate} accessExpired={shop.accessExpired} /> : !ready ? <Empty icon={IconTruckDelivery} title="Your shop will be back soon">Please ask Nikhil to reconnect Ocado. Your booked delivery is still with Ocado.</Empty> : !shop.state ? <Empty icon={IconTruckDelivery} title="Your shop isn’t available yet">{shop.syncing ? 'Loading your booked delivery and basket…' : 'Refresh the shop to try again. No changes have been sent.'}</Empty> : <>
            {currentTab === 'basket' && <Basket {...basketProps} />}
            {currentTab === 'search' && <Search key={`${shop.auth?.account_id}:${shop.auth?.account_version}`} lines={lines} locked={locked} action={shop.action} reportError={shop.reportError} onOffer={setOffer} />}
            {currentTab === 'slots' && <Slots key={`${shop.auth?.account_id}:${shop.auth?.account_version}`} queue={shop.queue} locked={locked} action={shop.action} reportError={shop.reportError} />}
          </>}
        </>}
      </main><p className="app-footnote">Prices and offers are confirmed by Ocado. Times shown for the UK.</p>
    </div></div>
    <nav className="bottom-nav" aria-label="Shared shop navigation">{tabs.filter(({ id }) => id !== 'account' || canManageSettings).map(({ id, label, icon: Icon }) => <button type="button" key={id} className={currentTab === id ? 'active' : ''} aria-current={currentTab === id ? 'page' : undefined} onClick={() => navigate(id)}><span className="nav-icon"><Icon size={23} stroke={currentTab === id ? 2 : 1.6} />{id === 'basket' && totalItems > 0 && <span className="nav-badge">{totalItems > 99 ? '99+' : totalItems}</span>}</span><span>{label}</span></button>)}</nav>
    {offer && <OfferPicker key={`${shop.auth?.account_id}:${shop.auth?.account_version}:${offer.id}`} offer={offer} lines={lines} locked={locked} selection={shop.selection} targetId={shop.queue?.target_id} addOffer={shop.addOffer} addSingle={shop.action} reportError={shop.reportError} onClose={() => setOffer(null)} />}
    {checkout && <Confirm title="Place your order?" onClose={() => setCheckout(false)}>
      {checkout.target_id !== shop.queue?.target_id && <p className="inline-error" role="alert">Your delivery has changed. Go back and review your basket before confirming.</p>}
      <p>You are confirming checkout of the <strong>{order ? 'booked delivery' : 'active shared basket'}</strong> with {totalItems} items, using the shared account’s saved payment method.</p>
      <div className="confirmation-total"><span>Last known total</span><strong>{money(basket?.total)}</strong></div>
      <p>Your changes may affect the final total. We’ll place your order when you’ve finished editing. You can cancel your changes before saving starts.</p>
      <p className="muted">If Ocado needs you to verify payment, we’ll let you know. We won’t try a payment again automatically.</p>
      <button type="button" className="button button-primary" disabled={locked || checkout.target_id !== shop.queue?.target_id || !totalItems || shop.queue?.actions?.some((item) => item.kind === 'checkout')} onClick={async () => { const accepted = await shop.action({ ...checkout, kind: 'checkout', confirm: true }); if (accepted) setCheckout(false) }}>{shop.busy ? 'Please wait…' : 'Confirm order'}</button>
    </Confirm>}
  </div>
}
