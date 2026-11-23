// humanq の画面 (server/humanq.html) の振る舞いテスト。headless chromium で実際のページを開く。
//
//   PLAYWRIGHT_PATH=<playwright の index.mjs> node tests/ui_test.mjs
//
// PLAYWRIGHT_PATH が無ければ `import("playwright")` を使う。サーバー (humanq.py) は 8770〜8779 の空いている
// ポートと一時の state dir で起動し、/api/items・/api/answer・/api/undo は page.route の台本で返す
// (遅延・失敗・データの変化を決定的に作るため。bd は要らない)。poll の setInterval は止めて、
// ページの load() を直接呼んで進める。HUMANQ_SERVER_DIR で別の server/ を試せる。
import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const SERVER_DIR = process.env.HUMANQ_SERVER_DIR || path.join(here, "..", "server");

async function loadPlaywright() {
  const p = process.env.PLAYWRIGHT_PATH;
  const mod = p ? await import(pathToFileURL(p).href) : await import("playwright");
  return mod.chromium ? mod : mod.default;
}

// ---- サーバーとブラウザ -------------------------------------------------------------

let browser, server, stateDir, baseUrl;

function portFree(port) {
  return new Promise(resolve => {
    const s = net.createServer();
    s.once("error", () => resolve(false));
    s.listen(port, "127.0.0.1", () => s.close(() => resolve(true)));
  });
}

async function startServer() {
  let port = 0;
  for (let p = 8770; p <= 8779; p++) if (await portFree(p)) { port = p; break; }
  assert.ok(port, "8770〜8779 に空きが無い");
  stateDir = mkdtempSync(path.join(os.tmpdir(), "humanq-ui-"));
  server = spawn("python3", [path.join(SERVER_DIR, "humanq.py"), "--state-dir", stateDir, "--port", String(port)],
    { stdio: ["ignore", "inherit", "inherit"] });
  baseUrl = `http://127.0.0.1:${port}/`;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(baseUrl + "api/ping")).ok) return; } catch { /* まだ */ }
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error("サーバーが起動しない");
}

before(async () => {
  await startServer();
  const pw = await loadPlaywright();
  browser = await pw.chromium.launch({ headless: true });
});

after(async () => {
  if (browser) await browser.close();
  if (server) server.kill("SIGTERM");
  if (stateDir) rmSync(stateDir, { recursive: true, force: true });
});

// ---- 台本 ---------------------------------------------------------------------------

const P1 = "/work/alpha", P2 = "/work/beta";
const item = (id, o = {}) => ({
  id, title: `title ${id}`, priority: 1, labels: [], created_at: "2026-11-24",
  description: `desc ${id}`, notes: "",
  q: { kind: "decision", ask: `ask ${id}`, context: "", recommended: "a",
       options: [{ key: "a", label: "案A", desc: "" }, { key: "b", label: "案B", desc: "" }] },
  qsig: "sig-1",                       // サーバーが付ける問いの署名。問いを変えるテストは changedQ で別の値にする
  ...o,
});
// エージェントが問いを書き直した (key a の意味が変わる)
const changedQ = it => ({ ...it, qsig: "sig-2", q: { ...it.q, ask: "NEW ASK",
  options: [{ key: "a", label: "まったく別の案", desc: "" }, { key: "b", label: "案B", desc: "" }] } });
const answeredItem = (id, answer = "[humanq] 回答: 案A (a)\n") => ({ ...item(id), answer });
const proj = (p, human = [], ans = [], ok = true) =>
  ({ path: p, name: p.split("/").pop(), ok, ...(ok ? {} : { error: "bd down" }), human, answered: ans });

class Script {
  constructor(projects) {
    this.projects = projects;
    this.itemsCount = 0;
    this.answers = [];
    this.undos = [];
    this.queue = [];                 // 次の /api/items に返す固定のデータ (先頭から使う)
    this.gates = {};
    this.releases = [];
    // 既定の回答の応答は、サーバーと同じく、見ていた問いの署名 (qsig) が今の問いと違えば 409 changed にする
    this.answerResult = body => {
      const cur = (this.project(body.project) || { human: [] }).human.find(i => i.id === body.id);
      if (cur && body.qsig !== undefined && body.qsig !== cur.qsig)
        return { status: 409, body: { error: "問いが書き換わっている", code: "changed", op: "answer" } };
      this.moveToAnswered(body);
      return { status: 200, body: { ok: true } };
    };
    this.undoResult = body => { this.moveToHuman(body); return { status: 200, body: { ok: true } }; };
  }
  project(p) { return this.projects.find(x => x.path === p); }
  moveToAnswered({ project, id }) {
    const p = this.project(project);
    p.human = p.human.filter(i => i.id !== id);
    p.answered.push(answeredItem(id));
  }
  moveToHuman({ project, id }) {
    const p = this.project(project);
    p.answered = p.answered.filter(i => i.id !== id);
    p.human.push(item(id));
  }
  snapshot() { return JSON.parse(JSON.stringify({ projects: this.projects })); }
  // kind (items / answer / undo) の応答を release() するまで止める
  hold(kind) {
    let resolve;
    const p = new Promise(r => { resolve = r; });
    const release = () => { if (this.gates[kind] === p) delete this.gates[kind]; resolve(); };
    this.gates[kind] = p;
    this.releases.push(release);
    return release;
  }
  releaseAll() { for (const r of this.releases) r(); }
}

