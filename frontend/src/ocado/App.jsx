import { useEffect, useRef, useState } from 'react'
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
  { id: 'delivery', label: 'Delivery', icon: IconTruckDelivery },
  { id: 'search', label: 'Search', icon: IconSearch },
  { id: 'basket', label: 'Basket', icon: IconBasket },
  { id: 'slots', label: 'Slots', icon: IconCalendarWeek },
  { id: 'account', label: 'Account', icon: IconUserCircle },
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

function DeliveryCard({ order, queue, ready, loading, onSlots }) {
  const moved = queue?.actions?.findLast((action) => action.kind === 'slot')
  return <section className="delivery-card" aria-label="Current delivery">
    <div className="delivery-heading"><span className="eyebrow">{order ? 'Your next delivery' : 'Your next shop'}</span><span className="delivery-symbol"><IconTruckDelivery size={31} stroke={1.4} /></span></div>
    {order ? <>
      <h1>{day(order.delivery_start)}</h1>
      <p className="delivery-time">{slotTime(order.delivery_start, order.delivery_end)}</p>
      {order.address && <p className="delivery-address">{order.address}</p>}
      <div className="deadline"><IconClock size={17} /><span>{order.edit_until ? `Edit until ${day(order.edit_until)}, ${time(order.edit_until)}` : 'Edit deadline unavailable'}{!order.editable && ' · Not currently editable'}</span></div>
    </> : <>
      <h1>{loading ? 'Finding your delivery…' : ready ? 'A fresh start for your shop.' : 'Good food. Shared simply.'}</h1>
      <p className="delivery-time">{ready ? 'No upcoming booked delivery' : 'Connect the shared Ocado account to see your next delivery.'}</p>
      <div className="deadline"><IconClock size={17} /><span>{ready ? 'Choose a slot for your active basket. An edit deadline appears after booking.' : 'Delivery time and edit deadline appear here once connected.'}</span></div>
    </>}
    {moved && <p className="queued-slot"><IconCalendarWeek size={17} />Queued move: {day(moved.start)}, {slotTime(moved.start, moved.end)}</p>}
    {ready && <button type="button" className="delivery-link" onClick={onSlots}>{order ? 'Find another delivery time' : 'Choose a delivery time'}<IconArrowRight size={18} /></button>}
  </section>
}

function QueueBanner({ queue, busy, cancel, now }) {
  if (!queue || queue.status === 'idle') return null
  const seconds = queue.due_at ? Math.max(0, Math.ceil((Date.parse(queue.due_at) - now) / 1000)) : 0
  const countdown = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
  const headings = { pending: seconds > 0 ? `Shared changes send in ${countdown}` : 'Shared changes are ready to send', running: 'Sending the shared changes…', failed: 'The shared changes need attention', succeeded: 'The shared shop is up to date' }
  return <section className={`queue-banner queue-${queue.status}`} aria-label="Shared change queue">
    <div className="queue-top"><span className="queue-icon">{queue.status === 'succeeded' ? <IconCheck size={20} /> : <IconClock size={20} />}</span><div><h2 aria-live="polite">{headings[queue.status] || queue.status}</h2><p>{queue.status === 'pending' ? 'Anyone’s next change restarts the 3-minute timer. Nothing is sent immediately.' : queue.status === 'running' ? 'Please wait. New changes and cancellation are paused while Ocado applies this batch.' : queue.status === 'failed' ? 'No automatic retry. Check the actual order before clearing these changes, especially after a checkout error.' : 'Changes from everyone using this account appear here.'}</p></div></div>
    {queue.error && <p className="queue-error" role="alert">{queue.error}</p>}
    {queue.actions?.length > 0 && <details className="queue-details"><summary>{queue.actions.length} shared queued {queue.actions.length === 1 ? 'change' : 'changes'}</summary><ul>{queue.actions.map((action) => <li key={action.id}>{action.kind === 'quantity' ? `${action.name || action.sku}: ${action.quantity === 0 ? 'remove from basket' : `quantity ${action.quantity}`}` : action.kind === 'slot' ? `Delivery: ${day(action.start)}, ${slotTime(action.start, action.end)}` : 'Confirmed checkout'}<span className="muted"> · user {action.user_id}</span></li>)}</ul></details>}
    {(queue.status === 'pending' || queue.status === 'failed') && <button type="button" className="button button-outline button-small" disabled={busy} onClick={cancel}>{busy ? 'Please wait…' : queue.status === 'failed' ? 'Clear failed changes' : 'Cancel all queued changes'}</button>}
  </section>
}

