// Original stub — NOT derived from onyx-dot-app/onyx's actual ee/ source
// (never read; separately licensed — see ../../../../../basalt/NOTICE.md).
// src/providers/QueryControllerProvider.tsx (real CE code) wraps this in
// paidTierGated(), which on a single-tier backend like Basalt always
// renders the "Invisible" passthrough instead — so this body never
// actually executes. It exists only so the import resolves. Signature
// matches the passthrough's own children-only usage, not the EE source.
import type { ReactNode } from 'react'

export function QueryControllerProvider({ children }: { children?: ReactNode }) {
  return <>{children}</>
}
