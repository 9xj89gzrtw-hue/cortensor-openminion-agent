// Regenerate the README screenshots from the latest HTML report.
// Usage: NODE_PATH=<npx cache>/node_modules node scripts/screenshots.mjs
import { createRequire } from "node:module";
import { readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

// Playwright is usually only present in the npx cache, and NODE_PATH is ignored
// by ESM imports, so resolve it through a CommonJS require.
const require = createRequire(import.meta.url);

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  console.error(
    "playwright not found. Install it (npm i -D playwright) or run:\n" +
      "  NODE_PATH=$(npm root -g) node scripts/screenshots.mjs",
  );
  process.exit(1);
}

// Use the installed Chrome: the bundled browser download is not always present,
// and the machine's Chrome is enough for a static HTML page.
const LAUNCH = process.env.PLAYWRIGHT_CHANNEL
  ? { channel: process.env.PLAYWRIGHT_CHANNEL }
  : { channel: "chrome" };

const root = resolve(new URL("..", import.meta.url).pathname);
const demo = join(root, "artifacts", "demo");
const reportsDir = join(demo, "reports");

const candidates = readdirSync(reportsDir)
  .filter((name) => name.endsWith("-smoke"))
  .map((name) => join(reportsDir, name, "report.html"))
  .filter((path) => {
    try {
      return statSync(path).isFile();
    } catch {
      return false;
    }
  })
  .sort();

if (!candidates.length) {
  console.error("No report.html found. Run ./canary demo first.");
  process.exit(1);
}

const report = candidates[candidates.length - 1];
const browser = await chromium.launch({ ...LAUNCH, headless: true });
const page = await browser.newPage({
  viewport: { width: 1280, height: 1000 },
  deviceScaleFactor: 2,
});

await page.goto(`file://${report}`, { waitUntil: "load" });
await page.waitForTimeout(250);

const shot = async (name, clip) => {
  const path = join(demo, name);
  await page.screenshot({ path, clip, fullPage: false });
  console.log(`wrote ${path}`);
};

// 1. Hero: title, verdict badge and the three KPI cards.
await shot("report-overview.png", { x: 0, y: 0, width: 1280, height: 620 });

// boundingBox() is viewport-relative; scroll by the element's document offset.
const scrollToSection = async (text, offset) => {
  await page.evaluate(
    ({ text, offset }) => {
      const heading = [...document.querySelectorAll("h2")].find((el) =>
        el.textContent.includes(text),
      );
      if (!heading) throw new Error(`section not found: ${text}`);
      const top = heading.getBoundingClientRect().top + window.scrollY;
      window.scrollTo(0, Math.max(0, top - offset));
    },
    { text, offset },
  );
  await page.waitForTimeout(200);
};

// 2. Gates table + quality bars.
await scrollToSection("Gates", 40);
await shot("report-gates.png", { x: 0, y: 0, width: 1280, height: 900 });

// 3. Per-case results table.
await scrollToSection("Per-case results", 30);
await shot("report-cases.png", { x: 0, y: 0, width: 1280, height: 780 });

// 4. Full page, used as a printable one-image summary in the submission.
await page.evaluate(() => window.scrollTo(0, 0));
await page.screenshot({ path: join(demo, "report-full.png"), fullPage: true });
console.log(`wrote ${join(demo, "report-full.png")}`);

await browser.close();
