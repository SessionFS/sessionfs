import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { getItem as lsGet } from './utils/storage'

// Initialize theme from localStorage or system preference before first render
const stored = lsGet('sfs-theme');
const prefersDark = typeof window.matchMedia === 'function'
  && window.matchMedia('(prefers-color-scheme: dark)').matches;
const theme = stored === 'light' || stored === 'dark'
  ? stored
  : (prefersDark ? 'dark' : 'light');
document.documentElement.setAttribute('data-theme', theme);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
