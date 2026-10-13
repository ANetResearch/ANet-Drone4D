// Sandbox session bar (AWR-04 §10.2; M15-FR-206; UX-FR-109, UX-FR-110; AWR-14 §20.4.4 to §20.4.6): a 32 px strip under
// the header, built like the replay banner (one entry of the banner stack, so the unobscured rect moves down by one banner).
// Left to right: the session badge ("my sandbox · world · template"; "starting the sandbox" with a spinner while
// SPAWNING, "ending" while DRAINING) with a HoverCard (sid with copy, created, lifetime cap, usage), the remaining
// lifetime (C-class text at 1 Hz; red text only in the last 60 s) with "renew" (disabled with the reason when the session
// cannot renew), the rate badge (granted, "reaches xk" with the reason when capped, greyed during manual control),
// entity counts (vehicles, targets, nests, active tasks against their limits; a limit reached is outlined), the simulated
// calendar and light band, and "load scenario", "switch world", "reset", "end session" (the last two confirm first). The
// compact breakpoint keeps the badge, lifetime and rate and folds the rest into a "more" menu.
// Expiry (`sandbox.expiring`): a second banner line counts down 60 s with "export snapshot" and "renew" (when allowed).
import { PausedNote } from '@/ui/power/PowerOverlay'
import { usePower } from '@/app/power/store'
import * as React from 'react'
import { useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Alert, AlertAction, AlertDescription } from '@/ui/components/ui/alert'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/ui/components/ui/hover-card'
import { Spinner } from '@/ui/components/ui/spinner'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { confirmThen } from '@/ui/actions/ConfirmHost'
import { limitedKey } from '@/ui/notify/d2Notices'
import { manualStore } from '@/ui/manual/controller'
import { useStore } from 'zustand'
import { remainingS, sandbox, sandboxStore, useSandbox } from './session'
import { sandboxDialogs } from './dialogs'
import { CalendarPopover } from './CalendarPopover'
import { useSecond } from './useFetched'
import { lifetimeText } from './format'


function Lifetime() {
  const t = useT()
  const now = useSecond()
  const left = remainingS(sandboxStore.getState(), now)
  const renewable = useSandbox((s) => s.session?.renewable ?? false)
  const block = useSandbox((s) => s.session?.renew_block_reason ?? null)
  const [busy, setBusy] = React.useState(false)
  const renew = async () => {
    setBusy(true)
    const ok = await sandbox.keepalive()
    setBusy(false)
    if (!ok) notify('sandbox:renew', 'info', t('sandbox.renew.refused'))
  }
  const reason = block === 'queue_nonempty' ? 'sandbox.renew.blocked.queue' : block === 'max_lifetime' ? 'sandbox.renew.blocked.max' : 'sandbox.renew.blocked.degraded'
  return (
    <span className="flex items-center gap-1" data-session-lifetime="">
      <span className="text-muted-foreground">{t('sandbox.bar.left')}</span>
      <span data-island="" className={cn('font-mono tabular-nums', left <= 60 && 'text-brand-text')} data-session-left={Math.floor(left)}>{lifetimeText(left)}</span>
      <Tooltip>
        <TooltipTrigger render={<Button size="xs" variant="ghost" disabled={!renewable || busy} focusableWhenDisabled
          className="aria-disabled:opacity-50" onClick={() => void renew()} data-session-renew="" />}>
          {t('sandbox.bar.renew')}
        </TooltipTrigger>
        <TooltipContent>{renewable ? t('sandbox.bar.renewHint') : t(reason)}</TooltipContent>
      </Tooltip>
    </span>
  )
}

function Rate() {
  const t = useT()
  const granted = useSandbox((s) => s.session?.clock.rate_granted ?? 1)
  const cap = useSandbox((s) => s.session?.clock.rate_cap ?? 10)
  const limited = useSandbox((s) => s.session?.clock.limited_by ?? null)
  const manual = useStore(manualStore, (s) => s.state === 'ACTIVE' || s.state === 'ACQUIRING')
  const capped = limited !== null || cap < 10
  const why = t(`sandbox.limitedBy.${manual ? 'manual_control' : limitedKey(limited)}`)
  return (
    <Tooltip>
      <TooltipTrigger render={<Badge variant="outline" tabIndex={0} className={cn('gap-1 font-mono', manual && 'opacity-50')} data-session-rate={granted} />}>
        {`×${fmt.num(granted, granted < 1 ? 2 : 0)}`}
        {capped || manual ? <span className="font-sans text-muted-foreground">{manual ? t('sandbox.bar.manualLock') : t('sandbox.bar.reach', { k: fmt.num(cap, 1) })}</span> : null}
      </TooltipTrigger>
      <TooltipContent>{capped || manual ? t('sandbox.rate.capped', { k: fmt.num(manual ? 1 : cap, 1), why }) : t('sandbox.bar.rateFree')}</TooltipContent>
    </Tooltip>
  )
}

