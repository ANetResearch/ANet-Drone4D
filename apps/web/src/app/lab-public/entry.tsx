// /lab/ 独立静态构建的入口（VITE_AWR_LAB_ONLY = 1；vite.config.ts labPublicPlugin 以 ./index.html 取代应用的 index.html）。Owner: M15.
// 只加载 React、shadcn 部件、morphicons 图标、lieflat 表格与本构建的小词典；不加载应用（src/main.tsx）、引擎、three、实时客户端与
// 内部工作区页面（构建期模块守卫，D3-AC-21）。启动遮罩是 index.html 的静态标记，首帧渲染后移除。
import '@/styles/index.css'
import { createRoot } from 'react-dom/client'
import { LabPublicApp } from './App'
import { initialLabLocale, setLabLocale } from './i18n'

setLabLocale(initialLabLocale())
const root = document.getElementById('root')
if (root) {
  createRoot(root).render(<LabPublicApp />)
  requestAnimationFrame(() => document.getElementById('boot-mask')?.remove())
}
