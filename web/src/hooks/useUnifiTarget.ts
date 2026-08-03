import useSWR from "swr";
import { errorHandlingFetcher } from "@/lib/fetcher";

const STATUS_ENDPOINT = "/api/admin/unifi-target/status";

export interface UnifiTargetStatus {
  target: "sim" | "real" | null;
  output: string;
  ok: boolean | null;
}

export default function useUnifiTarget() {
  const {
    data,
    error,
    isLoading,
    mutate: refetch,
  } = useSWR<UnifiTargetStatus>(STATUS_ENDPOINT, errorHandlingFetcher, {
    refreshInterval: 15000,
  });

  return {
    target: data?.target ?? null,
    lastOutput: data?.output,
    lastOk: data?.ok ?? null,
    isLoading,
    fetchError: error,
    refetch,
  };
}
