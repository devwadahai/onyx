import AgentEditorPage from "@/views/AgentEditorPage";

// Route modules must expose a page component whose props match Next's page
// contract. The editor itself accepts optional editor state, so wrap it at
// the route boundary instead of re-exporting that broader component type.
export default function Page() {
  return <AgentEditorPage />;
}
