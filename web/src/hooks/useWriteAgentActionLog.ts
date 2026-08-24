import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";

const ENDPOINT = "/api/admin/write-agent-action-log";

export type WriteAgentActionOutcome =
  | "EXECUTED"
  | "BLOCKED_NOT_ADMIN"
  | "FAILED";

export interface WriteAgentActionLogEntry {
  id: number;
  user_email: string | null;
  tool_name: string;
  mcp_server_name: string | null;
  outcome: WriteAgentActionOutcome;
  detail: string | null;
  created_at: string;
}

interface WriteAgentActionLogResponse {
  entries: WriteAgentActionLogEntry[];
}

export default function useWriteAgentActionLog() {
  const { data, error, isLoading, mutate } =
    useSWR<WriteAgentActionLogResponse>(ENDPOINT, errorHandlingFetcher, {
      refreshInterval: 15000,
    });

  return {
    entries: data?.entries ?? [],
    isLoading,
    fetchError: error,
    refetch: mutate,
  };
}
