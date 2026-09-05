import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'

// HTML shell と React の契約が壊れている場合は、空画面のまま継続せず即座に失敗させる。
const root = document.getElementById('root')
if (!root) {
  throw new Error('Missing #root element')
}

// StrictMode により、開発時に副作用 cleanup の不足を検出しやすくする。
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