async function withPage(script, fn, ctxOptions = {}, setup = null) {
  const ctx = await browser.newContext(ctxOptions);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  await page.addInitScript(() => {
    const si = window.setInterval;
    window.setInterval = (f, ms, ...a) => (ms === 15000 ? 0 : si(f, ms, ...a));   // poll は止める
  });
  // r.raw があれば、JSON にしないでそのまま返す (壊れた応答の再現)。r.abort なら、応答を返さず通信を切る
  const json = (route, status, body, raw) =>
    route.fulfill({ status, contentType: raw !== undefined ? "text/html" : "application/json", body: raw !== undefined ? raw : JSON.stringify(body) });
  const reply = (route, r) => r.abort ? route.abort("failed") : json(route, r.status, r.body, r.raw);
  await page.route("**/api/items", async route => {
    script.itemsCount++;
    const snap = script.queue.length ? script.queue.shift() : script.snapshot();   // 要求を受けた時点のデータ
    if (script.gates.items) await script.gates.items;
    await json(route, 200, snap);
  });
  await page.route("**/api/answer", async route => {
    const body = route.request().postDataJSON();
    script.answers.push(body);
    if (script.gates.answer) await script.gates.answer;
    await reply(route, script.answerResult(body));
  });
  await page.route("**/api/undo", async route => {
    const body = route.request().postDataJSON();
    script.undos.push(body);
    if (script.gates.undo) await script.gates.undo;
    await reply(route, script.undoResult(body));
  });
  try {
    if (setup) await setup(page);
    await page.goto(baseUrl);
    await ready(page);
    await fn(page, ctx);
    assert.deepEqual(errors, [], "ページで例外が出た");
  } finally {
    script.releaseAll();
    await ctx.close();
  }
}

const ready = page => page.waitForFunction(() => /更新|保留/.test(document.getElementById("meta").textContent));
const qcard = (page, id) => page.locator(`section.card:has(textarea):has(.id:text-is("${id}"))`);
const acard = (page, id) => page.locator(`section.card:has(.ans):has(.id:text-is("${id}"))`);

async function waitFor(fn, ms = 4000, what = "条件") {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { if (await fn()) return; } catch { /* 要素がまだ無い */ }
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error(`待っても満たされない: ${what}`);
}

async function answerFirst(page, id) {
  await qcard(page, id).locator(".opt").first().click();
  await qcard(page, id).locator("button.primary").click();
}

// 押している最中 (mouse down → 待つ → up) を再現する。ボタンの位置は押す直前に測る
async function press(page, locator, ms = 60) {
  const box = await locator.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(ms);
  await page.mouse.up();
}

// 古い要素が畳み終わって外れ、そのカードが 1 枚だけになるまで待つ
const single = (page, id) => waitFor(async () => (await qcard(page, id).count()) === 1, 3000, `${id} のカードが 1 枚`);

// B の選択肢を選び、補足欄に書いて保留にする (B の中身を s.projects[0].human[i] で変えて load すると、B は作り直されず保留になる)
async function typeInto(page, id, text = "memo") {
  await qcard(page, id).locator(".opt").first().click();
  const ta = qcard(page, id).locator("textarea");
  await ta.click();
  await ta.pressSequentially(text);
  return ta;
}

// ---- 1. クリックの取りこぼし -------------------------------------------------------------

// A を送信中に B の補足欄へ書き始める。A の POST が返って読み込みが入力中に届き、そのまま B の「回答する」を押す
async function typingInBWhileANotified(page, s) {
  const release = s.hold("answer");
  await answerFirst(page, "t-A1");
  const ta = qcard(page, "t-B2").locator("textarea");
  await ta.click();
  await ta.pressSequentially("memo");
  release();
  await waitFor(() => s.itemsCount >= 2, 4000, "A の成功後の読み込み");
  await page.waitForTimeout(450);     // A が畳み終わって B の位置が落ち着くまで
}

test("入力中にデータが変わっても、押している最中の「回答する」(mouse down → 1 tick → up) が効く", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typingInBWhileANotified(page, s);
    const box = await qcard(page, "t-B2").locator("button.primary").boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.waitForTimeout(60);    // focusout の setTimeout 0 が走る
    await page.mouse.up();
    await waitFor(() => s.answers.length === 2, 3000, "B の回答の POST");
    assert.deepEqual(s.answers.map(a => a.id), ["t-A1", "t-B2"]);
    assert.equal(s.answers[1].text, "memo");
  });
});

