// Bookmark label editor (M12-FR-026; M12 §8.2 "书签编辑", §8.4 M key; AWR-14 §6.10): a Popover anchored to the Timeline
// readout, opened by M (after addBookmark), by the bookmark table of the Timeline tab or by the track's context menu.
// Labels are at most 64 characters (sanitised by stores/timeline); Enter saves, Esc closes, "delete" removes the mark.
// Shared bookmarks (operator or admin) go through runs REST, viewers keep local ones (M12 store actions).
import * as React from 'react'
import { useStore } from 'zustand'
import { useT } from '@/app/i18n'
import { createAwrStore } from '@/lib/createStore'
import { Button } from '@/ui/components/ui/button'
import { Field, FieldDescription, FieldLabel } from '@/ui/components/ui/field'
import { Input } from '@/ui/components/ui/input'
import { Popover, PopoverContent, PopoverHeader, PopoverTitle } from '@/ui/components/ui/popover'
import { timeline, useTimeline } from '@/stores/timeline'
import { simLabel } from '@/ui/layout/TimelineTrackArea'

export const bookmarkUiStore = createAwrStore<{ editId: string | null }>('ui.bookmark', () => ({ editId: null }))
export const bookmarkUi = {
  edit(id: string | null): void {
    bookmarkUiStore.setState({ editId: id })
  },
  /** M: add a bookmark at the seen time, then open its label editor */
  async addAndEdit(): Promise<void> {
    const b = await timeline.addBookmark('')
    if (b) bookmarkUiStore.setState({ editId: b.id })
  },
}

const LABEL_MAX = 64
const anchorEl = (): Element | null => (typeof document === 'undefined' ? null : document.querySelector('[data-timeline-readout]'))

export function BookmarkEditor() {
  const t = useT()
  const editId = useStore(bookmarkUiStore, (s) => s.editId)
  const bm = useTimeline((s) => s.bookmarks.find((b) => b.id === editId) ?? null)
  const [draft, setDraft] = React.useState('')
  const [forId, setForId] = React.useState<string | null>(null)
  if (bm && forId !== bm.id) {
    setForId(bm.id)
    setDraft(bm.label)
  }
  const open = editId !== null && bm !== null
  const save = () => {
    if (bm) void timeline.editBookmark(bm.id, draft)
    bookmarkUi.edit(null)
  }
  return (
    <Popover open={open} onOpenChange={(o) => {
      if (!o) bookmarkUi.edit(null)
    }}>
      <PopoverContent anchor={anchorEl} side="top" align="start" className="w-80" data-bookmark-editor="">
        <PopoverHeader>
          <PopoverTitle>{t('bookmark.title')}</PopoverTitle>
        </PopoverHeader>
        {bm ? (
          <form className="flex flex-col gap-3" onSubmit={(e) => {
            e.preventDefault()
            save()
          }}>
            <Field>
              <FieldLabel htmlFor="bm-label">{t('bookmark.label')}</FieldLabel>
              <Input id="bm-label" autoFocus value={draft} maxLength={LABEL_MAX} placeholder={t('bookmark.placeholder')}
                onChange={(e) => setDraft(e.target.value)} />
              <FieldDescription className="font-mono">{`${simLabel(bm.tS)} · ${t(bm.source === 'user' ? 'bookmark.shared' : 'bookmark.local')}`}</FieldDescription>
            </Field>
            <div className="flex items-center justify-between gap-2">
              <Button type="button" size="sm" variant="ghost" onClick={() => {
                void timeline.removeBookmark(bm.id)
                bookmarkUi.edit(null)
              }}>{t('bookmark.delete')}</Button>
              <Button type="submit" size="sm">{t('bookmark.save')}</Button>
            </div>
          </form>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}
