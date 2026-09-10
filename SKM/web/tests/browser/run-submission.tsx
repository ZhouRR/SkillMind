import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { SubmissionHarness } from './SubmissionHarness'
import '../../src/styles.css'

// Component と mount を分離し、FastRefresh の自己 import で root を重複生成しない。
const root = document.getElementById('root')
if (!root) throw new Error('Missing browser fixture root')
createRoot(root).render(<StrictMode><SubmissionHarness /></StrictMode>)