test("入力中にデータが変わっても、Tab → Enter で「回答する」が効く", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typingInBWhileANotified(page, s);
    await page.keyboard.press("Tab");
    await page.keyboard.press("Enter");
    await waitFor(() => s.answers.length === 2, 3000, "B の回答の POST");
    assert.equal(s.answers[1].id, "t-B2");
  });
});

// ---- 2. 消えたまま戻らない -----------------------------------------------------------------

test("回答したカードが、別の入力中に判断待ちへ戻ったデータで出直す", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    const release = s.hold("answer");
    await answerFirst(page, "t-A1");
    const ta = qcard(page, "t-B2").locator("textarea");
    await ta.click();
    await ta.pressSequentially("x");
    release();
    await waitFor(() => s.itemsCount >= 2, 4000, "A の成功後の読み込み");
    s.projects[0].answered = [];                                  // エージェントが問い直した
    s.projects[0].human = [item("t-A1"), item("t-B2")];
    await page.evaluate(() => load());
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "A が判断待ちに出る");
    assert.equal(await ta.inputValue(), "x");
    assert.ok(await ta.evaluate(e => e === document.activeElement), "B の入力のフォーカスが保たれる");
  });
});

test("A が判断待ちと回答済みの両方に入ったデータを挟んでも、A は判断待ちに出直す", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    const release = s.hold("answer");
    await answerFirst(page, "t-A1");
    const ta = qcard(page, "t-B2").locator("textarea");
    await ta.click();
    await ta.pressSequentially("x");
    // 書き込みの途中を読んだデータ: A が両方に居る (成功後の最初の読み込みに返る)
    s.queue.push({ projects: [proj(P1, [item("t-A1"), item("t-B2")], [answeredItem("t-A1")])] });
    release();
    await waitFor(() => s.itemsCount >= 2, 4000, "A の成功後の読み込み");
    s.projects[0].answered = [];
    s.projects[0].human = [item("t-A1"), item("t-B2")];
    await page.evaluate(() => load());
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "A が判断待ちに出る");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 0, 3000, "回答済みの A は消える");
  });
});

// ---- 3. 捨てられた読み直し -------------------------------------------------------------------

test("poll の最中に POST が先に返っても、その後に新しい読み直しが走る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    const releaseItems = s.hold("items");
    await page.evaluate(() => { window.__poll = load(); });     // poll が出ていて、応答が止まっている
    await waitFor(() => s.itemsCount === 2, 3000, "poll の要求");
    await answerFirst(page, "t-A1");                             // POST は先に成功して返る
    await waitFor(() => s.answers.length === 1, 3000, "POST");
    await page.waitForTimeout(150);
    releaseItems();
    const releaseNext = s.hold("items");                         // POST の後に始まる読み直しは止めておく
    await page.evaluate(() => window.__poll);                    // 古い poll の応答 (A がまだ判断待ちにいる) が届いた
    await page.waitForTimeout(100);
    // 古い応答のデータでは、POST が成功した A は判断待ちに戻らない (次の読み直しを待つあいだも隠れたまま)
    // (畳んでいる最中の A は DOM に残っていて、.leaving が付いている。戻ってきた A は .leaving が外れる)
    assert.equal(await page.locator('section.card:not(.leaving):has(.id:text-is("t-A1"))').count(), 0, "古い poll の応答で A が判断待ちに戻った");
    assert.equal((await page.locator("#count").textContent()).trim(), "1 件");
    await waitFor(() => s.itemsCount === 3, 3000, "POST の後に始まる読み直し");
    releaseNext();
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに A が出る");
  });
});

// ---- 4. 畳んでいる途中の失敗 -----------------------------------------------------------------

test("畳んでいる途中で送信が失敗したら、同じカードが戻って .failed が付き、読み直しが走る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
  await withPage(s, async page => {
    const handle = await qcard(page, "t-A1").elementHandle();
    const before = s.itemsCount;
    await answerFirst(page, "t-A1");
    await waitFor(() => s.itemsCount > before, 3000, "失敗の後の読み直し");
    await waitFor(() => handle.evaluate(e => e.isConnected && !e.classList.contains("leaving") && e.classList.contains("failed")),
      3000, "畳みかけのカードが .failed で戻る");
    assert.match(await handle.evaluate(e => e.textContent), /bd boom/);
    // 戻り切ったら、動きの指定は残らず、押せる
    await waitFor(() => handle.evaluate(e => e.style.height === "" && !e.classList.contains("anim") && !e.inert && e.offsetHeight > 100),
      3000, "戻り終わったカードの状態");
    // 送り直せる
    s.answerResult = body => { s.moveToAnswered(body); return { status: 200, body: { ok: true } }; };
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(() => s.answers.length === 2, 3000, "送り直しの POST");
  });
});

