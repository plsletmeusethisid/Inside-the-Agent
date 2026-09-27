// Run from the project root; uses real OpenAI calls and a locally installed Edge.
const { chromium } = require('../.verification/node_modules/playwright-core');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const demoUrl = process.env.DEMO_URL || 'http://localhost:3000';
const apiUrl = process.env.DEMO_API_URL || 'http://localhost:8000';

const frames = text => text.split(/\r?\n/).filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)));
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.setDefaultTimeout(20000);
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      window.traceBodies = {};
      const originalFetch = window.fetch;
      window.fetch = async (...args) => {
        const response = await originalFetch(...args);
        if (response.headers.get('content-type')?.includes('text/event-stream')) {
          const key = new URL(String(args[0]), window.location.href).href;
          response.clone().text().then(text => { window.traceBodies[key] = text; }).catch(() => {});
        }
        return response;
      };
      window.traceObservations = { draft: false, running: false, evidenceWhileRunning: false };
      new MutationObserver(() => {
        if (document.querySelector('.draft-answer')) window.traceObservations.draft = true;
        if (document.querySelector('.trace-stage.running')) window.traceObservations.running = true;
        if (document.querySelector('.retrieval-output[aria-busy=true] .retrieval-results')) window.traceObservations.evidenceWhileRunning = true;
      }).observe(document, { subtree: true, childList: true, attributes: true });
    });
    const readyPromise = page.waitForResponse(r => r.url().endsWith('/ready'));
    await page.goto(demoUrl);
    const ready = await readyPromise;
    assert.equal(ready.status(), 200);
    if (new URL(apiUrl).origin !== new URL(demoUrl).origin) {
      assert.equal(ready.headers()['access-control-allow-origin'], new URL(demoUrl).origin);
    }
    await page.waitForSelector('.service-status.ready');
    await page.locator('input[type=file]').setInputFiles(path.resolve('examples/it-support-guide.pdf'));
    await page.locator('input[type=range]').nth(0).press('Home');
    await page.locator('input[type=range]').nth(1).press('Home');
    await page.locator('input[type=range]').nth(1).press('ArrowRight');
    await page.locator('input[type=range]').nth(1).press('ArrowRight');
    const uploadPromise = page.waitForResponse(r => r.url().endsWith('/documents/stream'));
    await page.getByRole('button', { name: 'Parse & inspect chunks' }).click();
    async function readStream(response) {
      await page.waitForFunction(url => typeof window.traceBodies[url] === 'string', response.url(), { timeout: 120000 });
      const text = await page.evaluate(url => { const text = window.traceBodies[url]; delete window.traceBodies[url]; return text; }, response.url());
      return frames(text);
    }
    const upload = await readStream(await uploadPromise);
    assert.equal(upload.at(-1).event, 'completed', JSON.stringify(upload.at(-1)));
    const doc = upload.at(-1).data.result;
    assert.equal(doc.chunk_count, 5);
    const original = await page.request.get(`${apiUrl}/documents/${doc.id}/pdf`);
    assert.equal(original.status(), 200);
    assert.match(original.headers()['content-type'], /application\/pdf/);
    assert.deepEqual(await original.body(), fs.readFileSync('examples/it-support-guide.pdf'));
    console.log(JSON.stringify({ document: doc.id, upload: upload.map(e => e.event) }));
    await page.waitForSelector('.chunk-row');
    const embeddingPromise = page.waitForResponse(r => r.url().endsWith('/embeddings/stream'), { timeout: 120000 });
    await page.getByRole('button', { name: 'Embed & store chunks' }).click();
    const embedding = await readStream(await embeddingPromise);
    assert.equal(embedding.at(-1).event, 'completed', JSON.stringify(embedding.at(-1)));
    assert.equal(embedding.at(-1).data.result.embedded_chunks, 5);
    await page.waitForFunction(() => document.querySelector('.embedding-progress strong')?.textContent === '5 chunks embedded and stored.');
    const before = await (await page.request.get(`${apiUrl}/documents/${doc.id}/chunks`)).json();
    const answerButton = page.getByRole('button', { name: 'Answer from document' });
    async function ask(question, mode = 'answer') {
      await page.getByLabel('Question', { exact: true }).fill(question);
      const pending = page.waitForResponse(r => r.url().endsWith(`/${mode}/stream`), { timeout: 120000 });
      await (mode === 'answer' ? answerButton : page.getByRole('button', { name: 'Find matching chunks' })).click();
      const response = await pending;
      const events = await readStream(response);
      assert.equal(events.at(-1).event, 'completed', JSON.stringify(events.at(-1)));
      await page.waitForFunction(() => document.querySelector('.retrieval-output')?.getAttribute('aria-busy') === 'false');
      const result = events.at(-1).data.result;
      console.log(JSON.stringify({ question, events: events.filter(e => e.event !== 'token').map(e => e.event), tokens: events.filter(e => e.event === 'token').length, answer: result.answer, citations: result.citations }));
      return { result, events };
    }
    for (const question of ['How do I connect to the VPN?', 'How do I reset my password?']) {
      const { result, events } = await ask(question);
      assert.equal(result.answer_status, 'answered');
      assert.ok(events.some(e => e.event === 'token'));
      assert.equal(await page.locator('.answer-text').innerText(), result.answer);
      for (let i = 0; i < result.citations.length; i++) {
        const citation = result.citations[i];
        const links = page.locator('.answer-citations li').nth(i);
        await links.getByRole('link', { name: 'View retrieved passage' }).click();
        assert.equal(await page.evaluate(() => document.activeElement.id), `evidence-${citation.chunk_id}`);
        const pdfLink = await links.getByRole('link', { name: `Open PDF page ${citation.page_number}` }).getAttribute('href');
        assert.equal(new URL(pdfLink, demoUrl).href, `${apiUrl}/documents/${doc.id}/pdf#page=${citation.page_number}`);
        await page.locator('.answer-citations button').nth(i).click();
        const content = result.context.find(c => c.chunk_id === result.citations[i].chunk_id).content;
        assert.equal(await page.locator('.page-text mark').innerText(), content);
        const permalink = await links.getByRole('link', { name: 'Permalink to source' }).getAttribute('href');
        const sourceTab = await browser.newPage();
        await sourceTab.goto(new URL(permalink, demoUrl).href);
        await sourceTab.locator('.page-text mark').waitFor();
        assert.equal(await sourceTab.locator('.page-text mark').innerText(), content);
        assert.equal(await sourceTab.locator('.trace-stage.complete').count(), 0);
        await sourceTab.close();
      }
    }
    const observations = await page.evaluate(() => window.traceObservations);
    assert.deepEqual(observations, { draft: true, running: true, evidenceWhileRunning: true });
    await page.locator('.retrieval-lab').screenshot({ path: '.verification/trace-desktop.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('.retrieval-lab').screenshot({ path: '.verification/trace-mobile.png' });
    const noAnswer = await ask('What is the capital of France?');
    assert.equal(noAnswer.result.answer_status, 'insufficient_evidence');
    assert.deepEqual(noAnswer.result.citations, []);
    await page.locator('.retrieval-filter summary').click();
    await page.getByLabel('Minimum cosine similarity').fill('1');
    const skipped = await ask('How do I connect to the VPN?');
    assert.equal(skipped.result.generation.performed, false);
    assert.ok(skipped.events.some(e => e.event === 'generation_skipped'));
    await page.getByLabel('Minimum cosine similarity').fill('-1');
    await page.getByLabel('Response', { exact: true }).selectOption('retrieve');
    await ask('How do I connect to the VPN?', 'retrieve');
    await page.getByLabel('Response', { exact: true }).selectOption('answer');
    const fixture = (name, seq, data = {}) => `event: ${name}\ndata: ${JSON.stringify({ event: name, sequence: seq, run_id: 'browser-fixture', elapsed_ms: seq, data })}\n\n`;
    await page.route('**/answer/stream', route => route.fulfill({ contentType: 'text/event-stream', body: fixture('generation_started', 1) + fixture('token', 2, { text: 'Unvalidated draft' }) + fixture('error', 3, { stage: 'generation', message: 'Fixture provider failure' }) }));
    await answerButton.click();
    await page.locator('.retrieval-form [role=alert]').filter({ hasText: 'Fixture provider failure' }).waitFor();
    assert.equal(await page.locator('.draft-answer').count(), 0);
    assert.equal(await page.locator('.retrieval-output .trace-stage.failed').count(), 1);
    await page.unroute('**/answer/stream');
    await page.route('**/answer/stream', async route => { await new Promise(r => setTimeout(r, 1500)); await route.fulfill({ contentType: 'text/event-stream', body: fixture('completed', 1, { result: { answer: 'stale' } }) }).catch(() => {}); });
    await answerButton.click();
    await page.getByRole('button', { name: 'Cancel request' }).click();
    await page.waitForTimeout(1700);
    assert.equal(await page.locator('.answer-text').count(), 0);
    assert.match(await page.getByRole('region', { name: 'Question execution' }).innerText(), /Cancelled/);
    await page.unroute('**/answer/stream');
    const after = await (await page.request.get(`${apiUrl}/documents/${doc.id}/chunks`)).json();
    assert.deepEqual(after, before);
    await page.reload();
    await page.waitForSelector('.chunk-row');
    assert.equal(await page.locator('.vector-preview').count() >= 5, true);
    assert.equal(await page.locator('.trace-stage.complete').count(), 0);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, observations, mobileOverflow: false, browserErrors: errors }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
