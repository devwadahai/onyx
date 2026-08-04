"use client";

import { useSettings } from "@/lib/settings/hooks";
import { DEFAULT_LOGO_SIZE_PX } from "@/lib/constants";
import { cn } from "@opal/utils";
import Truncated from "@/refresh-components/texts/Truncated";
import { SvgOnyxLogo, SvgOnyxLogoTyped } from "@opal/logos";

export interface LogoProps {
  folded?: boolean;
  size?: number;
  className?: string;
  // Always render the real Onyx logo, ignoring enterprise white-label settings
  // (custom logo / application name). Used by Onyx-branded surfaces like Craft.
  onyxBranded?: boolean;
}

// Onyx's own enterprise white-label logo (enterprise?.use_custom_logo /
// application_name, set via ee/onyx/server/enterprise_settings) is gated
// behind the NEXT_PUBLIC_ENABLE_PAID_EE_FEATURES build-time flag — off in
// this deployment, and deliberately not flipped on just for this, since
// that flag also gates other Enterprise-only UI/analytics this deployment
// isn't licensed for. So this is a direct, always-on fallback instead of
// relying on that system.
const NOVOLINK_LOGO_URL = "/novolink-logo.png";
const NOVOLINK_APPLICATION_NAME = "NovoLink AI";

export function Logo({ folded, size, className, onyxBranded }: LogoProps) {
  const resolvedSize = size ?? DEFAULT_LOGO_SIZE_PX;
  const { enterprise, logoUrl } = useSettings();
  const logoDisplayStyle = enterprise?.logo_display_style;
  const applicationName = enterprise?.application_name ?? NOVOLINK_APPLICATION_NAME;

  if (onyxBranded) {
    return folded ? (
      <SvgOnyxLogo size={resolvedSize} className={cn("shrink-0", className)} />
    ) : (
      <SvgOnyxLogoTyped size={resolvedSize} className={className} />
    );
  }

  const logo = (
    <div
      className={cn(
        "aspect-square rounded-full overflow-hidden relative shrink-0",
        className
      )}
      style={{ height: resolvedSize }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        alt="Logo"
        src={logoUrl ?? NOVOLINK_LOGO_URL}
        className="object-cover object-center w-full h-full"
      />
    </div>
  );

  const renderNameAndPoweredBy = (opts: {
    includeLogo: boolean;
    includeName: boolean;
  }) => {
    return (
      <div className="flex min-w-0 gap-2">
        {opts.includeLogo && logo}
        {!folded && (
          /* H3 text is 4px larger (28px) than the Logo icon (24px), so negative margin hack. */
          <div className="flex flex-1 flex-col -mt-0.5">
            {opts.includeName && (
              <Truncated headingH3>{applicationName}</Truncated>
            )}
            {/* "Powered by Onyx" removed for this private, non-public
                deployment — same EE-gating issue as the logo (see above)
                means the normal hide_onyx_branding toggle isn't reachable
                here either. NEXT_PUBLIC_DO_NOT_USE_TOGGLE_OFF_DANSWER_POWERED
                intentionally left untouched. */}
          </div>
        )}
      </div>
    );
  };

  // Handle "logo_only" display style
  if (logoDisplayStyle === "logo_only") {
    return renderNameAndPoweredBy({ includeLogo: true, includeName: false });
  }

  // Handle "name_only" display style
  if (logoDisplayStyle === "name_only") {
    return renderNameAndPoweredBy({ includeLogo: false, includeName: true });
  }

  // Handle "logo_and_name" or default behavior
  return applicationName ? (
    renderNameAndPoweredBy({ includeLogo: true, includeName: true })
  ) : folded ? (
    <SvgOnyxLogo size={resolvedSize} className={cn("shrink-0", className)} />
  ) : (
    <SvgOnyxLogoTyped size={resolvedSize} className={className} />
  );
}