test("1 件しかないプロジェクトで失敗しても、見出しの入れ物ごと元に戻る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await page.locator("section.card.failed").count()) === 1, 3000, "カードが戻る");
    await page.waitForTimeout(500);
    assert.equal(await page.locator("h3").count(), 1);
    assert.equal(await page.locator("h2").count(), 1);
    assert.equal(await page.locator(".empty").count(), 0);
    assert.equal(await page.locator(".leaving, .anim").count(), 0, "畳みの状態が残っている");
    assert.equal((await page.locator("#count").textContent()).trim(), "1 件");
  });
});

test("ほかで回答済み (400 gone) のときは、カードを戻さず、読み直して今の状態を出す", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = body => { s.moveToAnswered(body); return { status: 400, body: { error: "このチケットは判断待ちに無い", code: "gone" } }; };
  await withPage(s, async page => {
    const before = s.itemsCount;
    await answerFirst(page, "t-A1");
    await waitFor(() => s.itemsCount > before, 3000, "読み直し");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "今の状態 (回答済み) が出る");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 0, 2000, "判断待ちには戻さない (畳み終わって消える)");
    assert.match(await page.locator(".toast").first().innerText(), /ほかで回答・反映済み/);
  });
});

test("ラベルの付け替えだけ失敗 (回答は記録済み) のときは、回答済みに出して「記録済み」と知らせる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = body => { s.moveToAnswered(body); return { status: 502, body: { error: "label remove boom", stage: "label", written: true } }; };
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 0, 2000, "判断待ちには戻さない (畳み終わって消える)");
    assert.match(await page.locator(".toast").first().innerText(), /記録済み/);
  });
});

test("2xx でも JSON でない応答は失敗として扱い、書き込まれていなければカードを戻す", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 200, raw: "<html>not json</html>" });
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator("textarea").fill("memo");
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(async () => (await page.locator("section.card.failed").count()) === 1, 3000, "カードが戻る");
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "memo");
  });
});

test("想定外の形のデータは使わず、前のデータのまま送信も効く", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    s.queue.push({ projects: null });
    await page.evaluate(() => load());
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    await answerFirst(page, "t-A1");
    await waitFor(() => s.answers.length === 1, 3000, "POST");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
  });
});

test("トーストは閉じるボタンで消せる", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    await page.locator(".toast .toast-x").first().click();
    await waitFor(async () => (await page.locator(".toast").count()) === 0, 2000, "トーストが消える");
  });
});

test("失敗のトーストは要約だけで閉じるボタンがあり、全文はカードの中にある", async () => {
  const long = "x".repeat(300) + " " + "/very/long/path/".repeat(30);
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 502, body: { error: long } });
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    const t = await page.locator(".toast").first().innerText();
    assert.ok(t.length <= 240, `トーストが長い (${t.length} 文字)`);
    assert.ok(await page.locator(".toast button").count() >= 1, "トーストに閉じるボタンが無い");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "カードが戻る");
    assert.ok((await qcard(page, "t-A1").locator(".errbox pre").textContent()).includes(long), "エラーの全文がカードに無い");
  });
});

test("reduced-motion でも、失敗で戻ったカードの目印 (.failed) は残る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await page.locator("section.card.failed").count()) === 1, 3000, ".failed のカード");
    assert.equal(await page.locator("section.card.flash").count(), 0, "動きの flash は付けない");
  }, { reducedMotion: "reduce" });
});

test("トーストの表示時間は、タブが見えている間だけ数える", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
  const setHidden = (page, v) => page.evaluate(h => Object.defineProperty(document, "hidden", { configurable: true, get: () => h }), v);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    await setHidden(page, true);
    await page.clock.runFor(10000);                    // 裏のタブで 10 秒 (表示時間の 6 秒より長い)
    assert.equal(await page.locator(".toast").count(), 1, "見えていない間にトーストが消えた");
    await setHidden(page, false);
    await page.clock.runFor(7000);
    await waitFor(async () => (await page.locator(".toast").count()) === 0, 3000, "見えている間に 6 秒経てば消える");
  }, {}, page => page.clock.install({ time: 0 }));
});

// ---- 5. 件数 ---------------------------------------------------------------------------------

test("押した直後に h2 の件数・空の見出し・「判断待ちはありません」が更新される", async () => {
  const s = new Script([proj(P1, [item("t-A1")]), proj(P2, [item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    s.hold("answer");                                            // POST はずっと返らない
    const h2s = () => page.$$eval("h2", es => es.map(e => e.textContent));
    assert.deepEqual(await h2s(), ["判断 (1)", "判断 (2)"]);
    await answerFirst(page, "t-B2");
    await waitFor(async () => JSON.stringify(await h2s()) === JSON.stringify(["判断 (1)", "判断 (1)"]), 3000, "beta の件数が 1");
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await page.$$eval("h3", es => es.map(e => e.textContent))).length === 1, 3000, "alpha の見出しが消える");
    await answerFirst(page, "t-C3");
    await waitFor(async () => (await page.locator(".empty").count()) === 1 &&
      /判断待ちはありません/.test(await page.locator(".empty").textContent()), 3000, "空の文言");
    // 空の見出しは、中のカードが畳み終わったら外れる
    await waitFor(async () => (await page.locator("h2").count()) === 0 && (await page.locator("h3").count()) === 0, 3000, "見出しが全部外れる");
    assert.equal((await page.locator("#count").textContent()).trim(), "0 件");
  });
});

