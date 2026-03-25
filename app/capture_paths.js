const puppeteer = require('puppeteer');

const targetUrl = process.env.TARGET_URL || 'https://gemini.google.com';
const timeoutMs = Number(process.env.NAV_TIMEOUT_MS || '60000');

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
  page.on('response', (response) => {
    const url = response.url();
    if (!url.startsWith('data:')) {
      resourceUrls.add(url);
    }
  });

  try {
    await page.goto(targetUrl, { waitUntil: 'networkidle2', timeout: timeoutMs });
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
