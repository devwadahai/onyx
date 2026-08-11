import { redirect } from "next/navigation";
import type { Route } from "next";
import { unstable_noStore as noStore } from "next/cache";
import { requireAuth } from "@/lib/auth/svcSS";
import { ProjectsProvider } from "@/providers/ProjectsContext";
import { VoiceModeProvider } from "@/providers/VoiceModeProvider";
import AppSidebar from "@/sections/sidebar/AppSidebar";
// Import the client layout boundary directly. The aggregate @opal/layouts
// barrel also exports toast/store modules that use React client hooks; pulling
// that barrel into this server layout makes Next traverse the store as a
// Server Component dependency during dev/build.
import * as RootLayout from "@opal/layouts/root/components";
import AppChrome from "@/layouts/chromes/AppChrome";

export interface LayoutProps {
  children: React.ReactNode;
}

export default async function Layout({ children }: LayoutProps) {
  noStore();

  // Only check authentication - data fetching is done client-side via SWR hooks
  const authResult = await requireAuth();

  if (authResult.redirect) {
    redirect(authResult.redirect as Route);
  }

  return (
    <ProjectsProvider>
      {/* VoiceModeProvider wraps the full app layout so TTS playback state
          persists across page navigations (e.g., sidebar clicks during playback).
          It only activates WebSocket connections when TTS is actually triggered. */}
      <VoiceModeProvider>
        <RootLayout.Root>
          <AppSidebar />
          <AppChrome>{children}</AppChrome>
        </RootLayout.Root>
      </VoiceModeProvider>
    </ProjectsProvider>
  );
}