// ---- 6. サーバーの入れ替え ----------------------------------------------------------------------

test("403 token で再読み込みの案内が出て、下書きが再読み込みの後も残る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 403, body: { error: "token" } });
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    await qcard(page, "t-A1").locator("textarea").fill("keep me");
    await qcard(page, "t-A1").locator("button.primary").click();
    await page.getByRole("button", { name: /再読み込み/ }).waitFor({ timeout: 3000 });
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "カードが戻る");
    s.answerResult = body => { s.moveToAnswered(body); return { status: 200, body: { ok: true } }; };
    await page.reload();
    await ready(page);
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "keep me");
    assert.ok(await qcard(page, "t-A1").locator(".opt.sel").count() === 1, "選んだ選択肢も戻る");
  });
});

// ---- 既存の動き (壊れていないこと) ------------------------------------------------------------------

test("回答は選択肢と補足を送り、カードは消えて回答済みに出る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    await qcard(page, "t-A1").locator("textarea").fill("memo");
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(() => s.answers.length === 1, 3000, "POST");
    assert.deepEqual(s.answers[0], { project: P1, id: "t-A1", option: "b", text: "memo", qsig: "sig-1" });   // qsig は見ていた問いの署名
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 0, 3000, "カードが消える");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
  });
});

test("取り消しは判断待ちに戻す", async () => {
  const s = new Script([proj(P1, [item("t-B2")], [answeredItem("t-A1")])]);
  await withPage(s, async page => {
    await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
    await waitFor(() => s.undos.length === 1, 3000, "POST");
    assert.deepEqual(s.undos[0], { project: P1, id: "t-A1" });
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに出る");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 0, 3000, "回答済みから消える");
  });
});

test("データが変わっても、開いた本文は開いたまま", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-B2").locator("summary").click();
    s.projects[0].human[0].title = "changed title";
    await page.evaluate(() => load());
    await waitFor(async () => /changed title/.test(await qcard(page, "t-A1").textContent()), 3000, "A の題が変わる");
    assert.equal(await qcard(page, "t-B2").locator("details").evaluate(e => e.open), true);
  });
});

test("新しいカードは高さを 0 から広げて入り、最初の描画では動かさない", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    assert.equal(await page.locator("section.card.anim").count(), 0, "最初の描画で動いている");
    s.projects[0].human.push(item("t-C3"));
    const r = await page.evaluate(async () => {
      await load();
      const c = [...document.querySelectorAll("section.card")].find(e => e.querySelector(".id").textContent === "t-C3");
      return { height: c.style.height, anim: c.classList.contains("anim") };
    });
    assert.ok(r.anim && r.height, "入場の動き (高さの遷移) が付いていない");
    await waitFor(() => qcard(page, "t-C3").evaluate(e => e.style.height === "" && !e.classList.contains("anim")), 3000, "動きの後始末");
  });
});

test("reduced-motion では新しいカードを動かさず、消えるときは即座に外す", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    s.projects[0].human.push(item("t-C3"));
    const anim = await page.evaluate(async () => {
      await load();
      return document.querySelectorAll("section.card.anim").length;
    });
    assert.equal(anim, 0);
  }, { reducedMotion: "reduce" });
});

// ---- 描き直しの突き合わせ -------------------------------------------------------------------

test("別のカードの入力中でも、ほかのカードは更新され、入力のフォーカスと値は保たれる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    const ta = qcard(page, "t-B2").locator("textarea");
    await ta.click();
    await ta.pressSequentially("abc");
    const handle = await ta.elementHandle();
    s.projects[0].human[0].title = "new title";
    await page.evaluate(() => load());
    await waitFor(async () => /new title/.test(await qcard(page, "t-A1").textContent()), 3000, "A の題が更新される");
    assert.ok(await handle.evaluate(e => e.isConnected && e === document.activeElement && e.value === "abc"));
  });
});

test("フォーカスを持つカード自身の中身が変わったら、そのカードだけ後回しにして、離れたあとの読み込みで作り直す", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-B2").locator(".opt").first().click();
    const ta = qcard(page, "t-B2").locator("textarea");
    await ta.click();
    await ta.pressSequentially("abc");
    s.projects[0].human[1].description = "NEW BODY";
    await page.evaluate(() => load());
    assert.ok(!/NEW BODY/.test(await qcard(page, "t-B2").textContent()), "入力中のカードは作り直さない");
    await page.locator("h1").click();                         // フォーカスを外す
    await page.evaluate(() => load());
    await waitFor(async () => /NEW BODY/.test(await qcard(page, "t-B2").textContent()), 3000, "作り直される");
    assert.equal(await qcard(page, "t-B2").locator("textarea").inputValue(), "abc");
    assert.equal(await qcard(page, "t-B2").locator(".opt.sel").count(), 1, "選択が下書きから戻る");
  });
});

