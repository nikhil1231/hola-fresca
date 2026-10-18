import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { installAccessSessionHandling } from '../api/session.js'
import App from './App.jsx'
import './ocado.css'

installAccessSessionHandling()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