function Counts() {
  const t = useT()
  const usage = useSandbox((s) => s.session?.usage)
  const limits = useSandbox((s) => s.session?.limits)
  const items: { key: string; n: number; max: number }[] = [
    { key: 'vehicles', n: usage?.vehicles ?? 0, max: limits?.vehicles_max ?? 12 },
    { key: 'targets', n: usage?.targets ?? 0, max: limits?.targets_max ?? 30 },
    { key: 'nests', n: usage?.nests ?? 0, max: limits?.nests_max ?? 4 },
    { key: 'tasks', n: usage?.tasks_active ?? 0, max: limits?.tasks_active_max ?? 6 },
  ]
  return (
    <span className="flex items-center gap-2" data-session-counts="">
      {items.map((x) => (
        <span key={x.key} className={cn('rounded-sm px-1 font-mono tabular-nums', x.n >= x.max && 'ring-1 ring-foreground/40')} data-session-count={x.key}>
          <span className="font-sans text-muted-foreground">{t(`sandbox.bar.${x.key}`)}</span>
          {` ${x.n}/${x.max}`}
        </span>
      ))}
    </span>
  )
}

function SessionBadge() {
  const t = useT()
  const phase = useSandbox((s) => s.phase)
  const world = useSandbox((s) => s.world)
  const template = useSandbox((s) => s.template)
  const sid = useSandbox((s) => s.sid)
  const created = useSandbox((s) => s.session?.created_unix_ns ?? null)
  const maxNs = useSandbox((s) => s.session?.lifetime_max_unix_ns ?? null)
  const [copied, setCopied] = React.useState(false)
  const since = useSandbox((s) => s.phaseSinceMs)
  const now = useSecond()
  // "slow start" after 10 s in SPAWNING (AWR-14 §20.4.5)
  const slow = phase === 'spawning' && since > 0 && now - since > 10_000
  const copy = () => {
    if (!sid) return
    void navigator.clipboard?.writeText(sid).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }, () => null)
  }
  const label = phase === 'spawning' ? t('sandbox.spawning') : phase === 'draining' ? t('sandbox.draining') : t('role.sandbox')
  return (
    <HoverCard>
      <HoverCardTrigger render={<Badge variant="secondary" className="gap-1" data-session-badge={phase} />}>
        {phase === 'spawning' ? <Spinner className="size-3" /> : <Icon icon="sandbox.mine" />}
        <span>{label}</span>
        <span className="text-muted-foreground">{`· ${world ?? '—'}${template ? ` · ${t(`sandbox.template.${template}.short`)}` : ''}`}</span>
        {slow ? <span className="text-muted-foreground">{t('sandbox.spawningSlow')}</span> : null}
      </HoverCardTrigger>
      <HoverCardContent className="w-72 text-hud-sub">
        <dl className="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1">
          <dt className="text-muted-foreground">{t('sandbox.info.sid')}</dt>
          <dd className="flex items-center gap-1 font-mono">
            {sid ?? '—'}
            <Button size="icon-xs" variant="ghost" aria-label={t('sandbox.info.copy')} onClick={copy}><StateIcon icon="copy" alt={copied} /></Button>
          </dd>
          <dt className="text-muted-foreground">{t('sandbox.info.created')}</dt>
          <dd className="font-mono">{created ? fmt.wallTime(Number(BigInt(created) / 1_000_000n)) : '—'}</dd>
          <dt className="text-muted-foreground">{t('sandbox.info.maxLife')}</dt>
          <dd className="font-mono">{maxNs ? fmt.wallTime(Number(BigInt(maxNs) / 1_000_000n)) : '—'}</dd>
        </dl>
      </HoverCardContent>
    </HoverCard>
  )
}

