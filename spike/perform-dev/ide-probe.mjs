// Spike 92: drive code-server headlessly through a given base URL.
// Usage: node ide-probe.mjs <baseUrl> <label> [edit]
import { createRequire } from 'node:module';
const require = createRequire('/home/ubuntu/.claude/skills/gstack/node_modules/');
const { chromium } = require('playwright-core');
const [base, label, doEdit] = process.argv.slice(2);
const exe = '/home/ubuntu/.cache/ms-playwright/chromium-1234/chrome-linux/chrome';
const browser = await chromium.launch({ executablePath: exe, headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
const ws = []; const fails = [];
page.on('websocket', w => {
  const rec = { url: w.url().replace(/[?].*/, ''), open: true, frames: 0, closed: false, err: null };
  ws.push(rec);
  w.on('framereceived', () => rec.frames++);
  w.on('socketerror', e => rec.err = String(e));
  w.on('close', () => rec.closed = true);
});
page.on('requestfailed', r => fails.push(`${r.failure()?.errorText} ${r.url().slice(0, 90)}`));
const t0 = Date.now();
const resp = await page.goto(`${base}/?folder=/workspaces/demo-astroshop-problems`, { waitUntil: 'domcontentloaded', timeout: 30000 });
let workbench = false, explorer = false, tShell = null, tExplorer = null;
try { await page.waitForSelector('.monaco-workbench', { timeout: 30000 }); workbench = true; tShell = Date.now() - t0; } catch {}
try { await page.waitForSelector('.explorer-folders-view .monaco-list-row', { timeout: 30000 }); explorer = true; tExplorer = Date.now() - t0; } catch {}
await page.waitForTimeout(3000);
out0: { const m = await page.locator('#monaco-dialog-message-body').innerText().catch(()=>null); globalThis.__dialog = m; }
const bodyText = (await page.locator('body').innerText().catch(() => '')).slice(0, 4000);
const out = { label, httpStatus: resp?.status(), workbench, explorer, tShellMs: tShell, tExplorerMs: tExplorer, websockets: ws, requestFailures: fails.slice(0, 8),
  dialog: globalThis.__dialog,
  reconnectMsg: /reconnect|Could not connect|unable to connect|disconnected/i.test(bodyText) ? bodyText.match(/.{0,80}(reconnect|Could not connect|unable to connect|disconnected).{0,80}/i)?.[0] : null };
if (doEdit && explorer) {
  const mark = `spike92-edit-${Date.now()}`;
  await page.keyboard.press('Control+P'); await page.waitForTimeout(800);
  await page.keyboard.type('mkdocs.yaml'); await page.waitForTimeout(1200); await page.keyboard.press('Enter');
  await page.waitForSelector('.monaco-editor .view-lines', { timeout: 15000 });
  await page.waitForTimeout(2500);
  await page.locator('.monaco-editor .view-lines').first().click();
  await page.keyboard.press('Control+Home'); await page.keyboard.type(`# ${mark}\n`);
  await page.waitForTimeout(500);
  await page.keyboard.press('Control+S'); await page.waitForTimeout(2500);
  out.dirtyAfterSave = await page.locator('.tab.dirty').count();
  out.editMark = mark;
  // integrated terminal
  await page.keyboard.press('Control+Backquote');
  await page.waitForSelector('.terminal-wrapper, .xterm', { state: 'attached', timeout: 20000 });
  await page.waitForTimeout(6000);
  await page.keyboard.type(`kubectl get pods -A > /tmp/ide-term-${mark}.txt 2>&1; echo KUBECONFIG=$KUBECONFIG SHELL=$0 >> /tmp/ide-term-${mark}.txt; echo TERM-DONE-${mark}\n`, { delay: 15 });
  await page.waitForTimeout(6000);
  out.termRendered = (await page.locator('.xterm').count()) > 0;
  await page.screenshot({ path: `$SCRATCH/spike/ide-${label}.png` });
} else {
  await page.screenshot({ path: `$SCRATCH/spike/ide-${label}.png` });
}
console.log(JSON.stringify(out, null, 1));
await browser.close();
