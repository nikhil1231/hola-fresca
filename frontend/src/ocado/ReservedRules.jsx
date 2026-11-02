import { useEffect, useState } from 'react'
import { request } from './api.js'

export function ReservedRules({ selection, reportError }) {
  const [settings, setSettings] = useState(null)
  const [query, setQuery] = useState('')
  const [products, setProducts] = useState([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [reload, setReload] = useState(0)
  const sameAccount = (data) => data.account_id === selection.account_id && data.account_version === selection.account_version
  useEffect(() => {
    const controller = new AbortController()
    request('/reserved', { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) {
        if (!sameAccount(data)) throw new Error('The Ocado account changed. Refresh the shop.')
        setSettings(data)
      }
    }).catch((error) => { if (!controller.signal.aborted) reportError(error) })
    return () => controller.abort()
  // The parent remounts this panel when account selection changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection.account_id, selection.account_version, reload, reportError])
  useEffect(() => {
    const controller = new AbortController()
    setProducts([])
    if (!query.trim()) return () => controller.abort()
    const timer = window.setTimeout(() => {
      request(`/search?q=${encodeURIComponent(query.trim())}`, { signal: controller.signal }).then((data) => {
        if (!controller.signal.aborted && sameAccount(data)) setProducts(data.products)
      }).catch((error) => { if (!controller.signal.aborted) reportError(error) })
    }, 350)
    return () => { window.clearTimeout(timer); controller.abort() }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, selection.account_id, selection.account_version, reportError])
  if (!settings) return <p role="status">Loading Reserved rules…</p>
  const update = (sku, quantity) => { setMessage(''); setSettings((old) => ({ ...old, rules: old.rules.map((rule) => rule.sku === sku ? { ...rule, quantity } : rule) })) }
  return <section className="reserved-panel">
    <div className="section-heading"><h2>Ocado Reserved</h2><button type="button" className="text-button" disabled={busy} onClick={() => setReload((old) => old + 1)}>Refresh</button></div>
    <p className="section-copy">Set the number of packs you want in each Reserved delivery. Zero removes a product. Other products stay as Ocado chose them.</p>
    <p>Rules run once per Reserved order, including upcoming Reserved orders that have not been processed yet. Your edits after that are left alone. Manual orders and baskets are excluded. Processing waits while you have a separate basket or unsaved shared changes.</p>
    <form className="account-form" onSubmit={async (event) => {
      event.preventDefault(); setBusy(true); setMessage('')
      try {
        const data = await request('/reserved', { body: { ...selection, enabled: settings.enabled, rules: settings.rules } })
        if (!sameAccount(data)) throw new Error('The Ocado account changed. Refresh the shop.')
        setSettings(data); setMessage('Rules saved. Enabled rules are checked automatically every minute.')
      } catch (error) { reportError(error) } finally { setBusy(false) }
    }}>
      <label className="reserved-enable"><input type="checkbox" checked={settings.enabled} disabled={busy} onChange={(event) => { setMessage(''); setSettings({ ...settings, enabled: event.target.checked }) }} />Apply my rules automatically</label>
      <ul className="reserved-rule-list">{settings.rules.map((rule) => <li key={rule.sku}>
        <label><span>{rule.name}</span><input type="number" aria-label={`Target packs for ${rule.name}`} min="0" max="50" step="1" required value={rule.quantity} disabled={busy} onChange={(event) => update(rule.sku, event.target.value === '' ? '' : Number(event.target.value))} /></label>
        <button type="button" className="text-button" disabled={busy} onClick={() => { setMessage(''); setSettings({ ...settings, rules: settings.rules.filter((item) => item.sku !== rule.sku) }) }}>Delete rule</button>
      </li>)}</ul>
      {!settings.rules.length && <p>No product rules yet. Find products below.</p>}
      <button type="submit" className="button button-primary" disabled={busy || (settings.enabled && !settings.rules.length)}>{busy ? 'Saving…' : 'Save Reserved rules'}</button>
      {message && <p role="status">{message}</p>}
    </form>
    <label className="reserved-search">Find a product for your rules<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search Ocado products" /></label>
    <ul className="reserved-rule-list">{products.map((product) => <li key={product.sku}><span>{product.name} {product.pack}</span><button type="button" className="button button-outline button-small" disabled={busy || settings.rules.some((rule) => rule.sku === product.sku)} onClick={() => {
      setSettings({ ...settings, rules: [...settings.rules, { sku: product.sku, name: product.name || product.sku, quantity: 1 }] }); setMessage('New rule added. Save your rules to apply it.')
    }}>{settings.rules.some((rule) => rule.sku === product.sku) ? 'Added' : 'Add rule'}</button></li>)}</ul>
    <h3>Recent Reserved deliveries</h3>
    {!settings.runs.length && <p>No Reserved deliveries processed yet.</p>}
    <ul className="reserved-history">{settings.runs.map((run) => <li key={run.order_id}><strong>Order {run.order_id}: {run.status === 'completed' ? 'Rules applied' : run.status === 'running' ? 'Applying rules' : 'Needs attention'}</strong>{run.warning && <p role="alert">{run.warning}</p>}</li>)}</ul>
    <p>If changes would take the item total below £40, they are discarded and a warning appears here. Review that delivery manually; it will not be retried automatically.</p>
  </section>
}
