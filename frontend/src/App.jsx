import { useEffect, useState } from 'react'
import './App.css'

const initialHealth = {
  loading: true,
  status: 'checking',
  service: 'HolaFresca',
}

function App() {
  const [health, setHealth] = useState(initialHealth)

  useEffect(() => {
    let cancelled = false

    async function loadHealth() {
      try {
        const response = await fetch('/api/health')
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const data = await response.json()
        if (!cancelled) setHealth({ loading: false, ...data })
      } catch (error) {
        if (!cancelled) {
          setHealth({
            loading: false,
            status: 'offline',
            service: error instanceof Error ? error.message : 'Unknown error',
          })
        }
      }
    }

    loadHealth()

    return () => {
      cancelled = true
    }
  }, [])

  const online = health.status === 'ok'

  return (
    <main className="shell">
      <section className="workspace" aria-label="HolaFresca status">
        <p className="eyebrow">Fresh stack check</p>
        <h1>HolaFresca</h1>
        <p className="summary">
          FastAPI is wired through Vite, ready for tickets to start shaping the product.
        </p>

        <div className="status-row">
          <span className={online ? 'status-dot online' : 'status-dot'} aria-hidden="true" />
          <div>
            <p className="status-label">{health.loading ? 'Checking backend' : 'Backend status'}</p>
            <p className="status-value">
              {health.status} · {health.service}
            </p>
          </div>
        </div>
      </section>
    </main>
  )
}

export default App
