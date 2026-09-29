// In-app navigation through the router (pushState) rendered as a shadcn Button link.
import type * as React from 'react'
import { Button } from '@/ui/components/ui/button'
import { navigate } from './router'

export function Link({ to, children, variant = 'link', size = 'sm', className }: { to: string; children: React.ReactNode; variant?: 'link' | 'ghost' | 'outline'; size?: 'sm' | 'xs' | 'default'; className?: string }) {
  return (
    <Button
      variant={variant}
      size={size}
      className={className}
      render={<a href={to} />}
      nativeButton={false}
      onClick={(e: React.MouseEvent) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
        e.preventDefault()
        navigate(to)
      }}
    >
      {children}
    </Button>
  )
}
