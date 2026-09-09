import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from '../../src/App'
import { AccountsHarness } from './AccountsHarness'
import '../../src/styles.css'

// Fast Refresh の自己 import が root を二重生成しないよう、component 定義は別 module に置く。
const root = document.getElementById('root')
if (!root) throw new Error('Missing browser fixture root')
createRoot(root).render(<StrictMode>
  {new URLSearchParams(location.search).get('app') === '1' ? <App /> : <AccountsHarness />}
</StrictMode>)