function Basket({ lines, basket, order, queue, locked, action, onSearch, onCheckout }) {
  const pending = queue?.status === 'pending' || queue?.status === 'running' || queue?.status === 'failed'
  const quantity = lines.reduce((sum, line) => sum + line.quantity, 0)
  return <>
    <div className="section-heading"><div><span className="eyebrow">The shared shop</span><h2>Your basket <span className="count">{quantity}</span></h2></div><button type="button" className="text-button" onClick={onSearch}>Add items<IconPlus size={17} /></button></div>
    <p className="section-copy">{pending ? 'Projected quantities include everyone’s queued changes. Prices and totals may not reflect those changes yet.' : 'One basket, shared by everyone. Quantity changes are queued for three minutes.'}</p>
    {!lines.length ? <Empty title="Room for something delicious">Search for your favourites and start adding to the shared basket.</Empty> : <div className="product-list">{lines.map((line) => <article className="basket-line" key={line.sku}><span className="product-placeholder"><IconShoppingBag size={23} stroke={1.3} /></span><div className="product-information"><h3>{line.name || line.sku}</h3><p>{line.cost == null ? 'Price unavailable' : `${money(line.cost)} · ${pending ? 'last known line total' : 'line total'}`}</p></div><Quantity name={line.name || line.sku} quantity={line.quantity} disabled={locked} onChange={(next) => action({ kind: 'quantity', sku: line.sku, name: line.name || line.sku, quantity: next })} /></article>)}</div>}
    <section className="basket-summary">
      <div><span>{pending ? 'Last known basket total' : 'Basket total'}</span><strong>{money(basket?.total)}</strong></div>
      {basket?.minimum != null && <p>Minimum order {money(basket.minimum)}. Delivery and final prices are confirmed by Ocado.</p>}
      {basket?.restrictions?.length > 0 && <ul className="restrictions">{basket.restrictions.map((restriction, index) => <li key={index}>{restriction}</li>)}</ul>}
      {!order && basket?.can_checkout === false && <p>Ocado does not currently mark this basket ready for checkout. Queued items or a slot may resolve this; eligibility is checked again after applying your changes.</p>}
      <button type="button" className="button button-primary" disabled={locked || !lines.length || queue?.actions?.some((item) => item.kind === 'checkout')} onClick={onCheckout}>{queue?.actions?.some((item) => item.kind === 'checkout') ? 'Checkout is queued' : 'Review & queue checkout'}<IconArrowRight size={18} /></button>
      <p className="checkout-note">{order ? 'This delivery is already booked. Queued item and slot edits automatically re-check out this order when the timer ends.' : 'Checkout uses the shared account’s saved payment method. Review before confirming.'}</p>
    </section>
  </>
}

