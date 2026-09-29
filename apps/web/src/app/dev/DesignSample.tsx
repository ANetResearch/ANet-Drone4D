// Design system sample page, /dev/design, dev and test builds only (M15 §3.1 design reviewers; M15-FR-084): the four
// design pillars with deterministic sample data (lieflat rnd): shadcn base-mira components under the motion layer,
// morphicons Icon and StateIcon (whitelisted morph and swap), lieflat charts (canvas and SVG) and the JS recipes.
// Used by perf/m15 smoke screenshots and by visual review; never registered in plain production builds.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { notify } from '@/app/providers/ToastProvider'
import { fmt } from '@/lib/format'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/ui/components/ui/accordion'
import { Badge } from '@/ui/components/ui/badge'
import { Button } from '@/ui/components/ui/button'
import { Checkbox } from '@/ui/components/ui/checkbox'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/ui/components/ui/dialog'
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/ui/components/ui/dropdown-menu'
import { Field, FieldLabel } from '@/ui/components/ui/field'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/ui/components/ui/sheet'
import { Switch } from '@/ui/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/ui/components/ui/tooltip'
import { Icon } from '@/ui/icons/Icon'
import { StateIcon } from '@/ui/icons/StateIcon'
import { BrandLockup } from '@/ui/brand'
import { LfBarRank } from '@/ui/lf/LfBarRank'
import { LfChartCard } from '@/ui/lf/LfChartCard'
import { LfLine } from '@/ui/lf/LfLine'
import { LfStat } from '@/ui/lf/LfStat'
import { LfTable } from '@/ui/lf/LfTable'
import { LfTickGauge } from '@/ui/lf/LfTickGauge'
import { LfRing } from '@/ui/lf/series'
import { rnd } from '@/ui/lf/rnd'
import { MotionNumber } from '@/ui/motion/MotionNumber'
import { SwapText } from '@/ui/motion/SwapText'
import { MatrixLoader } from '@/ui/motion/MatrixLoader'
import { setUserMotion, useMotionTier, type UserMotion } from '@/ui/motion/tier'
import { useElementWidth } from '@/ui/layout/useElementWidth'

const RING = new LfRing(600)
const ALT = new LfRing(600)
function feed(now: number) {
  const k = Math.floor(now / 100)
  RING.push(now, 30 + 8 * Math.sin(k / 9) + rnd(k, 3) * 6)
  ALT.push(now, 60 + 20 * Math.sin(k / 23) + rnd(k, 7) * 4)
}
const STATIC = Array.from({ length: 30 }, (_, d) => ({ t: d, v: 46 + 22 * Math.sin(d / 4.6) + 14 * Math.sin(d / 2.1) + rnd(d + 1, 5) * 12, hollow: d % 7 >= 5 }))
const RANK = [['SZ', 38], ['SH', 27], ['NY', 22], ['SF', 16], ['SU', 11], ['CHI', 7]].map(([label, value]) => ({ label: String(label), value: Number(value) * 1000 }))
const TABLE = Array.from({ length: 6 }, (_, i) => ({ key: `p600-0${i + 1}`, alt: 40 + rnd(i, 2) * 80, bat: Math.round(50 + rnd(i, 4) * 50) }))

function LiveSample() {
  const t = useT()
  const [ref, w] = useElementWidth<HTMLDivElement>(300)
  React.useEffect(() => {
    const id = setInterval(() => feed(performance.now()), 100)
    return () => clearInterval(id)
  }, [])
  return (
    <LfChartCard title={t('design.live')} sub="ALT · 60 S" src="LIVE LINE · CPU CANVAS · 10 HZ" figureId="design-live">
      <LfStat label="ALT" format={(v) => fmt.num(v, 1)} bind={() => (ALT.len() ? ALT.v(ALT.len() - 1) : Number.NaN)} unit="m" spark={RING} />
      <div ref={ref} className="mt-2">{w > 0 ? <LfLine mode="live" series={ALT} domain={[0, 120]} target={80} width={w} height={72} ariaLabel={t('design.live')} format={(v) => fmt.num(v, 1)} /> : null}</div>
    </LfChartCard>
  )
}

