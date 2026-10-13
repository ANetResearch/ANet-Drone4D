/* ANet Drone4D 文档站：语言与版本浮层的键盘操作与 aria 状态（点击切换由 sphinx_rtd_theme 的 theme.js 处理）。 */
(function () {
  'use strict'
  function sync(root, head) {
    head.setAttribute('aria-expanded', root.classList.contains('shift-up') ? 'true' : 'false')
  }
  document.addEventListener('DOMContentLoaded', function () {
    var root = document.querySelector('.drone4d-flyout')
    if (!root) return
    var head = root.querySelector('.rst-current-version')
    if (!head) return
    head.addEventListener('click', function () { window.setTimeout(function () { sync(root, head) }, 0) })
    head.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        head.click()
      }
    })
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && root.classList.contains('shift-up')) {
        root.classList.remove('shift-up')
        sync(root, head)
        head.focus()
      }
    })
  })
})()