test("畳むカードがフォーカスを持っていたら、次のカードそのもの (中のボタンではなく) にフォーカスが移る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    s.hold("answer");
    await answerFirst(page, "t-A1");
    await waitFor(() => page.evaluate(() => {
      const a = document.activeElement;
      return !!a && a.matches("section.card[tabindex='-1']") && a.querySelector(".id").textContent === "t-B2";
    }), 3000, "B のカード自身にフォーカス");
  });
});

test("「回答する」を Enter で 2 回押しても、次の回答済みカードの取り消しは押されない", async () => {
  const s = new Script([proj(P1, [item("t-B2")], [answeredItem("t-A1")])]);
  s.hold("answer");
  await withPage(s, async page => {
    await qcard(page, "t-B2").locator(".opt").first().click();
    await qcard(page, "t-B2").locator("button.primary").focus();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(50);
    await page.keyboard.press("Enter");                         // キーのリピートや押し直し
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers.map(a => a.id), ["t-B2"]);
    assert.deepEqual(s.undos, [], "別のチケットの取り消しが送られた");
  });
});

test("「回答する」を Enter で 2 回押しても、次の判断待ちカードの選択肢は選ばれない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.hold("answer");
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").first().click();
    await qcard(page, "t-A1").locator("button.primary").focus();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(50);
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);
    assert.equal(await qcard(page, "t-B2").locator(".opt.sel").count(), 0, "次のカードの選択肢が選ばれた");
    assert.ok(!/t-B2/.test(await page.evaluate(() => sessionStorage.getItem("humanq-drafts")) || ""), "次のカードの下書きができた");
  });
});

test("読めなかったプロジェクト (ok:false) では、カードを入れ替えず、読めるようになっても入場し直さない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    const handle = await qcard(page, "t-A1").elementHandle();
    s.queue.push({ projects: [proj(P1, [], [], false)] });
    await page.evaluate(() => load());
    await waitFor(async () => /bd を読めない/.test(await page.locator("main").textContent()), 3000, "バナー");
    assert.ok(await handle.evaluate(e => e.isConnected && !e.classList.contains("leaving")), "ok:false でカードが消えた");
    await page.evaluate(() => load());
    await waitFor(async () => !/bd を読めない/.test(await page.locator("main").textContent()), 3000, "バナーが消える");
    assert.ok(await handle.evaluate(e => e.isConnected && !e.classList.contains("anim")), "同じカードが残る");
  });
});

test("送信中のカードは、データに居なくても居ても、書き込みが終わるまで隠れたまま", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    const release = s.hold("answer");
    await answerFirst(page, "t-A1");
    s.queue.push({ projects: [proj(P1, [item("t-B2")])] });                    // 書き込みの途中: A がどこにも無い
    await page.evaluate(() => load());
    s.queue.push({ projects: [proj(P1, [item("t-A1"), item("t-B2")])] });      // まだ反映されていないデータ
    await page.evaluate(() => load());
    await page.waitForTimeout(500);
    assert.equal(await qcard(page, "t-A1").count(), 0, "送信中の A が戻ってきた");
    release();
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "成功後は回答済みに出る");
  });
});

// ---- 0.1.5: 押している最中の描き直し・問いの署名・失敗の知らせ・下書き ---------------------------------------

test("入力中に保留になったカードの下のカードを押している最中に、上のカードが作り直されても、click が消えない", async () => {
  // B の補足欄に入力している間に B の背景が長くなって保留になる。入力欄からフォーカスが外れたとき B が作り直されて高くなり、
  // その下の C が押している最中に動くと、離した位置にボタンが無く click が消える (スクロールしていない背の高い画面)
  const s = new Script([proj(P1, [item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "abc");
    const b = s.projects[0].human[0];
    s.projects[0].human[0] = { ...changedQ(b), q: { ...changedQ(b).q, context: Array.from({ length: 8 }, (_, i) => `line${i + 1}`).join("\n") } };
    await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => staleCount), 1, "B が保留になっていない");
    await qcard(page, "t-C3").locator(".opt").first().evaluate(e => e.click());     // C に選択を入れる (フォーカスは B の入力欄のまま)
    await press(page, qcard(page, "t-C3").locator("button.primary"));
    await waitFor(() => s.answers.length === 1, 3000, "C の回答の POST");
    assert.deepEqual(s.answers.map(a => a.id), ["t-C3"]);
    // 離したあとには、後回しにしていた B が作り直される
    await waitFor(async () => /line8/.test(await qcard(page, "t-B2").textContent()), 3000, "押したあとに B が作り直される");
  }, { viewport: { width: 1280, height: 1600 } });
});

