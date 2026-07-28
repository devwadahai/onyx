// Original stub — NOT derived from onyx-dot-app/onyx's actual ee/ source
// (never read; separately licensed — see ../../../../../../basalt/NOTICE.md).
// Gated the same way as ee/sections/SearchUI.tsx — see that file's header.
export async function searchDocuments(
  _query: string,
  _opts?: { filters?: unknown; numHits?: number },
): Promise<{ search_docs: never[] }> {
  return { search_docs: [] }
}
