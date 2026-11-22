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
