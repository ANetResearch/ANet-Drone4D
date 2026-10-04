// Entry of the public demo build (ADR-083; vite.config.ts swaps it in for src/main.tsx when VITE_AWR_DEMO=public): "/"
// renders the landing page (index.html hides the boot mask there before any script runs); every other path loads the
// full application (src/main.tsx, a separate chunk with the engine, three and the realtime client), so visitors who only
// read the landing page never download the 3D runtime.
import '@/styles/index.css'
import { renderLanding } from './Landing'

if (location.pathname === '/') renderLanding(document.getElementById('root')!)
else void import('../../main')
