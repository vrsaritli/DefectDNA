import type { Page, Route } from "@playwright/test";

const code = (value: string, label: string) => ({ code: value, label });

export const rca = {
  id: "rca-1",
  status: "AI_PROPOSED",
  version: 1,
  defect: { ado_id: 419, title: "Saved card cannot be deleted", module: "Shop\\API\\Payments" },
  proposed: {
    root_cause: code("CODE", "Code"),
    sdlc_phase: code("DEVELOPMENT", "Development"),
    testing_gap: code("INSUFFICIENT_COVERAGE", "Insufficient coverage"),
    other_proposal: null,
    confidence: 0.86,
    rationale: "The delete endpoint ignored the token id parameter.",
  },
  final: null,
  retrieved_cases: [
    {
      ref: "C1",
      defect_id: 402,
      similarity: 0.81,
      title: "Saved card still listed after delete",
      root_cause: code("CODE", "Code"),
      sdlc_phase: null,
      testing_gap: code("NO_TEST_CASE", "No test case"),
      source: "REVIEW",
    },
  ],
  cited_cases: [{ ref: "C1", defect_id: 402, similarity: 0.81, root_cause: code("CODE", "Code") }],
  retrieval_status: "OK",
  failure_reason: null,
  taxonomy_version: 1,
  model: { llm: "openai/gpt-oss-120b", prompt_version: "rca-v2" },
  llm_attempts: 1,
  llm_batch: { size: 3, usage: { total_tokens: 3200 } },
  scrub_counts: {},
  analyzed_at: "2026-11-22T10:00:00Z",
};

const summary = {
  total: 3,
  approved: 2,
  corrected: 1,
  seeded: 0,
  ai_agreement_rate: 0.6667,
  by_root_cause: [
    { key: "CODE", count: 2 },
    { key: "CONFIGURATION", count: 1 },
  ],
  by_module: [{ key: "Shop\\API\\Payments", count: 3 }],
  by_release: [{ key: "Release 1", count: 3 }],
  by_sdlc_phase: [{ key: "DEVELOPMENT", count: 3 }],
  by_testing_gap: [{ key: "NO_TEST_CASE", count: 3 }],
  by_severity: [{ key: "2 - High", count: 3 }],
  by_month: [{ month: "2026-10", total: 3, by_root_cause: { CODE: 2, CONFIGURATION: 1 } }],
  filters: { modules: ["Shop\\API\\Payments"], releases: ["Release 1"], months: ["2026-10"] },
  labels: {
    root_cause: { CODE: "Code", CONFIGURATION: "Configuration" },
    sdlc_phase: { DEVELOPMENT: "Development" },
    testing_gap: { NO_TEST_CASE: "No test case" },
  },
  note: "Counts include approved and corrected RCAs only.",
};

export type MockState = { mustChangePassword: boolean; reviews: unknown[]; role: string; analyses?: unknown[] };

export async function mockApi(page: Page, state: MockState): Promise<void> {
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    const method = route.request().method();

    if (path === "/auth/login") {
      return json(route, {
        csrf_token: "csrf-1",
        user: { id: "u1", username: "lead@example.test", must_change_password: state.mustChangePassword },
      });
    }
    if (path === "/auth/change-password") {
      state.mustChangePassword = false;
      return json(route, { status: "ok", csrf_token: "csrf-2" });
    }
    if (path === "/projects") return json(route, { projects: [{ id: "demo", name: "Demo", role: state.role }] });
    if (path.endsWith("/trends/summary")) return json(route, summary);
    if (path.endsWith("/trends")) {
      return json(route, {
        group_by: ["module"],
        rows: [{ module: "Shop\\API\\Payments", total: 3, by_root_cause: { CODE: 2, CONFIGURATION: 1 } }],
        labels: summary.labels,
      });
    }
    if (path.endsWith("/taxonomy")) {
      return json(route, {
        version: 1,
        root_causes: [code("CODE", "Code"), code("DATA", "Data")],
        sdlc_phases: [code("DEVELOPMENT", "Development")],
        testing_gaps: [code("INSUFFICIENT_COVERAGE", "Insufficient coverage")],
      });
    }
    if (path.endsWith("/knowledge/stats")) {
      return json(route, { total: 1, active: 1, pending_embedding: 0, by_source: {}, rag_enabled: true, current_embedding_model: "embed-english-v3.0" });
    }
    if (path.endsWith("/import-batches")) {
      return json(route, {
        batches: [
          { id: "batch-1", source: "ADO_QUERY", label: "Query 1a2b3c4d", defects: 3, created_at: "2026-11-20T10:00:00Z" },
          { id: "batch-0", source: "CSV_IMPORT", label: "defects.xlsx", defects: 5, created_at: "2026-11-19T10:00:00Z" },
        ],
      });
    }
    if (path.endsWith("/ado-connection")) {
      return json(route, { connected: true, organization: "https://dev.azure.com/acme", project: "Shop", source: "project", checked_at: "2026-11-22T10:00:00Z", pat_configured: true });
    }
    if (path.endsWith("/analyses") && method === "POST") {
      (state.analyses ??= []).push(route.request().postDataJSON());
      return json(route, { job_id: null, queued: 0, skipped_reviewed: 2, skipped_reviewed_ids: [303, 450], skipped_already_analyzed: 1, remaining: 0, import_label: "Query 1a2b3c4d" });
    }
    if (path.endsWith("/defects")) {
      return json(route, {
        total: 1,
        defects: [
          {
            ado_id: 419, title: rca.defect.title, module: rca.defect.module, release: "Release 1", state: "Closed",
            rca: { id: "rca-1", status: "AI_PROPOSED", root_cause: code("CODE", "Code"), sdlc_phase: code("DEVELOPMENT", "Development"), testing_gap: code("NO_TEST_CASE", "No test case"), confidence: 0.82 },
          },
        ],
      });
    }
    if (path.endsWith("/rcas")) return json(route, { total: 1, rcas: [rca] });
    if (path === "/rcas/rca-1/review" && method === "POST") {
      state.reviews.push(route.request().postDataJSON());
      return json(route, { ...rca, status: "APPROVED", final: { ...rca.proposed, note: null }, knowledge_status: "ACTIVE" });
    }
    if (path === "/rcas/rca-1") return json(route, rca);
    if (path.includes("/defects/419")) {
      return json(route, {
        defect: { ado_id: 419, title: rca.defect.title, module: rca.defect.module, release: "Release 1", severity: "2 - High", priority: "2", state: "Closed", description: "Card still listed.", repro_steps: "Delete a saved card", comments: "" },
        rca,
      });
    }
    return json(route, { error: { code: "NOT_MOCKED", message: `${method} ${path}` } }, 404);
  });
}

export async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Username").fill("lead@example.test");
  await page.getByLabel("Password").fill("Existing-Password-123!");
  await page.getByRole("button", { name: "Sign in" }).click();
}
