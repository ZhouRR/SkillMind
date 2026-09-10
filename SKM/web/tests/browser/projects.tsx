import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from '../../src/App'
import '../../src/styles.css'

// Route・認証・Project の state を test shell で代用せず、本番入口全体を mount する。
const root = document.getElementById('root')
if (!root) throw new Error('Missing browser fixture root')
createRoot(root).render(<StrictMode><App /></StrictMode>)
