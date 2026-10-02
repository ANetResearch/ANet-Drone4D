// Settings content (M15-FR-026; AWR-14 §5.6): six tabs (general, render, motion, shortcuts, account, about); changes apply
// immediately and persist (no save button); render items that need a renderer rebuild carry the "applies after reload"
// badge; the motion tab shows the effective tier and its source (os, user, governor or test).
import { useT } from '@/app/i18n'
import { Badge } from '@/ui/components/ui/badge'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/ui/components/ui/field'
import { Button } from '@/ui/components/ui/button'
import { ScrollArea } from '@/ui/components/ui/scroll-area'
import { RadioGroup, RadioGroupItem } from '@/ui/components/ui/radio-group'
import { Switch } from '@/ui/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsPanels, TabsTrigger } from '@/ui/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/ui/components/ui/toggle-group'
import { AboutContent } from '@/ui/views/AboutDialog'
import { ShortcutTable } from '@/ui/views/ShortcutHelp'
import { useRt } from '@/ui/shell/RtContext'
import { roleKeyOf, useConnView } from '@/ui/shell/connView'
import { releaseControl, requestControl } from '@/ui/shell/control'
import { layers, useLayers } from '@/stores/layers'
import { usePerf } from '@/stores/perf'
import { setUserMotion, useMotionSource, useMotionTier, type UserMotion } from '@/ui/motion/tier'
import type { SettingsTab } from '@/app/router/search'
import { prefs, usePrefs } from '@/stores/prefs'

export const SETTINGS_TABS: readonly SettingsTab[] = ['general', 'render', 'motion', 'shortcuts', 'account', 'about']

export function SettingsPanel({ tab, onTab }: { tab: SettingsTab; onTab: (t: SettingsTab) => void }) {
  const t = useT()
  const ui = usePrefs((s) => s.ui)
  const tier = useMotionTier()
  const source = useMotionSource()
  const tier3 = usePerf((s) => s.tier)
  const deviceClass = usePerf((s) => s.deviceClass)
  const edl = useLayers((s) => s.edl)
  return (
    <Tabs value={tab} onValueChange={(v) => onTab(v as SettingsTab)}>
      <TabsList>
        {SETTINGS_TABS.map((k) => <TabsTrigger key={k} value={k}>{t(`settings.tab.${k}`)}</TabsTrigger>)}
      </TabsList>
      <TabsPanels className="min-h-64">
        <TabsContent value="general">
          <FieldGroup>
            <Field>
              <FieldLabel>{t('settings.altRef')}</FieldLabel>
              <ToggleGroup spacing={0} size="sm" variant="outline" value={[ui.altRef]} onValueChange={(v: unknown[]) => v[0] && prefs.setUi('altRef', v[0] as 'AGL' | 'MSL')}>
                <ToggleGroupItem value="AGL">AGL</ToggleGroupItem>
                <ToggleGroupItem value="MSL">MSL</ToggleGroupItem>
              </ToggleGroup>
            </Field>
            <Field>
              <FieldLabel>{t('settings.coord')}</FieldLabel>
              <ToggleGroup spacing={0} size="sm" variant="outline" value={[ui.coord]} onValueChange={(v: unknown[]) => v[0] && prefs.setUi('coord', v[0] as 'enu' | 'lla')}>
                <ToggleGroupItem value="enu">ENU</ToggleGroupItem>
                <ToggleGroupItem value="lla">{t('settings.coord.lla')}</ToggleGroupItem>
              </ToggleGroup>
            </Field>
            <Field orientation="horizontal" className="justify-between">
              <FieldLabel htmlFor="set-labels">{t('settings.labels')}</FieldLabel>
              <Switch id="set-labels" checked={ui.labels} onCheckedChange={(on: boolean) => prefs.setUi('labels', on)} />
            </Field>
          </FieldGroup>
        </TabsContent>
        <TabsContent value="render">
          <FieldGroup>
            <Field>
              <FieldLabel className="gap-2">{t('settings.backend')}<Badge variant="outline">{t('settings.reloadRequired')}</Badge></FieldLabel>
              <FieldDescription>{t('settings.backendHint')}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>{t('settings.tier')}</FieldLabel>
              <FieldDescription data-settings-tier={tier3 ?? ''}>{tier3 ? t('settings.tierValue', { tier: tier3, device: deviceClass ?? '—' }) : t('hud.tierUnknown')}</FieldDescription>
            </Field>
            <Field orientation="horizontal" className="justify-between">
              <FieldLabel htmlFor="set-edl">{tier3 === 'S' ? t('layers.edlSoft') : t('layers.edl')}</FieldLabel>
              <Switch id="set-edl" checked={edl && tier3 !== 'S'} disabled={tier3 === 'S'} onCheckedChange={(on: boolean) => layers.setEdl(on)} />
            </Field>
          </FieldGroup>
        </TabsContent>
        <TabsContent value="motion">
          <FieldGroup>
            <Field>
              <FieldLabel>{t('settings.motion')}</FieldLabel>
              <RadioGroup value={ui.motion} onValueChange={(v) => {
                prefs.setUi('motion', v as UserMotion)
                setUserMotion(v as UserMotion)
              }}>
                {(['system', 'lite', 'reduced'] as const).map((m) => (
                  <Field key={m} orientation="horizontal">
                    <RadioGroupItem value={m} id={`motion-${m}`} />
                    <FieldLabel htmlFor={`motion-${m}`} className="font-normal">{t(`settings.motion.${m}`)}</FieldLabel>
                  </Field>
                ))}
              </RadioGroup>
              <FieldDescription data-motion-effective={tier}>{t('settings.motion.effective', { tier: t(`motion.tier.${tier}`), source: t(`motion.source.${source}`) })}</FieldDescription>
            </Field>
          </FieldGroup>
        </TabsContent>
        <TabsContent value="shortcuts">
          <ScrollArea className="max-h-96"><ShortcutTable /></ScrollArea>
        </TabsContent>
        <TabsContent value="account">
          <AccountTab />
        </TabsContent>
        <TabsContent value="about">
          <ScrollArea className="max-h-96 pr-2"><AboutContent /></ScrollArea>
        </TabsContent>
      </TabsPanels>
    </Tabs>
  )
}

function AccountTab() {
  const t = useT()
  const rt = useRt()
  const role = useConnView((s) => s.role)
  const seat = useConnView((s) => s.seat)
  const key = roleKeyOf({ role, seat })
  const writer = key === 'role.operator' || key === 'role.admin'
  return (
    <FieldGroup>
      <Field>
        <FieldLabel>{t('settings.role')}</FieldLabel>
        <FieldDescription data-account-role={role ?? ''}>{t(key)} {'·'} {t(`seat.${seat ?? 'none'}`)}</FieldDescription>
      </Field>
      <Field orientation="horizontal">
        {writer ? <Button size="sm" variant="outline" onClick={() => releaseControl()}>{t('control.release')}</Button>
          : <Button size="sm" variant="outline" onClick={() => void requestControl(rt)}>{t('control.request')}</Button>}
      </Field>
    </FieldGroup>
  )
}
