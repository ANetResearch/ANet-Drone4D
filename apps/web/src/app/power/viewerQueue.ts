// Viewer queue client (R-D2-31, R-D2-34; ADR-120; AWR-04 §15.6.3; AWR-17 §17.20.3 R81 to R83; M15-FR-226; UX-FR-151).
// Owner: M15 (interim: the net functions belong to M11's net layer, request d2-WP-12c-to-M11; this file keeps the same
// wire form so the swap is an import change). When the shared show's live viewer seats are full the handshake is refused
// with error 517 and close 4431; the page then takes a ticket (POST /api/viewer/queue, idempotent per principal),
// long-polls it (GET .../{ticket}?wait_s=25 with `Authorization: Bearer vq1.…`) and reconnects by itself on
// `seat_ready` (the seat is reserved 30 s for this principal). A ticket without a poll for 60 s is void (410 519): the
// page queues again. Leaving (power saving, navigation) deletes the ticket (R83). The store is powerStore.viewerTicket.
import { powerStore, type ViewerTicket } from './store'

export type { ViewerTicket } from './store'

const BASE = '/api/viewer/queue'
const WAIT_S = 25
let abort: AbortController | null = null
type Fetcher = (url: string, init: RequestInit) => Promise<Response>
let fetcher: Fetcher = (u, i) => fetch(u, i)

function setTicket(t: ViewerTicket | null): void {
  const s = powerStore.getState()
  powerStore.setState({ viewerTicket: t, version: s.version + 1 })
}

async function json<T>(r: Response): Promise<T | null> {
  try {
    return (await r.json()) as T
  } catch {
    return null
  }
}

const sleep = (ms: number, signal: AbortSignal): Promise<void> => new Promise((res) => {
  const id = setTimeout(res, ms)
  signal.addEventListener('abort', () => {
    clearTimeout(id)
    res()
  }, { once: true })
})

export const viewerQueue = {
  /** tests replace the transport */
  setFetcher(f: Fetcher | null): void {
    fetcher = f ?? ((u, i) => fetch(u, i))
  },

  /** take a ticket (or the same one again) and long-poll until seat_ready; `onReady` reconnects the link */
  async join(principal: string, onReady: () => void): Promise<void> {
    abort?.abort()
    const ac = new AbortController()
    abort = ac
    while (!ac.signal.aborted) {
      let ticket: ViewerTicket | null = null
      try {
        const r = await fetcher(BASE, { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' },
          body: JSON.stringify({ principal_hint: principal }), signal: ac.signal })
        const b = await json<{ ticket_id: string; ticket_token: string; position: number; eta_s: number | null }>(r)
        if ((r.status === 200 || r.status === 201) && b?.ticket_id) {
          ticket = { ticketId: b.ticket_id, ticketToken: b.ticket_token, position: b.position, etaS: b.eta_s, state: 'waiting' }
        }
      } catch {
        ticket = null
      }
      if (ac.signal.aborted) return
      if (!ticket) {
        // no queue endpoint (older server) or a refusal: wait and try the handshake again
        setTicket({ ticketId: '', ticketToken: '', position: 0, etaS: null, state: 'waiting' })
        await sleep(15_000, ac.signal)
        if (!ac.signal.aborted) onReady()
        return
      }
      setTicket(ticket)
      const ready = await viewerQueue.poll(ticket, ac.signal)
      if (ac.signal.aborted) return
      if (ready) {
        setTicket({ ...ticket, state: 'seat_ready', position: 0 })
        onReady()
        return
      }
      // the ticket was voided (519): queue again
    }
  },

  /** long-poll one ticket; true on seat_ready, false when it was voided */
  async poll(t: ViewerTicket, signal: AbortSignal): Promise<boolean> {
    while (!signal.aborted) {
      let r: Response
      try {
        r = await fetcher(`${BASE}/${encodeURIComponent(t.ticketId)}?wait_s=${WAIT_S}`, { headers: { authorization: `Bearer ${t.ticketToken}`, accept: 'application/json' }, signal })
      } catch {
        if (signal.aborted) return false
        await sleep(2000, signal)
        continue
      }
      if (r.status === 410) return false
      const b = await json<{ state: ViewerTicket['state']; position: number; eta_s: number | null }>(r)
      if (!b || r.status !== 200) {
        await sleep(2000, signal)
        continue
      }
      if (b.state === 'seat_ready') return true
      if (b.state === 'expired') return false
      setTicket({ ...t, position: b.position, etaS: b.eta_s, state: 'waiting' })
    }
    return false
  },

  /** leave the queue (power saving, navigation, the card's "leave" button) */
  async leave(): Promise<void> {
    abort?.abort()
    abort = null
    const t = powerStore.getState().viewerTicket
    setTicket(null)
    if (t?.ticketId) await fetcher(`${BASE}/${encodeURIComponent(t.ticketId)}`, { method: 'DELETE', headers: { authorization: `Bearer ${t.ticketToken}` } }).catch(() => null)
  },
}