test("保留中のカード (問い以外が変わっただけ) 自身の「回答する」を押している最中に作り直されず、click が届いて送られる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "memo");
    s.projects[0].human[1].description = "NEW BODY";                         // 問いは変わらない
    await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => staleCount), 1, "B が保留になっていない");
    await press(page, qcard(page, "t-B2").locator("button.primary"));
    await waitFor(() => s.answers.length === 1, 3000, "B の回答の POST");
    assert.equal(s.answers[0].id, "t-B2");
    assert.equal(s.answers[0].text, "memo");
  });
});

test("保留中のカード (問い以外が変わっただけ) の「回答する」へ Tab で移して Enter を押しても、作り直されずに送られる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "memo");
    s.projects[0].human[1].description = "NEW BODY";
    await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => staleCount), 1, "B が保留になっていない");
    await page.keyboard.press("Tab");                                         // 入力欄から同じカードの「回答する」へ
    await page.waitForTimeout(60);                                            // focusout の setTimeout 0 が走る (人が Enter を押すまでの間)
    await page.keyboard.press("Enter");
    await waitFor(() => s.answers.length === 1, 3000, "B の回答の POST");
    assert.equal(s.answers[0].id, "t-B2");
  });
});

test("入力中で保留のカードは、別のカードのボタンへフォーカスが移ると、読み込みを待たずに作り直される", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "abc");
    s.projects[0].human[1].description = "NEW BODY";
    await page.evaluate(() => load());
    assert.ok(!/NEW BODY/.test(await qcard(page, "t-B2").textContent()), "入力中のカードは作り直さない");
    const loads = s.itemsCount;
    await qcard(page, "t-A1").locator(".opt").first().focus();                // フォーカスの移り先がある (relatedTarget)
    await waitFor(async () => /NEW BODY/.test(await qcard(page, "t-B2").textContent()), 3000, "作り直される");
    assert.equal(s.itemsCount, loads, "読み込みで直ったのではない");
    assert.equal(await qcard(page, "t-B2").locator("textarea").inputValue(), "abc");
  });
});

test("問いが変わったのに古い表示のまま送った回答は書かれず (409)、新しい問いでカードが作り直されて、選択だけ消える", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-B2").locator(".opt").first().click();
    await qcard(page, "t-B2").locator("textarea").fill("memo");
    s.projects[0].human[1] = changedQ(s.projects[0].human[1]);               // エージェントが問いを書き直した。画面はまだ読み直していない
    await qcard(page, "t-B2").locator("button.primary").click();
    await waitFor(() => s.answers.length === 1, 3000, "POST");
    assert.equal(s.answers[0].qsig, "sig-1", "見ていた問いの署名を送る");
    await waitFor(async () => /NEW ASK/.test(await qcard(page, "t-B2").textContent()), 3000, "新しい問いで作り直される");
    await single(page, "t-B2");
    assert.equal(await qcard(page, "t-B2").locator(".opt.sel").count(), 0, "選んだ選択肢が残っている");
    assert.equal(await qcard(page, "t-B2").locator("textarea").inputValue(), "memo", "補足は残る");
    assert.match(await qcard(page, "t-B2").locator(".errbox").innerText(), /問いが更新されました/);
    assert.match(await page.locator(".toast").first().innerText(), /問いが更新されました。見直してから送ってください/);
    assert.equal(await acard(page, "t-B2").count(), 0, "回答済みに入った");
  });
});

test("入力中で保留のカードの問いが変わっていたら、「回答する」は送らずに作り直して見直してもらう", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "memo");
    s.projects[0].human[1] = changedQ(s.projects[0].human[1]);
    await page.evaluate(() => load());
    assert.ok(!/NEW ASK/.test(await qcard(page, "t-B2").textContent()), "入力中のカードは作り直さない");
    await press(page, qcard(page, "t-B2").locator("button.primary"));         // 押している最中に作り直されず、click が届く
    await waitFor(async () => /NEW ASK/.test(await qcard(page, "t-B2").textContent()), 3000, "新しい問いで作り直される");
    await single(page, "t-B2");
    assert.equal(s.answers.length, 0, "古い問いのまま回答が送られた");
    assert.equal(await qcard(page, "t-B2").locator(".opt.sel").count(), 0, "選んだ選択肢が残っている");
    assert.equal(await qcard(page, "t-B2").locator("textarea").inputValue(), "memo");
    assert.match(await page.locator(".toast").first().innerText(), /問いが更新されました。見直してから送ってください/);
    assert.ok(await page.evaluate(() => document.activeElement.matches("section.card")), "押したボタンが外れたので、フォーカスは新しいカードへ");
  });
});