function Search({ lines, locked, action, reportError }) {
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
    <div className="section-heading"><div><span className="eyebrow">Make something good</span><h2>Find your favourites</h2></div></div>
    <label className="search-field"><IconSearch size={22} /><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Milk, bread, something for dinner…" aria-label="Search Ocado products" autoComplete="off" />{query && <button type="button" aria-label="Clear search" onClick={() => setQuery('')}><IconX size={18} /></button>}</label>
    <p className="section-copy">Live Ocado prices and stock. Adding an item updates the shared queue, not the order immediately.</p>
    {loading && <p className="loading" role="status">Searching the shelves…</p>}
    {error && <div className="inline-error" role="alert">{error}</div>}
    {!query.trim() && <Empty icon={IconSearch} title="What’s on your list?">Search the Ocado shelves, then use + to add to the shared basket.</Empty>}
    {result && !result.products?.length && <Empty icon={IconSearch} title="Nothing on this shelf">Try another name or a simpler search.</Empty>}
    {result?.products?.length > 0 && <><p className="results-label" aria-live="polite">{result.products.length} results for “{result.query}”</p><div className="search-grid">{result.products.map((product) => <article className="search-product" key={product.sku}><div className="product-topline"><span className="product-placeholder"><IconShoppingBag size={26} stroke={1.3} /></span>{product.in_stock === false && <span className="stock-badge">Out of stock</span>}</div><h3>{product.name || product.sku}</h3><p className="pack">{product.pack || 'Pack information unavailable'}</p><div className="product-bottom"><div><strong>{money(product.price)}</strong>{product.unit_price != null && <small>{money(product.unit_price)}{product.unit_basis ? ` / ${product.unit_basis}` : ''}</small>}</div><Quantity name={product.name || product.sku} quantity={quantities.get(String(product.sku)) || 0} disabled={locked} unavailable={product.in_stock === false} onChange={(quantity) => action({ kind: 'quantity', sku: product.sku, name: product.name, quantity })} /></div></article>)}</div></>}
  </>
}

function Slots({ queue, order, locked, action, reportError }) {
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
  const groups = new Map()
  for (const slot of items || []) {
    const key = slot.start ? day(slot.start) : slot.day || 'Date unavailable'
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(slot)
  }
  const queued = queue?.actions?.findLast((item) => item.kind === 'slot')
  return <>
    <div className="section-heading"><div><span className="eyebrow">A time that suits everyone</span><h2>Delivery slots</h2></div><button type="button" className="text-button" disabled={loading} onClick={() => setReload((value) => value + 1)}>Refresh</button></div>
    <p className="section-copy">{order ? 'Choose a replacement for the booked delivery.' : 'Choose a delivery time for the active basket.'} A selection is only sent after you queue it and the shared timer finishes. Availability can change.</p>
    {loading && <p className="loading" role="status">Finding delivery times…</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!loading && !error && !items?.length && <Empty icon={IconCalendarWeek} title="No delivery slots available">Try refreshing later. Your current booking has not changed.</Empty>}
    {!loading && !error && [...groups].map(([date, slots]) => <section className="slot-day" key={date}><h3>{date}</h3><div className="slot-grid">{slots.map((slot) => <button type="button" className={`slot-option ${selected?.slot_id === slot.slot_id ? 'selected' : ''}`} key={slot.slot_id} aria-pressed={selected?.slot_id === slot.slot_id} disabled={!slot.available || locked} onClick={() => setSelected({ ...slot, target_id: queue.target_id })}><strong>{slotTime(slot.start, slot.end)}</strong><span>{slot.available ? money(slot.price) : 'Unavailable'}</span>{slot.eco && <small className="eco"><IconLeaf size={14} />Greener slot</small>}{queued?.slot_id === slot.slot_id && <small>Currently queued</small>}</button>)}</div></section>)}
    {selected && <section className="slot-confirm"><div><span className="eyebrow">Your selection</span><h3>{day(selected.start)}</h3><p>{slotTime(selected.start, selected.end)} · {money(selected.price)}</p></div><button type="button" className="button button-primary" disabled={locked || loading || Boolean(error) || selected.target_id !== queue.target_id} onClick={async () => { const accepted = await action({ kind: 'slot', target_id: selected.target_id, slot_id: selected.slot_id, start: selected.start, end: selected.end }); if (accepted) setSelected(null) }}>Queue {order ? 'delivery move' : 'delivery slot'}<IconArrowRight size={18} /></button></section>}
  </>
}