export function DesignSample() {
  const t = useT()
  const tier = useMotionTier()
  const [playing, setPlaying] = React.useState(false)
  const [visible, setVisible] = React.useState(true)
  const [count, setCount] = React.useState(1280)
  const [phase, setPhase] = React.useState(0)
  const phases = ['design.phase.takeoff', 'design.phase.cruise', 'design.phase.landing']
  return (
    <main data-view="design" className="app-layer-overlay-page overflow-auto bg-background p-4" style={{ top: 0 }}>
      <div className="mb-4 flex items-center gap-3">
        <BrandLockup />
        <Badge variant="outline">{t('design.title')}</Badge>
        <ToggleGroup spacing={0} size="sm" variant="outline" value={[tier === 'full' ? 'system' : tier === 'lite' ? 'lite' : 'reduced']}
          onValueChange={(v: unknown[]) => v[0] && setUserMotion(v[0] as UserMotion)} aria-label={t('settings.motion')} className="ml-auto">
          <ToggleGroupItem value="system">full</ToggleGroupItem>
          <ToggleGroupItem value="lite">lite</ToggleGroupItem>
          <ToggleGroupItem value="reduced">reduced</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <section className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2" data-testid="overlays">
            <Dialog>
              <DialogTrigger render={<Button data-testid="open-dialog" />}>{t('design.dialog')}</DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t('design.dialog')}</DialogTitle>
                  <DialogDescription>{t('design.dialogBody')}</DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <DialogClose render={<Button variant="outline" />}>{t('common.cancel')}</DialogClose>
                  <DialogClose render={<Button />}>{t('common.confirm')}</DialogClose>
                </DialogFooter>
              </DialogContent>
            </Dialog>
            <Sheet>
              <SheetTrigger render={<Button variant="outline" data-testid="open-sheet" />}>{t('design.sheet')}</SheetTrigger>
              <SheetContent side="right">
                <SheetHeader>
                  <SheetTitle>P600-01</SheetTitle>
                  <SheetDescription>{t('design.sheetBody')}</SheetDescription>
                </SheetHeader>
              </SheetContent>
            </Sheet>
            <DropdownMenu>
              <DropdownMenuTrigger render={<Button variant="outline" data-testid="open-menu" />}>
                <Icon icon="nav.layers" data-icon="inline-start" />
                {t('panel.layers.title')}
              </DropdownMenuTrigger>
              <DropdownMenuContent className="w-44">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>{t('layers.pointcloud')}</DropdownMenuLabel>
                  <DropdownMenuCheckboxItem defaultChecked>EDL</DropdownMenuCheckboxItem>
                  <DropdownMenuItem><Icon icon="env.wind" />{t('env.windSpeed')}</DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <Tooltip>
              <TooltipTrigger render={<Button variant="ghost" size="icon" aria-label={t('camera.focus')} data-testid="tt-trigger" />}>
                <Icon icon="cmd.track" />
              </TooltipTrigger>
              <TooltipContent>{t('camera.focus')}</TooltipContent>
            </Tooltip>
            <Button variant="outline" data-testid="toast-info" onClick={() => notify('design:info', 'info', t('design.toast'), t('design.toastBody'))}>{t('design.toastButton')}</Button>
          </div>
          <Tabs defaultValue="a">
            <TabsList>
              <TabsTrigger value="a">{t('camera.orbit')}</TabsTrigger>
              <TabsTrigger value="b">{t('camera.fpv')}</TabsTrigger>
              <TabsTrigger value="c">{t('camera.bird')}</TabsTrigger>
            </TabsList>
            <TabsPanels className="rounded-lg p-3 ring-1 ring-foreground/10">
              <TabsContent value="a">{t('design.tabA')}</TabsContent>
              <TabsContent value="b">{t('design.tabB')}</TabsContent>
              <TabsContent value="c">{t('design.tabC')}</TabsContent>
            </TabsPanels>
          </Tabs>
          <Accordion defaultValue={['layers']}>
            <AccordionItem value="layers">
              <AccordionTrigger>{t('panel.layers.title')}</AccordionTrigger>
              <AccordionContent>
                <Field orientation="horizontal" className="justify-between"><FieldLabel htmlFor="d-sw">{t('layers.pointcloud')}</FieldLabel><Switch id="d-sw" defaultChecked /></Field>
                <Field orientation="horizontal"><Checkbox id="d-cb" defaultChecked /><FieldLabel htmlFor="d-cb">{t('layers.zones')}</FieldLabel></Field>
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="env">
              <AccordionTrigger>{t('panel.env.title')}</AccordionTrigger>
              <AccordionContent>{fmt.mor(850)} {'·'} {fmt.mor(12000)}</AccordionContent>
            </AccordionItem>
          </Accordion>
          <div className="flex items-center gap-2" data-testid="stateicons">
            <Button size="icon" variant="outline" aria-label={playing ? t('timeline.pause') : t('timeline.play')} data-testid="si-play" onClick={() => setPlaying((p) => !p)}>
              <StateIcon icon={playing ? 'tl.pause' : 'tl.play'} />
            </Button>
            <Button size="icon" variant="outline" aria-label={t('layers.visible')} data-testid="si-eye" onClick={() => setVisible((p) => !p)}>
              <StateIcon icon="layer.visible" alt={!visible} />
            </Button>
            <Button size="icon" variant="outline" aria-label={t('design.count')} onClick={() => setCount((c) => c + 37)}>
              <Icon icon="plus" />
            </Button>
            <MotionNumber value={count} format={fmt.count} className="text-hud-kpi font-extrabold" aria-label={t('design.count')} />
            <Button size="sm" variant="ghost" onClick={() => setPhase((p) => (p + 1) % phases.length)}>
              <SwapText value={t(phases[phase])} />
            </Button>
            <MatrixLoader label={t('design.loading')} />
          </div>
        </section>
        <section className="flex flex-col gap-3">
          <LiveSample />
          <LfChartCard title={t('design.static')} sub="30 DAYS" src="F2 HAIRLINE LINE · SVG" figureId="design-static">
            <LfLine mode="static" data={STATIC} ariaLabel={t('design.static')} format={(v) => fmt.num(v)} height={140} />
          </LfChartCard>
          <LfChartCard title={t('design.gauge')} src="F11 TICK GAUGE · SVG" figureId="design-gauge">
            <LfTickGauge value={73} ariaLabel={t('design.gauge')} remainder="27 TICKS TO GO" />
          </LfChartCard>
        </section>
        <section className="flex flex-col gap-3">
          <LfChartCard title={t('design.rank')} src="F1 RUNG BARS · SVG · 1 RUNG = 1K" figureId="design-rank">
            <LfBarRank variant="rung" data={RANK} hero="max" format={fmt.pts} ariaLabel={t('design.rank')} />
          </LfChartCard>
          <LfChartCard title={t('design.ticks')} src="F5 TICK ROWS · SVG" figureId="design-ticks">
            <LfBarRank variant="ticks" data={RANK} format={fmt.pts} height={200} ariaLabel={t('design.ticks')} />
          </LfChartCard>
          <LfChartCard title={t('design.table')} src="TABLE.LOG · SHADCN TABLE" figureId="design-table">
            <LfTable columns={[{ key: 'key', label: 'ID' }, { key: 'alt', label: 'ALT', unit: 'm', align: 'right', format: (r) => fmt.num(r.alt, 1) }, { key: 'bat', label: 'BAT', unit: '%', align: 'right' }]}
              rows={TABLE} rowKey={(r) => r.key} hot={{ row: 'p600-03', col: 'bat' }} selected="p600-02" ariaLabel={t('design.table')} />
          </LfChartCard>
        </section>
      </div>
    </main>
  )
}