function Actions({ compact }: { compact: boolean }) {
  const t = useT()
  const template = useSandbox((s) => s.template)
  const live = useSandbox((s) => s.phase === 'active' || s.phase === 'ready')
  const reset = () => confirmThen({
    id: 'sandbox.reset', title: t('sandbox.reset.title'), body: t('sandbox.reset.body'), action: t('sandbox.bar.reset'),
    run: () => void (template ? sandbox.loadTemplate(template) : Promise.resolve(null)),
  })
  const end = () => confirmThen({
    id: 'sandbox.end', title: t('sandbox.end.title'), body: t('sandbox.end.body'), action: t('sandbox.bar.end'),
    run: () => void sandbox.end(),
  })
  if (compact) {
    return (
      <DropdownMenu>
        <DropdownMenuTrigger render={<Button size="xs" variant="ghost" aria-label={t('sandbox.bar.more')} data-session-more="" />}>
          <Icon icon="more" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuGroup>
            <DropdownMenuItem disabled={!live} onClick={() => sandboxDialogs.open('template')}>{t('sandbox.bar.template')}</DropdownMenuItem>
            <DropdownMenuItem disabled={!live} onClick={() => sandboxDialogs.open('world')}>{t('sandbox.bar.world')}</DropdownMenuItem>
            <DropdownMenuItem disabled={!live} onClick={reset}>{t('sandbox.bar.reset')}</DropdownMenuItem>
            <DropdownMenuItem onClick={end}>{t('sandbox.bar.end')}</DropdownMenuItem>
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    )
  }
  return (
    <span className="flex items-center gap-1">
      <Button size="xs" variant="ghost" disabled={!live} onClick={() => sandboxDialogs.open('template')} data-session-template="">{t('sandbox.bar.template')}</Button>
      <Button size="xs" variant="ghost" disabled={!live} onClick={() => sandboxDialogs.open('world')} data-session-world="">{t('sandbox.bar.world')}</Button>
      <Button size="xs" variant="ghost" disabled={!live} onClick={reset} data-session-reset="">{t('sandbox.bar.reset')}</Button>
      <Button size="xs" variant="outline" onClick={end} data-session-end="">{t('sandbox.bar.end')}</Button>
    </span>
  )
}

/**
 * the session strip (one banner); D2.1: "paused · last update" while power saving froze the texts. The actions sit in the
 * flow at the right (not over the readouts: the shadcn AlertAction is absolutely positioned and the calendar ran under
 * "switch world" at 1600 px, d2-INT-to-M15-M17 item 4); as the bar narrows the calendar goes first, then the entity counts
 * (container queries on the bar itself, so the rails and the dock do not matter), and the compact breakpoint folds the
 * actions into "more".
 */
export function SessionBar({ compact = false }: { compact?: boolean }) {
  const frozen = usePower((s) => s.frozen)
  return (
    <Alert data-session-bar="" className="@container/sessionbar flex h-8 items-center gap-3 py-1 has-data-[slot=alert-action]:pr-2">
      <AlertDescription className="flex min-w-0 flex-1 items-center gap-3 overflow-hidden whitespace-nowrap">
        <SessionBadge />
        {frozen ? <PausedNote /> : <Lifetime />}
        <Rate />
        {compact ? null : <span className="flex items-center @max-[62rem]/sessionbar:hidden" data-session-counts-slot=""><Counts /></span>}
        {compact ? null : <span className="flex items-center @max-[72rem]/sessionbar:hidden" data-session-calendar-slot=""><CalendarPopover /></span>}
      </AlertDescription>
      <AlertAction className="static shrink-0"><Actions compact={compact} /></AlertAction>
    </Alert>
  )
}

/** the expiry line (second banner) while `sandbox.expiring` counts down */
export function ExpiringBanner() {
  const t = useT()
  const expiring = useSandbox((s) => s.expiring)
  const renewable = useSandbox((s) => s.session?.renewable ?? false)
  const now = useSecond()
  if (!expiring) return null
  const left = Math.max(0, Math.ceil((expiring.atMs - now) / 1000))
  return (
    <Alert data-session-expiring={left} className="flex h-8 items-center gap-2 py-1">
      <Icon icon="alert.warning" />
      <AlertDescription className="truncate">{t('sandbox.expiring', { s: left, reason: t(`sandbox.endReason.${expiring.reason}`) })}</AlertDescription>
      <AlertAction className="flex gap-1">
        <Button size="xs" variant="outline" onClick={() => void sandboxDialogs.downloadSnapshot()}>{t('sandbox.exportSnapshot')}</Button>
        {renewable ? <Button size="xs" onClick={() => void sandbox.keepalive()}>{t('sandbox.bar.renew')}</Button> : null}
      </AlertAction>
    </Alert>
  )
}
