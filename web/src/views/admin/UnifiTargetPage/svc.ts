import { UnifiTargetStatus } from "@/hooks/useUnifiTarget";

const SWITCH_ENDPOINT = "/api/admin/unifi-target/switch";

export async function switchUnifiTarget(
  target: "sim" | "real"
): Promise<{ ok: boolean; data?: UnifiTargetStatus; error?: string }> {
  const resp = await fetch(SWITCH_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    return { ok: false, error: body.detail ?? `Request failed (${resp.status})` };
  }
  const data: UnifiTargetStatus = await resp.json();
  return { ok: true, data };
}
