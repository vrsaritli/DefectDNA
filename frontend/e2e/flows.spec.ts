import { expect, test } from "@playwright/test";
import { type MockState, mockApi, signIn } from "./mock-api";

test("first sign-in forces a password change, then shows the trends dashboard", async ({ page }) => {
  const state: MockState = { mustChangePassword: true, reviews: [], role: "QA_LEAD" };
  await mockApi(page, state);
  await signIn(page);

  await expect(page.getByRole("heading", { name: "Change your password" })).toBeVisible();
  await page.getByLabel("New password").fill("A-Brand-New-Password-456!");
  await page.getByRole("button", { name: "Update password" }).click();

  await expect(page.getByRole("heading", { name: "Root-cause trends" })).toBeVisible();
  const stat = (title: string) =>
    page.locator(".stat-card", { has: page.getByText(title, { exact: true }) });
  await expect(stat("Reviewed RCAs").locator(".stat-value")).toHaveText("3");
  await expect(stat("AI agreement").locator(".stat-value")).toHaveText("67%");
  await expect(page.getByRole("table")).toContainText("API › Payments");
});

test("a QA Lead reviews an AI proposal with its cited case and approves it", async ({ page }) => {
  const state: MockState = { mustChangePassword: false, reviews: [], role: "QA_LEAD" };
  await mockApi(page, state);
  await signIn(page);

  await page.getByRole("button", { name: "AI analysis" }).click();
  await expect(page.getByText("Knowledge base: 1 confirmed case")).toBeVisible();
  await page.getByRole("cell", { name: "Saved card cannot be deleted" }).click();

  await expect(page.getByText("Similar confirmed cases")).toBeVisible();
  await expect(page.getByText("Cited by AI")).toBeVisible();
  await page.getByRole("button", { name: "Approve AI proposal" }).click();

  await expect(page.getByText("#419 approved.")).toBeVisible();
  expect(state.reviews).toEqual([{ decision: "APPROVE", reopen: false }]);
});

test("an Admin analyzes one import, sees already-confirmed IDs, and can reject a proposal", async ({ page }) => {
  const state: MockState = { mustChangePassword: false, reviews: [], role: "ADMIN" };
  await mockApi(page, state);
  await signIn(page);

  await page.getByRole("button", { name: "AI analysis" }).click();
  await expect(page.getByLabel("Imported defects")).toHaveValue("batch-1");
  await page.getByRole("button", { name: "Analyze defects", exact: true }).click();
  await expect(page.getByText("2 defects already have a confirmed RCA.")).toBeVisible();
  await expect(page.locator(".id-chip")).toHaveText(["#303", "#450"]);
  expect(state.analyses).toEqual([{ import_batch_id: "batch-1", reanalyze: false }]);

  await page.getByRole("cell", { name: "Saved card cannot be deleted" }).click();
  await expect(page.getByRole("link", { name: "Open in Azure DevOps" })).toHaveAttribute(
    "href",
    "https://dev.azure.com/acme/Shop/_workitems/edit/419",
  );
  await page.getByRole("button", { name: "Reject" }).click();
  await page.getByRole("button", { name: "Confirm reject" }).click();
  expect(state.reviews).toEqual([{ decision: "REJECT", reopen: false, note: null }]);
});

test("the defect register shows each defect's RCA, confidence and status", async ({ page }) => {
  const state: MockState = { mustChangePassword: false, reviews: [], role: "VIEWER" };
  await mockApi(page, state);
  await signIn(page);

  await page.getByRole("button", { name: "Defects" }).click();
  const row = page.getByRole("row", { name: /Saved card cannot be deleted/ });
  await expect(row).toContainText("Code");
  await expect(row).toContainText("No test case");
  await expect(row).toContainText("82%");
  await expect(row).toContainText("AI proposed");
});
