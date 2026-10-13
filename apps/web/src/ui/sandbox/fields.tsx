// Form fields of the D2 management panels (AWR-14 §20.6.3, §20.7.3, §20.8; UX-FR-116, UX-FR-121): a number field with
// its unit in an InputGroup addon and the front-end range check (aria-invalid plus the reason under the field), committed
// on blur or Enter like the D1 environment sliders (U-03: the field shows a "pending" outline until the server answered;
// a refusal rolls back and the caller toasts the reason code), Escape cancels the edit; and a text field with the same
// commit rule (labels: 1-32 characters, sanitised before display and on the server). While not being edited a field
// shows the committed value (the draft is null), so a server update reaches it without an effect.
import * as React from 'react'
import { useT } from '@/app/i18n'
import { cn } from '@/lib/utils'
import { Field, FieldError, FieldLabel } from '@/ui/components/ui/field'
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from '@/ui/components/ui/input-group'
import type { Range } from '@/ui/panels/targets/targetModel'

export interface NumFieldProps {
  id: string
  label: string
  value: number | null | undefined
  unit?: string
  range: Range
  step?: number
  digits?: number
  readOnly?: boolean
  /** resolves true when accepted (the value stays), false when refused (rolled back) */
  onCommit?: (v: number) => Promise<boolean>
  /** the server's field error (code 522 violations, 540-544) */
  serverError?: string | null
  className?: string
}

export const showNum = (v: number | null | undefined, digits: number): string => (v === null || v === undefined || !Number.isFinite(v) ? '' : String(Number(v.toFixed(digits))))

/** a number draft outside its range (pure; tests) */
export function numBad(draft: string, range: Range): boolean {
  const n = Number(draft)
  return draft.trim() !== '' && (!Number.isFinite(n) || n < range.min || n > range.max)
}

export function NumField({ id, label, value, unit, range, step, digits = 2, readOnly = false, onCommit, serverError = null, className }: NumFieldProps) {
  const t = useT()
  // null while not editing: the field shows the committed value
  const [draft, setDraft] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState<string | null>(null)
  const cancelled = React.useRef(false)
  const text = draft ?? pending ?? showNum(value, digits)
  const bad = numBad(text, range)
  const commit = async () => {
    const d = draft
    setDraft(null)
    if (cancelled.current || d === null) {
      cancelled.current = false
      return
    }
    const num = Number(d)
    if (!onCommit || numBad(d, range) || d.trim() === '' || num === value) return
    setPending(d)
    await onCommit(num)
    setPending(null)
  }
  return (
    <Field className={className} data-invalid={bad || !!serverError ? '' : undefined} data-num-field={id}>
      {label ? <FieldLabel htmlFor={`nf-${id}`} className="text-hud-sub text-muted-foreground">{label}</FieldLabel> : null}
      <InputGroup className={cn(pending !== null && 'ring-1 ring-foreground/40')} data-pending={pending !== null ? '' : undefined}>
        <InputGroupInput id={`nf-${id}`} inputMode="decimal" value={text} readOnly={readOnly} aria-invalid={bad || !!serverError} aria-label={label || id}
          step={step} onFocus={() => setDraft(showNum(value, digits))} onChange={(e) => setDraft(e.target.value)} onBlur={() => void commit()}
          onKeyDown={(e) => {
            const el = e.target as HTMLInputElement
            if (e.key === 'Enter') el.blur()
            if (e.key === 'Escape') {
              cancelled.current = true
              el.blur()
            }
          }} />
        {unit ? <InputGroupAddon align="inline-end"><InputGroupText>{unit}</InputGroupText></InputGroupAddon> : null}
      </InputGroup>
      {bad ? <FieldError>{t('form.range', { min: range.min, max: range.max })}</FieldError> : serverError ? <FieldError>{serverError}</FieldError> : null}
    </Field>
  )
}

export interface TextFieldProps {
  id: string
  label: string
  value: string
  readOnly?: boolean
  validate?: (s: string) => boolean
  onCommit?: (v: string) => Promise<boolean>
}

export function TextField({ id, label, value, readOnly = false, validate, onCommit }: TextFieldProps) {
  const t = useT()
  const [draft, setDraft] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState<string | null>(null)
  const text = draft ?? pending ?? value
  const bad = validate ? !validate(text) : false
  const commit = async () => {
    const d = draft
    setDraft(null)
    if (d === null || !onCommit || (validate && !validate(d)) || d === value) return
    setPending(d.trim())
    await onCommit(d.trim())
    setPending(null)
  }
  return (
    <Field data-invalid={bad ? '' : undefined} data-text-field={id}>
      <FieldLabel htmlFor={`tf-${id}`} className="text-hud-sub text-muted-foreground">{label}</FieldLabel>
      <InputGroup className={cn(pending !== null && 'ring-1 ring-foreground/40')} data-pending={pending !== null ? '' : undefined}>
        <InputGroupInput id={`tf-${id}`} value={text} readOnly={readOnly} aria-invalid={bad} onFocus={() => setDraft(value)}
          onChange={(e) => setDraft(e.target.value)} onBlur={() => void commit()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          }} />
      </InputGroup>
      {bad ? <FieldError>{t('form.label')}</FieldError> : null}
    </Field>
  )
}
