import type { Target } from '@awr/contracts/types'
import { getLocale } from '@/app/i18n'
import { TextField } from '@/ui/sandbox/fields'

/** Identity stays the target id; descriptive edits do not restart its motion. */
export function IdentityEditor({ target, operator, patch }: {
  target: Target; operator: boolean; patch: (p: Partial<Target>) => Promise<boolean>
}) {
  const zh = getLocale() === 'zh-CN'
  const metadata = target.metadata ?? {}
  const save = (key: keyof NonNullable<Target['metadata']>, value: string) => patch({ metadata: { ...metadata, [key]: value } })
  return <div className="col-span-2 grid grid-cols-2 gap-2" data-target-identity="">
    <TextField id="target-role" label={zh ? '角色' : 'Role'} value={metadata.role ?? ''} readOnly={!operator}
      validate={(s) => s.length <= 48} onCommit={(s) => save('role', s)} />
    <TextField id="target-description" label={zh ? '外观 / 身份说明' : 'Description'} value={metadata.description ?? ''} readOnly={!operator}
      validate={(s) => s.length <= 256} onCommit={(s) => save('description', s)} />
    {target.class === 'vehicle' ? <>
      <TextField id="target-plate" label={zh ? '车牌号码' : 'Plate number'} value={metadata.plate_text ?? ''} readOnly={!operator}
        validate={(s) => /^[A-Z0-9一-鿿 -]{0,16}$/.test(s.toUpperCase())} onCommit={(s) => save('plate_text', s.toUpperCase())} />
      <TextField id="target-plate-region" label={zh ? '车牌所属地区' : 'Plate region'} value={metadata.plate_region ?? ''} readOnly={!operator}
        validate={(s) => s.length <= 32} onCommit={(s) => save('plate_region', s)} />
    </> : null}
  </div>
}
