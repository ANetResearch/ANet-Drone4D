// M15 use-mobile: useSyncExternalStore
import * as React from "react"

const MOBILE_BREAKPOINT = 768
const QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`

function subscribe(onChange: () => void) {
  const mql = window.matchMedia(QUERY)
  mql.addEventListener("change", onChange)
  return () => mql.removeEventListener("change", onChange)
}

const snapshot = () => window.innerWidth < MOBILE_BREAKPOINT
const serverSnapshot = () => false

export function useIsMobile() {
  return React.useSyncExternalStore(subscribe, snapshot, serverSnapshot)
}
