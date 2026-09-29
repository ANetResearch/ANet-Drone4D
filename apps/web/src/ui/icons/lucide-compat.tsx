// lucide-react compatibility layer for the shadcn sources only (M15-FR-067; g07 §4; AWR-15 §7.7). base-mira imports these
// 16 names; the icons codemod points `lucide-react` imports here. Canonical lucide names are imported (Loader2 is
// LoaderCircle, MoreHorizontal is Ellipsis); CheckIcon carries pathLength=1 and data-draw for the checkbox stroke draw.
// ArrowDown and PanelLeft are used only here, not in the registry. A missing name fails tsc (fail loud).
import * as React from 'react'
import {
  ArrowDown, Check, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, CircleCheck, Ellipsis,
  Info, LoaderCircle, Minus, OctagonX, PanelLeft, Search, TriangleAlert, X,
} from 'lucide'
import { IconSvg } from './Icon'
import type { IconNode } from './types'

type CompatProps = Omit<React.SVGProps<SVGSVGElement>, 'ref' | 'pathLength'> & {
  size?: number | string
  color?: string
  strokeWidth?: number | string
  absoluteStrokeWidth?: boolean
  ref?: React.Ref<SVGSVGElement>
}

function make(node: IconNode, name: string, extra?: { pathLength?: number } & Record<string, unknown>) {
  function C({ size, color, strokeWidth, absoluteStrokeWidth: _abs, style, ...p }: CompatProps) {
    const s = { ...(style as React.CSSProperties) } as React.CSSProperties & Record<string, string | number | undefined>
    if (color) s.color = color
    // lucide-react strokeWidth is on the 24 grid; the site stroke is screen pixels (non-scaling-stroke): 2 -> 1.5 px
    if (strokeWidth != null) s['--icon-stroke'] = `${Number(strokeWidth) * 0.75}px`
    return <IconSvg node={node} size={size} style={s} {...extra} {...p} />
  }
  C.displayName = name
  return C
}

export const ArrowDownIcon = make(ArrowDown, 'ArrowDownIcon')
export const CheckIcon = make(Check, 'CheckIcon', { 'data-draw': '', pathLength: 1 })
export const ChevronDownIcon = make(ChevronDown, 'ChevronDownIcon')
export const ChevronLeftIcon = make(ChevronLeft, 'ChevronLeftIcon')
export const ChevronRightIcon = make(ChevronRight, 'ChevronRightIcon')
export const ChevronUpIcon = make(ChevronUp, 'ChevronUpIcon')
export const CircleCheckIcon = make(CircleCheck, 'CircleCheckIcon')
export const InfoIcon = make(Info, 'InfoIcon')
export const Loader2Icon = make(LoaderCircle, 'Loader2Icon')
export const MinusIcon = make(Minus, 'MinusIcon')
export const MoreHorizontalIcon = make(Ellipsis, 'MoreHorizontalIcon')
export const OctagonXIcon = make(OctagonX, 'OctagonXIcon')
export const PanelLeftIcon = make(PanelLeft, 'PanelLeftIcon')
export const SearchIcon = make(Search, 'SearchIcon')
export const TriangleAlertIcon = make(TriangleAlert, 'TriangleAlertIcon')
export const XIcon = make(X, 'XIcon')

export const COMPAT_NAMES = [
  'ArrowDownIcon', 'CheckIcon', 'ChevronDownIcon', 'ChevronLeftIcon', 'ChevronRightIcon', 'ChevronUpIcon', 'CircleCheckIcon', 'InfoIcon',
  'Loader2Icon', 'MinusIcon', 'MoreHorizontalIcon', 'OctagonXIcon', 'PanelLeftIcon', 'SearchIcon', 'TriangleAlertIcon', 'XIcon',
] as const
