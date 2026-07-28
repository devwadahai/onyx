// Original stub — NOT derived from onyx-dot-app/onyx's actual ee/ source
// (never read; that's under a separate, non-permissive license — see
// ../../../../../basalt/NOTICE.md). This file exists only so paidTierGated()
// in src/ce.tsx (real CE code, MIT-licensed) has something to import: since
// this experiment runs Basalt as a single-tier backend, useTierAtLeast()
// is always false here, so paidTierGated always renders the Invisible
// passthrough instead of this component — its actual body never executes.
// The prop signature matches only what src/views/AppPage.tsx's call site
// (CE code) passes in, inferred from that call site, not from EE source.
import type { MinimalOnyxDocument } from '@/lib/search/interfaces'

interface SearchUIProps {
  onDocumentClick?: (doc: MinimalOnyxDocument) => void
}

export default function SearchUI(_props: SearchUIProps) {
  return null
}
