"use client";

import { useMemo } from "react";
import { SettingsLayouts } from "@opal/layouts";
import { Table, Text } from "@opal/components";
import { createTableColumns } from "@opal/components/table/columns";
import { SvgCheckCircle, SvgXCircle, SvgAlertCircle } from "@opal/icons";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import useWriteAgentActionLog, {
  WriteAgentActionLogEntry,
  WriteAgentActionOutcome,
} from "@/hooks/useWriteAgentActionLog";

const route = ADMIN_ROUTES.WRITE_AGENT_ACTION_LOG;

const OUTCOME_DISPLAY: Record<
  WriteAgentActionOutcome,
  { label: string; icon: typeof SvgCheckCircle; className: string }
> = {
  EXECUTED: {
    label: "Executed",
    icon: SvgCheckCircle,
    className: "text-status-success-05!",
  },
  BLOCKED_NOT_ADMIN: {
    label: "Blocked — not admin",
    icon: SvgAlertCircle,
    className: "text-status-warning-05!",
  },
  FAILED: {
    label: "Failed",
    icon: SvgXCircle,
    className: "text-status-error-05!",
  },
};

const tc = createTableColumns<WriteAgentActionLogEntry>();

export default function WriteAgentActionLogPage() {
  const { entries, isLoading, fetchError } = useWriteAgentActionLog();

  const columns = useMemo(
    () => [
      tc.column("created_at", {
        header: "When",
        weight: 14,
        cell: (value) => (
          <div className="whitespace-nowrap">
            <Text color="text-03">
              {new Date(value).toLocaleString(undefined, {
                dateStyle: "short",
                timeStyle: "short",
              })}
            </Text>
          </div>
        ),
      }),
      tc.column("user_email", {
        header: "User",
        weight: 20,
        cell: (value) => <Text>{value ?? "(unknown user)"}</Text>,
      }),
      tc.column("tool_name", {
        header: "Action",
        weight: 16,
        cell: (value) => (
          <Text font="secondary-mono" color="text-03">
            {value}
          </Text>
        ),
      }),
      tc.column("outcome", {
        header: "Outcome",
        weight: 18,
        cell: (value) => {
          const display = OUTCOME_DISPLAY[value];
          const Icon = display.icon;
          return (
            <div className="flex flex-row items-center gap-1">
              <Icon size={16} className={display.className} />
              <Text>{display.label}</Text>
            </div>
          );
        },
      }),
      tc.column("detail", {
        header: "Detail",
        weight: 32,
        cell: (value) => (
          <div className="truncate max-w-md" title={value ?? ""}>
            <Text color="text-03">{value ?? ""}</Text>
          </div>
        ),
      }),
    ],
    []
  );

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={route.icon}
        title={route.title}
        description="Every action the Write-Capable UniFi Actions Agent has executed or attempted — who confirmed it, when, and the result. Includes rejected attempts by non-admin users, not just successful ones. Most recent 200 entries."
        divider
      />
      <SettingsLayouts.Body>
        {fetchError && (
          <Text color="text-03">
            Couldn&apos;t load the action log. Try refreshing the page.
          </Text>
        )}
        {!fetchError && !isLoading && entries.length === 0 && (
          <Text color="text-03">
            No actions have been proposed or executed yet.
          </Text>
        )}
        {entries.length > 0 && (
          <Table
            data={entries}
            getRowId={(row) => String(row.id)}
            columns={columns}
          />
        )}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
