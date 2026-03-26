const puppeteer = require('puppeteer');

const targetUrl = process.env.TARGET_URL || 'https://gemini.google.com';
const timeoutMs = Number(process.env.NAV_TIMEOUT_MS || '60000');
const captureWaitMs = Number(process.env.CAPTURE_WAIT_MS || '6000');

function addIfHttpUrl(set, value, baseUrl) {
  if (!value || typeof value !== 'string') return;
  const input = value.trim();
  if (!input || input.startsWith('data:') || input.startsWith('javascript:')) return;

  try {
    const normalized = new URL(input, baseUrl).toString();
    if (normalized.startsWith('http://') || normalized.startsWith('https://')) {
      set.add(normalized);
    }
  } catch (_) {
    // ignore malformed URL
  }
}

(async () => {
  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();

  await page.setUserAgent(
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  );

  const resourceUrls = new Set();
  page.on('request', (req) => {
    addIfHttpUrl(resourceUrls, req.url(), targetUrl);
  });
  page.on('response', (response) => {
    addIfHttpUrl(resourceUrls, response.url(), targetUrl);
  });
  page.on('framenavigated', (frame) => {
    addIfHttpUrl(resourceUrls, frame.url(), targetUrl);
  });

  try {
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    await new Promise((resolve) => setTimeout(resolve, Math.max(captureWaitMs, 0)));

    const domUrls = await page.evaluate(() => {
      const values = new Set();
      const selectors = ['[src]', '[href]', '[data-play]', '[data-src]', '[data-url]'];
      const attrs = ['src', 'href', 'data-play', 'data-src', 'data-url'];
      for (const selector of selectors) {
        document.querySelectorAll(selector).forEach((el) => {
          for (const attr of attrs) {
            const v = el.getAttribute(attr);
            if (v) values.add(v);
          }
        });
      }

      if (Array.isArray(performance.getEntriesByType('resource'))) {
        performance.getEntriesByType('resource').forEach((entry) => {
          if (entry && typeof entry.name === 'string') values.add(entry.name);
        });
      }
      return Array.from(values);
    });
    domUrls.forEach((u) => addIfHttpUrl(resourceUrls, u, targetUrl));
  } catch (_) {
    // 导航超时或跳转不阻断输出，尽可能返回已截获资源。
  } finally {
    await browser.close();
  }

  process.stdout.write(JSON.stringify(Array.from(resourceUrls)));
})().catch((err) => {
  process.stderr.write(String(err?.message || err));
  process.exit(1);
});
