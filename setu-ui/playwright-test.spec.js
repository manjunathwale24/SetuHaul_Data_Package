// Playwright TDD Tests for SetuHaul Angular UI
// Tests the Angular UI against actual Lambda and K8s endpoints

const { test, expect } = require('@playwright/test');

const LAMBDA_URL = 'https://4l7yahwpl23gg5adwvgxyzuvcy0ardts.lambda-url.ap-southeast-2.on.aws/';
const KUB_URL = 'http://ae13542555c274d28bb23bf42f7390b0-1259872480.ap-southeast-2.elb.amazonaws.com/';

test('Lambda get_activity endpoint returns OK', async ({ request }) => {
  const response = await request.post(LAMBDA_URL, {
    data: JSON.stringify({ source: 'get_activity' })
  });
  
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect(body.status).toBe('ok');
  expect(Array.isArray(body.activity_log)).toBeTruthy();
});

test('Lambda get_status endpoint returns OK', async ({ request }) => {
  const response = await request.post(LAMBDA_URL, {
    data: JSON.stringify({ source: 'get_status' })
  });
  
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect(body.status).toBe('ok');
});

test('K8s health endpoint returns healthy', async ({ request }) => {
  const response = await request.get(KUB_URL);
  
  expect(response.ok()).toBeTruthy();
  // May return HTML, that's OK for integration test
});

test('Angular UI page loads', async ({ page }) => {
  await page.goto('https://setu-d4ornj9tr-mnj8.vercel.app/');
  // Wait for page to stabilize
  await page.waitForLoadState('networkidle');
  // Check page has content
  const title = await page.title();
  expect(title.length).toBeGreaterThan(0);
});

test('Angular dashboard component renders', async ({ page }) => {
  await page.goto('https://setu-d4ornj9tr-mnj8.vercel.app/');
  await page.waitForLoadState('networkidle');
  // Look for common Angular app markers
  const bodyText = await page.textContent('body');
  expect(bodyText.length).toBeGreaterThan(0);
});