test("書き込めなかった (written:false) 失敗のあと、チケットがどこにも無くても「反映されていました」とは言わない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = body => {                         // 書き込みは失敗し、同じ頃にエージェントが human を外した
    const p = s.project(body.project);
    p.human = p.human.filter(i => i.id !== body.id);
    return { status: 502, body: { error: "database is locked", stage: "comment", written: false, op: "answer" } };
  };
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    const t = await page.locator(".toast").first().innerText();
    assert.match(t, /書き込めませんでした。チケットはもう判断待ちにありません/);
    assert.ok(!/反映されていました/.test(t), t);
    await waitFor(async () => (await page.locator("section.card").count()) === 1, 3000, "A のカードは畳んで外れる");
    assert.ok(!/t-A1/.test(await page.evaluate(() => sessionStorage.getItem("humanq-drafts")) || ""), "下書きが残っている");
  });
});

test("通信が切れて書けたか分からず、チケットがどこにも無いときは、分からないと知らせる。行き先に居れば反映されていた", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = body => { const p = s.project(body.project); p.human = p.human.filter(i => i.id !== body.id); return { abort: true }; };
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    const t = await page.locator(".toast").first().innerText();
    assert.match(t, /書けたかどうか分かりません/);
    assert.ok(!/反映されていました/.test(t), t);
  });
  const s2 = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s2.answerResult = body => { s2.moveToAnswered(body); return { abort: true }; };            // 書けていたが応答が届かなかった
  await withPage(s2, async page => {
    await answerFirst(page, "t-A1");
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    assert.match(await page.locator(".toast").first().innerText(), /反映されていました/);
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
  });
});

test("ほかで回答済み (gone) の後に問い直されたカードは、前の選択と補足が入らずに出る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = body => { s.moveToAnswered(body); return { status: 400, body: { error: "gone", code: "gone", op: "answer" } }; };
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    await qcard(page, "t-A1").locator("textarea").fill("old memo");
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 0, 3000, "畳み終わる");
    s.projects[0].answered = [];                                                // エージェントが問い直した
    s.projects[0].human = [item("t-A1"), item("t-B2")];
    await page.evaluate(() => load());
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "問い直しが判断待ちに出る");
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "", "前の補足が残っている");
    assert.equal(await qcard(page, "t-A1").locator(".opt.sel").count(), 0, "前の選択が残っている");
    assert.ok(!/t-A1/.test(await page.evaluate(() => sessionStorage.getItem("humanq-drafts")) || ""), "下書きが保存に残っている");
  });
});

test("成功の直後 (畳み終わる前) に同じ問いが戻っても、古い要素は戻らず、入力は下書きに保存される", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    await qcard(page, "t-A1").locator("textarea").fill("first");
    const old = await qcard(page, "t-A1").elementHandle();
    await qcard(page, "t-A1").locator("button.primary").click();
    s.queue.push({ projects: [proj(P1, [item("t-A1"), item("t-B2")])] });        // 成功後の最初の読み込みで、もう問い直されている
    await waitFor(() => s.answers.length === 1, 3000, "POST");
    await waitFor(async () => (await page.locator("section.card").count()) >= 2, 3000, "問い直しが出る");
    await page.waitForTimeout(600);                                              // 古い要素が畳み終わる
    assert.equal(await old.evaluate(e => e.isConnected), false, "畳んだ古い要素が戻った");
    await single(page, "t-A1");
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "");
    assert.equal(await qcard(page, "t-A1").locator(".opt.sel").count(), 0);
    await qcard(page, "t-A1").locator("textarea").fill("second");
    assert.match(await page.evaluate(() => sessionStorage.getItem("humanq-drafts")), /second/, "入力が保存されない");
  });
});

test("取り消しのラベルの付け替えだけ失敗したときは、「取り消しは記録済み」と知らせる (回答とは言わない)", async () => {
  const s = new Script([proj(P1, [item("t-B2")], [answeredItem("t-A1")])]);
  s.undoResult = () => ({ status: 502, body: { error: "label add boom", stage: "label", written: true, op: "undo" } });
  await withPage(s, async page => {
    await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
    await waitFor(async () => (await page.locator("section.card.failed").count()) === 1, 3000, "カードが戻る");
    const t = await page.locator(".toast").first().innerText();
    assert.match(t, /取り消しは記録済みですが、ラベルの付け替えだけ失敗しました/);
    assert.ok(!/回答は記録済み/.test(t), t);
    assert.match(await page.locator("section.card.failed .errbox").innerText(), /取り消しは記録済みですが/);
  });
  // 読み直したら判断待ちに移っていたとき
  const s2 = new Script([proj(P1, [item("t-B2")], [answeredItem("t-A1")])]);
  s2.undoResult = body => { s2.moveToHuman(body); return { status: 502, body: { error: "label remove boom", stage: "label", written: true, op: "undo" } }; };
  await withPage(s2, async page => {
    await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    const t = await page.locator(".toast").first().innerText();
    assert.match(t, /取り消しは記録済みです/);
    assert.match(t, /判断待ちの一覧/);
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに出る");
  });
});
