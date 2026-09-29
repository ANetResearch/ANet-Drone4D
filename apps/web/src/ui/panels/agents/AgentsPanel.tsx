// AGENTS tab (M15-FR-022, D1-ext; AWR-14 §5.8; M14 §8.3): runtime state, the agent list (AID short, vehicle, capabilities,
// trust level) and the collaboration task table (task, capability, A2A state, AWR phase, verification, provider) from
// stores/agents (M14). Only attention states (failed, rejected, input-required) use a red outline. Registered only when
// the D1-ext agent runtime ships (registerExtPanels; ?ext=1 in dev and test builds).
import { useT } from '@/app/i18n'
import { fmt } from '@/lib/format'
import { sanitizeText } from '@/lib/sanitize'
import { Badge } from '@/ui/components/ui/badge'
import { LfTable, type LfColumn } from '@/ui/lf/LfTable'
import { PanelEmpty } from '@/ui/brand'
import { StatusBadge } from '@/ui/notify/StatusBadge'
import { selection } from '@/stores/selection'
import { useAgentsState, type AgentRow, type TaskRow } from '@/stores/agents'
import { setAgentsPanelOpen } from '@/ui/shell/RtContext'
import * as React from 'react'

const ATTENTION = new Set(['failed', 'rejected', 'input-required'])

export function AgentsPanel() {
  const t = useT()
  // subscribe agent/tasks and agent/+/status only while the panel is mounted (M14-to-M15, stores/agents bindAgents)
  React.useEffect(() => {
    setAgentsPanelOpen(true)
    return () => setAgentsPanelOpen(false)
  }, [])
  const state = useAgentsState((s) => s.runtimeState)
  const agentsMap = useAgentsState((s) => s.agents)
  const tasks = useAgentsState((s) => s.tasks)
  const agents = [...agentsMap.values()]
  const agentCols: LfColumn<AgentRow>[] = [
    { key: 'aidShort', label: 'AID', format: (a) => <span className="font-mono">{sanitizeText(a.aidShort, 16)}</span> },
    { key: 'vehicleId', label: t('agents.col.vehicle'), format: (a) => <span className="font-mono">{a.vehicleId}</span> },
    { key: 'caps', label: t('agents.col.caps'), format: (a) => a.caps.slice(0, 3).map((c) => sanitizeText(c, 24)).join(t('common.listSep')) },
    { key: 'trust', label: t('agents.col.trust'), align: 'right', format: (a) => `V${a.trust.verifyMax}` },
    { key: 'socPct', label: t('agents.col.soc'), unit: '%', align: 'right', format: (a) => fmt.num(a.socPct) },
  ]
  const taskCols: LfColumn<TaskRow>[] = [
    { key: 'taskId', label: t('agents.col.task'), format: (x) => <span className="font-mono">{sanitizeText(x.taskId, 24)}</span> },
    { key: 'capability', label: t('agents.col.capability'), format: (x) => sanitizeText(x.capability, 32) },
    { key: 'state', label: t('agents.col.state'), format: (x) => (ATTENTION.has(x.state) ? <StatusBadge kind="warning" text={t(`agents.a2a.${x.state}`)} /> : t(`agents.a2a.${x.state}`)) },
    { key: 'phase', label: t('agents.col.phase'), format: (x) => (x.phase ? t(`agents.phase.${x.phase}`) : '—') },
    { key: 'verified', label: t('agents.col.verified'), format: (x) => (x.verified ? t('agents.verified') : t('agents.unverified')) },
    { key: 'providerVehicle', label: t('agents.col.provider'), format: (x) => <span className="font-mono">{x.providerVehicle ?? '—'}</span> },
  ]
  return (
    <div className="flex flex-col gap-2" data-agents-panel="">
      <div className="flex items-center gap-2">
        <Badge variant="outline">{t(`agents.state.${state}`)}</Badge>
        <span className="text-hud-cap uppercase text-muted-foreground">{t('agents.count', { agents: agents.length, tasks: tasks.length })}</span>
      </div>
      {agents.length === 0 ? <PanelEmpty title={t('agents.empty')} /> : (
        <div className="grid grid-cols-2 gap-2">
          <LfTable columns={agentCols} rows={agents} rowKey={(a) => a.aid} ariaLabel={t('agents.list')} height={160} onRowClick={(a) => selection.select([a.vehicleId])} />
          <LfTable columns={taskCols} rows={tasks} rowKey={(x) => x.taskId} ariaLabel={t('agents.tasks')} height={160}
            onRowClick={(x) => (x.providerVehicle ? selection.select([x.providerVehicle]) : undefined)} />
        </div>
      )}
    </div>
  )
}