function Account({ auth, busy, authenticate, accessExpired }) {
  const [email, setEmail] = useState(auth?.email || '')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [confirmLogout, setConfirmLogout] = useState(false)
  useEffect(() => { if (auth?.email) setEmail(auth.email) }, [auth?.email])
  const otp = auth?.status === 'awaiting_otp' || auth?.status === 'otp_required'
  return <>
    <div className="section-heading"><div><span className="eyebrow">One account, together</span><h2>Shared account</h2></div></div>
    <div className="account-status"><span className={`status-dot ${isReady(auth) ? 'connected' : ''}`} /><div><strong>{isReady(auth) ? 'Ocado is connected' : otp ? 'One-time code needed' : 'Connect to Ocado'}</strong><p>{auth?.email || 'The household’s shared Ocado account'}</p></div><span className="status-pill">{auth?.status?.replaceAll('_', ' ') || 'Checking'}</span></div>
    <p className="section-copy">Everyone authorised for this app uses the same Ocado login, delivery, saved payment method and change queue. Signing out disconnects it for everyone.</p>
    {auth?.stage && auth.stage !== 'idle' && <p className="stage" role="status">Authentication: {auth.stage.replaceAll('_', ' ')}</p>}
    {!isReady(auth) && !otp && <form className="account-form" onSubmit={async (event) => { event.preventDefault(); const secret = password; setPassword(''); await authenticate('/login', { email: email.trim(), password: secret }) }}><label>Email address<input type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} disabled={busy || accessExpired} /></label><label>Password<input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy || accessExpired} /></label><button className="button button-primary" type="submit" disabled={busy || accessExpired}>{busy ? 'Connecting…' : 'Connect shared account'}<IconArrowRight size={18} /></button></form>}
    {otp && <form className="account-form" onSubmit={async (event) => { event.preventDefault(); const accepted = await authenticate('/otp', { code: code.trim() }); if (accepted) setCode('') }}><p>Enter the one-time code sent by Ocado to finish connecting the shared account.</p><label>One-time code<input type="text" inputMode="numeric" autoComplete="one-time-code" required value={code} onChange={(event) => setCode(event.target.value)} disabled={busy || accessExpired} /></label><button type="submit" className="button button-primary" disabled={busy || accessExpired}>{busy ? 'Verifying…' : 'Verify code'}</button></form>}
    <div className="account-actions"><button type="button" className="button button-outline" disabled={busy || accessExpired} onClick={() => authenticate('/session/refresh')}>{busy ? 'Please wait…' : 'Refresh Ocado session'}</button>{auth?.status && auth.status !== 'logged_out' && <button type="button" className="text-button danger-text" disabled={busy || accessExpired} onClick={() => setConfirmLogout(true)}>Disconnect shared account</button>}</div>
    {confirmLogout && <Confirm title="Disconnect for everyone?" onClose={() => setConfirmLogout(false)}><p>This logs the shared Ocado account out for every authorised user. It does not cancel a booked delivery.</p><button type="button" className="button button-danger" disabled={busy} onClick={async () => { const accepted = await authenticate('/logout'); if (accepted) setConfirmLogout(false) }}>Disconnect shared account</button></Confirm>}
  </>
}

function Confirm({ title, children, onClose }) {
  const ref = useRef(null)
  useEffect(() => { ref.current.showModal() }, [])
  return <dialog ref={ref} className="confirm-dialog" aria-labelledby="confirm-title" onCancel={onClose} onClose={onClose}><div className="dialog-heading"><h2 id="confirm-title">{title}</h2><button type="button" className="icon-button" aria-label="Close confirmation" onClick={onClose}><IconX size={22} /></button></div>{children}<button type="button" className="button button-outline" onClick={onClose}>Go back</button></dialog>
}

