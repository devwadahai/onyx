"use client";

import { useState } from "react";
import { SettingsLayouts, toast } from "@opal/layouts";
import { SvgCheckCircle, SvgDevKit, SvgHome } from "@opal/icons";
import { ADMIN_ROUTES } from "@/lib/admin-routes";
import { Section } from "@/layouts/general-layouts";
import { Button, SelectCard } from "@opal/components";
import { Card, ContentAction } from "@opal/layouts";
import { Disabled } from "@opal/core";
import Text from "@/refresh-components/texts/Text";
import useUnifiTarget from "@/hooks/useUnifiTarget";
import { switchUnifiTarget } from "@/views/admin/UnifiTargetPage/svc";

const route = ADMIN_ROUTES.UNIFI_TARGET;

interface TargetOption {
  key: "sim" | "real";
  title: string;
  description: string;
  icon: typeof SvgDevKit;
}

const TARGET_OPTIONS: TargetOption[] = [
  {
    key: "sim",
    title: "Simulator",
    description: "unifi-sim fixture data — safe for any testing",
    icon: SvgDevKit,
  },
  {
    key: "real",
    title: "Real Console (36acresedge)",
    description: "Live network — needs the Teleport tunnel/hotspot up",
    icon: SvgHome,
  },
];

export default function UnifiTargetPage() {
  const { target, lastOutput, isLoading, refetch } = useUnifiTarget();
  const [switching, setSwitching] = useState<"sim" | "real" | null>(null);

  async function handleSwitch(key: "sim" | "real") {
    setSwitching(key);
    try {
      const result = await switchUnifiTarget(key);
      if (!result.ok) {
        toast.error(result.error ?? `Failed to switch to ${key}`);
        return;
      }
      toast.success(`unifi-network-mcp now targeting ${key === "sim" ? "the simulator" : "the real 36acresedge console"}`);
      refetch();
    } finally {
      setSwitching(null);
    }
  }

  return (
    <SettingsLayouts.Root>
      <SettingsLayouts.Header
        icon={route.icon}
        title={route.title}
        description="Choose whether the UniFi Network MCP server talks to the local simulator or the real 36acresedge console. Both share the same MCP Action URL — no need to reconnect it in Actions after switching."
        divider
      />
      <SettingsLayouts.Body>
        <Section flexDirection="column" gap={0.75}>
          {TARGET_OPTIONS.map((option) => {
            const isActive = target === option.key;
            const isBusy = switching === option.key;
            return (
              <SelectCard
                key={option.key}
                state={isActive ? "filled" : "empty"}
                padding="sm"
                rounding="lg"
              >
                <Card.Header>
                  <ContentAction
                    sizePreset="main-ui"
                    variant="section"
                    icon={option.icon}
                    title={option.title}
                    description={option.description}
                    padding="lg"
                    rightChildren={
                      isActive ? (
                        <Section
                          flexDirection="row"
                          alignItems="center"
                          gap={0.25}
                        >
                          <Text mainUiAction text03>
                            Active
                          </Text>
                          <SvgCheckCircle
                            size={16}
                            className="text-status-success-05!"
                          />
                        </Section>
                      ) : (
                        <Disabled disabled={switching !== null || isLoading}>
                          <Button
                            prominence="secondary"
                            size="md"
                            onClick={() => handleSwitch(option.key)}
                          >
                            {isBusy ? "Switching..." : "Switch"}
                          </Button>
                        </Disabled>
                      )
                    }
                  />
                </Card.Header>
              </SelectCard>
            );
          })}
        </Section>

        {lastOutput && (
          <Section flexDirection="column" gap={0.25} padding={0.5}>
            <Text mainUiAction text03>
              Last switch output
            </Text>
            <pre className="whitespace-pre-wrap text-xs bg-background-tint-01 rounded-lg p-3 max-h-64 overflow-y-auto">
              {lastOutput}
            </pre>
          </Section>
        )}
      </SettingsLayouts.Body>
    </SettingsLayouts.Root>
  );
}