export default function App() {
  const shop = useSharedShop()
  const [tab, setTab] = useState('delivery')
  const [now, setNow] = useState(Date.now())
  const [checkout, setCheckout] = useState(false)
  const [cancelQueue, setCancelQueue] = useState(false)
  const [online, setOnline] = useState(navigator.onLine)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    const update = () => setOnline(navigator.onLine)
    window.addEventListener('online', update)
    window.addEventListener('offline', update)
    return () => { window.clearInterval(timer); window.removeEventListener('online', update); window.removeEventListener('offline', update) }
  }, [])
  const ready = isReady(shop.auth)
  const locked = shop.locked || !online
  // Quantity actions are absolute, so overlaying the queue is idempotent even
  // when the server basket already includes those projected quantities.
  const projected = new Map((shop.state?.basket?.lines || []).map((line) => [String(line.sku), { ...line }]))
  if (shop.state?.queue?.target_id === shop.queue?.target_id) {
    for (const action of shop.queue?.actions || []) {
      if (action.kind !== 'quantity') continue
      const key = String(action.sku)
      const existing = projected.get(key)
      projected.set(key, { ...existing, sku: action.sku, name: action.name || existing?.name || action.sku, quantity: action.quantity })
    }
  }
  const lines = [...projected.values()].filter((line) => line.quantity > 0)
  const basket = shop.state?.basket
  const order = shop.state?.order
  const totalItems = lines.reduce((sum, line) => sum + line.quantity, 0)
  const navigate = (next) => { setTab(next); document.getElementById('shop-content')?.focus({ preventScroll: true }) }
  const basketProps = { lines, basket, order, queue: shop.queue, locked, action: shop.action, onSearch: () => navigate('search'), onCheckout: () => setCheckout({ target_id: shop.state.queue.target_id }) }
  return <div className="ocado-app">
    <a className="skip-link" href="#shop-content">Skip to shop</a>
    <header className="app-header"><div className="header-inner"><a className="brand" href="#" onClick={(event) => { event.preventDefault(); navigate('delivery') }} aria-label="Shared shop delivery"><span className="brand-mark"><IconLeaf size={24} stroke={1.6} /></span><span>ocado<span className="brand-subtitle">the shared shop</span></span></a><span className="shared-label"><span className="status-dot connected" />Shared account</span></div></header>
    <div className="app-layout"><aside className="delivery-aside"><DeliveryCard order={order} queue={shop.queue} ready={ready} loading={shop.loading} onSlots={() => navigate('slots')} /><div className="aside-note"><IconUserCircle size={21} /><p>Shopping together, without the crossed wires.<br /><strong>Everyone sees the same basket.</strong></p></div></aside>
    <div className="workspace">
      {!online && <div className="notice notice-error" role="alert"><strong>You’re offline.</strong> Reconnect before making changes. Nothing is automatically retried.</div>}
      {shop.error && <div className="notice notice-error" role="alert"><div><strong>{shop.accessExpired ? 'Sign in again to continue' : 'We couldn’t complete that request'}</strong><p>{shop.error}</p></div>{shop.accessExpired ? <a className="button button-outline button-small" href={window.location.pathname + window.location.search}>Sign in again</a> : <button type="button" className="button button-outline button-small" disabled={shop.busy || shop.syncing} onClick={() => { shop.clearError(); void shop.refresh() }}>Refresh shop</button>}</div>}
      <QueueBanner queue={shop.queue} now={now} busy={shop.busy || !online || shop.accessExpired} cancel={() => setCancelQueue({ target_id: shop.queue.target_id })} />
      <main id="shop-content" className="content-panel" tabIndex={-1} aria-busy={shop.loading || shop.syncing}>
        {shop.loading && !shop.auth ? <div className="loading-block" role="status"><span className="spinner" /><h2>Opening the shared shop</h2><p>Finding your next delivery and everyone’s latest changes.</p></div> : <>
          {ready && <div className="sync-line"><span className={`status-dot ${shop.stale ? '' : 'connected'}`} /><span>{shop.busy ? 'Saving your shared change…' : shop.syncing ? 'Refreshing the shared basket…' : shop.stale ? 'Basket needs a refresh before editing' : 'Shared with everyone · updates every 3 seconds'}</span><button type="button" className="text-button" onClick={shop.refresh} disabled={shop.busy || shop.syncing || !online}>Refresh</button></div>}
          {(!ready || tab === 'account') ? <Account auth={shop.auth} busy={shop.busy} authenticate={shop.authenticate} accessExpired={shop.accessExpired} /> : !shop.state ? <Empty icon={IconTruckDelivery} title="Your shop isn’t available yet">{shop.syncing ? 'Loading your booked delivery and basket…' : 'Refresh the shop to try again. No changes have been sent.'}</Empty> : <>
            {tab === 'delivery' && <><div className="section-heading"><div><span className="eyebrow">Welcome to your shared shop</span><h2>{order ? 'All set for your next delivery' : 'Let’s fill the basket'}</h2></div><span className="heading-icon"><IconBasket size={26} stroke={1.4} /></span></div><p className="section-copy">{order ? 'Top up the essentials, find a better time, or check what’s already coming.' : 'Search for groceries, choose a slot and explicitly confirm checkout when everyone is ready.'} Every change joins one shared queue.</p><div className="quick-actions"><button type="button" onClick={() => navigate('search')}><IconSearch size={23} /><span>Find groceries<small>Fresh ideas & everyday favourites</small></span><IconArrowRight size={20} /></button><button type="button" onClick={() => navigate('slots')}><IconCalendarWeek size={23} /><span>Delivery times<small>{order ? 'Find a better slot' : 'Choose your first slot'}</small></span><IconArrowRight size={20} /></button></div><div className="delivery-basket"><Basket {...basketProps} /></div></>}
            {tab === 'basket' && <Basket {...basketProps} />}
            {tab === 'search' && <Search lines={lines} locked={locked} action={shop.action} reportError={shop.reportError} />}
            {tab === 'slots' && <Slots queue={shop.queue} order={order} locked={locked} action={shop.action} reportError={shop.reportError} />}
          </>}
        </>}
      </main><p className="app-footnote">All times Europe/London · Prices in GBP · This is the shared household shop</p>
    </div></div>
    <nav className="bottom-nav" aria-label="Shared shop navigation">{tabs.map(({ id, label, icon: Icon }) => <button type="button" key={id} className={tab === id ? 'active' : ''} aria-current={tab === id ? 'page' : undefined} onClick={() => navigate(id)}><span className="nav-icon"><Icon size={23} stroke={tab === id ? 2 : 1.6} />{id === 'basket' && totalItems > 0 && <span className="nav-badge">{totalItems > 99 ? '99+' : totalItems}</span>}</span><span>{label}</span></button>)}</nav>
    {checkout && <Confirm title="Queue checkout for everyone?" onClose={() => setCheckout(false)}>
      {checkout.target_id !== shop.queue?.target_id && <p className="inline-error" role="alert">The target delivery has changed. Go back and review the current shared basket before confirming.</p>}
      <p>You are confirming checkout of the <strong>{order ? 'booked delivery' : 'active shared basket'}</strong> with {totalItems} items, using the shared account’s saved payment method.</p>
      <div className="confirmation-total"><span>Last known total</span><strong>{money(basket?.total)}</strong></div>
      <p>Pending edits may change this total. Checkout runs after other queued changes, when the shared 3-minute timer ends. Anyone’s next edit resets that timer. You can cancel before processing starts.</p>
      <p className="muted">If Ocado requires payment verification or cannot complete checkout, the queue will show an error. A failed or uncertain checkout is never automatically retried.</p>
      <button type="button" className="button button-primary" disabled={locked || checkout.target_id !== shop.queue?.target_id || !totalItems || shop.queue?.actions?.some((item) => item.kind === 'checkout')} onClick={async () => { const accepted = await shop.action({ kind: 'checkout', confirm: true, target_id: checkout.target_id }); if (accepted) setCheckout(false) }}>{shop.busy ? 'Queuing…' : 'Confirm & queue checkout'}</button>
    </Confirm>}
    {cancelQueue && <Confirm title={shop.queue?.status === 'failed' ? 'Clear failed changes?' : 'Cancel everyone’s queued changes?'} onClose={() => setCancelQueue(false)}>
      {cancelQueue.target_id !== shop.queue?.target_id && <p className="inline-error" role="alert">The target delivery has changed. Go back and review the new queue.</p>}
      <p>{shop.queue?.status === 'failed' ? 'This clears the failed queue; it does not undo anything Ocado may already have applied. Check the booked order and payment before intentionally trying again.' : 'All pending item changes, slot moves and checkout confirmations from every user will be removed. The booked order itself is not cancelled.'}</p>
      <button type="button" className="button button-danger" disabled={shop.busy || shop.accessExpired || shop.queue?.status === 'running' || cancelQueue.target_id !== shop.queue?.target_id || !online} onClick={async () => { const accepted = await shop.cancel(cancelQueue.target_id); if (accepted) setCancelQueue(false) }}>{shop.busy ? 'Clearing…' : 'Clear shared queue'}</button>
    </Confirm>}
  </div>
}
