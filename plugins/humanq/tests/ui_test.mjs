// humanq の画面 (server/humanq.html) の振る舞いテスト。headless chromium で実際のページを開く。
//
//   PLAYWRIGHT_PATH=<playwright の index.mjs> node tests/ui_test.mjs
//
// PLAYWRIGHT_PATH が無ければ `import("playwright")` を使う。サーバー (humanq.py) は OS が選んだ空きの
// ポートと一時の state dir で起動し、/api/items・/api/answer・/api/undo・/api/snooze・/api/unsnooze は page.route の台本で返す
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

// sbBrowser は常時表示のスクロールバーを出せるブラウザ (Playwright の headless は既定で --hide-scrollbars を付け、
// スクロールバーが幅を取らない。Windows / Linux の Chrome や、macOS の「スクロールバーを常に表示」の画面を測るため)
let browser, sbBrowser, server, stateDir, baseUrl;

// OS に空きを選ばせる (port 0)。固定の範囲を順に調べると、同じマシンで suite と同時に回したとき取り合う
function osPort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

async function startServer() {
  const port = await osPort();
  assert.ok(port, "空きポートを OS から得られない");
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
  sbBrowser = await pw.chromium.launch({ headless: true, ignoreDefaultArgs: ["--hide-scrollbars"] });
});

after(async () => {
  if (browser) await browser.close();
  if (sbBrowser) await sbBrowser.close();
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
// asig はサーバーが付ける回答の署名。取り消しはこれを付けて送り、今の回答と違えば 409 stale になる
const answeredItem = (id, answer = "[humanq] 回答: 案A (a)\n", asig = `asig-${id}`) => ({ ...item(id), answer, asig });
const STALE_MSG = "取り消しませんでした。この回答はもう最新ではありません (ほかで答え直されています)";
const proj = (p, human = [], ans = [], ok = true) =>
  ({ path: p, name: p.split("/").pop(), ok, ...(ok ? {} : { error: "bd down" }), human, answered: ans });

class Script {
  constructor(projects) {
    this.projects = projects;
    this.itemsCount = 0;
    this.itemsFail = false;          // true の間、/api/items は 500 を返す
    this.answers = [];
    this.undos = [];
    this.queue = [];                 // 次の /api/items に返す固定のデータ (先頭から使う)
    this.gates = {};
    this.releases = [];
    this.answerN = 0;
    // 既定の回答の応答は、サーバーと同じく、見ていた問いの署名 (qsig) が今の問いと違えば 409 changed にする。
    // 成功には回答の署名 (asig) を入れる
    this.answerResult = body => {
      const cur = (this.project(body.project) || { human: [] }).human.find(i => i.id === body.id);
      if (cur && body.qsig !== undefined && body.qsig !== cur.qsig)
        return { status: 409, body: { error: "問いが書き換わっている", code: "changed", op: "answer" } };
      return { status: 200, body: { ok: true, asig: this.moveToAnswered(body) } };
    };
    // 既定の取り消しの応答も、サーバーと同じく、取り消す回答の署名 (asig) が今の回答と違えば 409 stale にする
    // (送らない古い画面は検査しない)
    this.undoResult = body => {
      const cur = (this.project(body.project) || { answered: [] }).answered.find(i => i.id === body.id);
      if (cur && body.asig !== undefined && body.asig !== cur.asig)
        return { status: 409, body: { error: "この回答はもう最新ではない", code: "stale", op: "undo" } };
      this.moveToHuman(body);
      return { status: 200, body: { ok: true } };
    };
    this.snoozes = [];
    this.unsnoozes = [];
    // 既定の保留の応答も、サーバーと同じく、問いの署名が違えば 409 changed、判断待ちに無ければ 400 gone
    this.snoozeResult = body => {
      const cur = (this.project(body.project) || { human: [] }).human.find(i => i.id === body.id);
      if (!cur) return { status: 400, body: { error: "このチケットは判断待ちに無い", code: "gone", op: "snooze" } };
      if (body.qsig !== undefined && body.qsig !== cur.qsig)
        return { status: 409, body: { error: "問いが書き換わっている", code: "changed", op: "snooze" } };
      this.moveToSnoozed(body);
      return { status: 200, body: { ok: true } };
    };
    this.unsnoozeResult = body => {
      const p = this.project(body.project) || { human: [], snoozed: [] };
      if (!(p.snoozed || []).some(i => i.id === body.id) && !p.human.some(i => i.id === body.id))
        return { status: 400, body: { error: "このチケットは判断待ちに無い", code: "gone", op: "unsnooze" } };
      this.moveFromSnoozed(body);
      return { status: 200, body: { ok: true } };
    };
  }
  project(p) { return this.projects.find(x => x.path === p); }
  // 判断待ちから保留中へ (サーバーの読みと同じく snooze {until, note, expired, changed} を付ける)
  moveToSnoozed({ project, id, until, note }) {
    const p = this.project(project);
    const it = p.human.find(i => i.id === id);
    p.human = p.human.filter(i => i.id !== id);
    p.snoozed = [...(p.snoozed || []), { ...it, snooze: { until, note: (note || "").trim(), expired: false, changed: false } }];
  }
  moveFromSnoozed({ project, id }) {
    const p = this.project(project);
    const it = (p.snoozed || []).find(i => i.id === id);
    p.snoozed = (p.snoozed || []).filter(i => i.id !== id);
    if (it && !p.human.some(i => i.id === id)) p.human.push({ ...it, snooze: null });
  }
  // 回答済みに移し、回答の署名を返す (サーバーと同じく、回答のたびに別の値)
  moveToAnswered({ project, id }) {
    const p = this.project(project);
    p.human = p.human.filter(i => i.id !== id);
    const asig = `asig-${id}-${++this.answerN}`;
    p.answered.push(answeredItem(id, undefined, asig));
    return asig;
  }
  moveToHuman({ project, id }) {
    const p = this.project(project);
    p.answered = p.answered.filter(i => i.id !== id);
    p.human.push(item(id));
  }
  // today (サーバーの今日) は、テストが入れたときだけ返す (返さないのは古いサーバーと同じ)
  snapshot() { return JSON.parse(JSON.stringify({ projects: this.projects, ...(this.today !== undefined ? { today: this.today } : {}) })); }
  // kind (items / answer / undo / snooze / unsnooze) の応答を release() するまで止める
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

async function withPage(script, fn, ctxOptions = {}, setup = null, b = browser) {
  const ctx = await b.newContext(ctxOptions);
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
    if (script.itemsFail) return json(route, 500, { error: "down" });    // サーバーに届かない・応答が壊れた状態
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
  await page.route("**/api/snooze", async route => {
    const body = route.request().postDataJSON();
    script.snoozes.push(body);
    if (script.gates.snooze) await script.gates.snooze;
    await reply(route, script.snoozeResult(body));
  });
  await page.route("**/api/unsnooze", async route => {
    const body = route.request().postDataJSON();
    script.unsnoozes.push(body);
    if (script.gates.unsnooze) await script.gates.unsnooze;
    await reply(route, script.unsnoozeResult(body));
  });
  try {
    if (setup) await setup(page);
    await page.goto(baseUrl);
    try {
      await ready(page);
    } catch (e) {
      // 最初の読み込みを待てなかったのがページの例外のせいなら、待ちの時間切れではなくその例外を失敗として出す
      assert.deepEqual(errors, [], "ページで例外が出た (最初の読み込みを待てなかった)");
      throw e;
    }
    try {
      await fn(page, ctx);
    } catch (e) {
      // ページで例外が出ていたら、その後の待ちの時間切れではなく、ページの例外を失敗として出す (原因が見える)
      assert.deepEqual(errors, [], `ページで例外が出た (テストは「${String(e && e.message).split("\n")[0]}」で止まった)`);
      throw e;
    }
    assert.deepEqual(errors, [], "ページで例外が出た");
  } finally {
    script.releaseAll();
    await ctx.close();
  }
}

const ready = page => page.waitForFunction(() => /更新/.test(document.getElementById("meta").textContent));
const qcard = (page, id) => page.locator(`section.card:has(textarea):has(.id:text-is("${id}"))`);
const acard = (page, id) => page.locator(`section.card:has(.ans):has(.id:text-is("${id}"))`);
// 回答済み・反映待ちの節は初めは畳んである (details.ansgrp)。回答済みのカードを押す・測るテストは、先にこれで開く。
// 畳んである節だけを押す (開いている節の summary を押すと閉じてしまう)。開いたかどうかは節ごとに覚えられるので、一度開けば読み込みで閉じない
const openAnswered = async page => {
  const closed = page.locator("details.ansgrp:not([open]) > summary");     // 開くと一致から外れるので、先頭を押し続ける (all() の添字は押すたびにずれる)
  for (let i = 0; i < 20 && (await closed.count()) > 0; i++) await closed.first().click();
};

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

// B の選択肢を選び、補足欄に書く (B の中身を s.projects[0].human[i] で変えて load すると、B は作り直しを後回しにされる)
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
    assert.equal(await page.locator("h2").count(), 1);
    assert.equal(await page.locator("h3").count(), 1);
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

test("押した直後に h3 の件数・空の見出し・「判断待ちはありません」が更新される", async () => {
  const s = new Script([proj(P1, [item("t-A1")]), proj(P2, [item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    s.hold("answer");                                            // POST はずっと返らない
    const h2s = () => page.$$eval("h3", es => es.map(e => e.textContent));
    assert.deepEqual(await h2s(), ["判断 (1)", "判断 (2)"]);
    await answerFirst(page, "t-B2");
    await waitFor(async () => JSON.stringify(await h2s()) === JSON.stringify(["判断 (1)", "判断 (1)"]), 3000, "beta の件数が 1");
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await page.$$eval("h2", es => es.map(e => e.textContent))).length === 1, 3000, "alpha の見出しが消える");
    await answerFirst(page, "t-C3");
    await waitFor(async () => (await page.locator(".empty").count()) === 1 &&
      /判断待ちはありません/.test(await page.locator(".empty").textContent()), 3000, "空の文言");
    // 空の見出しは、中のカードが畳み終わったら外れる
    await waitFor(async () => (await page.locator("h3").count()) === 0 && (await page.locator("h2").count()) === 0, 3000, "見出しが全部外れる");
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

// ---- サーバーに届かないとき・読めないプロジェクト -------------------------------------------------------

const noticeBox = page => page.locator("#notice");
const failLoad = async (page, s) => { s.itemsFail = true; await page.evaluate(() => load()); };

test("読み込みが 2 回続けて失敗すると、最後に読めた時刻つきの案内と (古いデータ) が出る。1 回では出ない。成功で消える", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    await page.clock.setFixedTime(new Date("2026-11-26T09:05:00"));
    await page.evaluate(() => load());                                  // この時刻が「最後に読めた」時刻
    await page.clock.setFixedTime(new Date("2026-11-26T10:20:00"));
    const meta = page.locator("#meta");
    await failLoad(page, s);
    assert.equal(await noticeBox(page).isHidden(), true, "1 回の失敗では案内を出さない");
    assert.ok(!/古いデータ/.test(await meta.textContent()), "1 回の失敗では目印を付けない");
    await failLoad(page, s);
    await noticeBox(page).waitFor({ state: "visible", timeout: 3000 });
    const text = await noticeBox(page).textContent();
    assert.match(text, /画面のサーバーに届きません/);
    assert.match(text, /最後に読めたのは 09:05/, "読めた時刻 (失敗した時刻ではない)");
    assert.ok(!/10:20/.test(text), "失敗した時刻を出さない");
    assert.match(text, /自動で読み直しています/);
    assert.ok(/古いデータ/.test(await meta.textContent()), "#meta の目印");
    assert.equal(await noticeBox(page).getByRole("button", { name: "今すぐ読み直す" }).count(), 1);
    s.itemsFail = false;
    await page.evaluate(() => load());
    await noticeBox(page).waitFor({ state: "hidden", timeout: 3000 });
    assert.ok(!/古いデータ/.test(await meta.textContent()), "成功で目印が外れる");
    assert.match(await meta.textContent(), /更新/);
    // 成功で失敗の数え直し: 1 回失敗しただけでは、また出ない
    await failLoad(page, s);
    assert.equal(await noticeBox(page).isHidden(), true, "成功のあとの 1 回の失敗では出ない");
  });
});

test("案内の「今すぐ読み直す」は読み込みを呼び、読めれば案内が消える", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    await failLoad(page, s);
    await failLoad(page, s);
    await noticeBox(page).waitFor({ state: "visible", timeout: 3000 });
    const n = s.itemsCount;
    s.itemsFail = false;
    await noticeBox(page).getByRole("button", { name: "今すぐ読み直す" }).click();
    await waitFor(() => s.itemsCount > n, 3000, "読み込みが呼ばれる");
    await noticeBox(page).waitFor({ state: "hidden", timeout: 3000 });
  });
});

test("token の案内が出ている間に失敗 → 成功しても、token の案内は上書きされず、消えない", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  s.answerResult = () => ({ status: 403, body: { error: "token" } });
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await page.getByRole("button", { name: /再読み込み/ }).waitFor({ timeout: 3000 });
    const before = await noticeBox(page).textContent();
    assert.match(before, /入れ替わりました/);
    await failLoad(page, s);
    await failLoad(page, s);
    assert.equal(await noticeBox(page).textContent(), before, "失敗が続いても文言は token のまま");
    assert.ok(/古いデータ/.test(await page.locator("#meta").textContent()), "目印は付く");
    s.itemsFail = false;
    await page.evaluate(() => load());
    await waitFor(async () => !/古いデータ/.test(await page.locator("#meta").textContent()), 3000, "目印が外れる");
    assert.equal(await noticeBox(page).isVisible(), true, "成功しても token の案内は消えない");
    assert.equal(await noticeBox(page).textContent(), before);
  });
});

test("読めなかったプロジェクトのバナーに、前に読めた時刻と「自動で読み直します」が出る。一度も読めていなければ時刻は出ない", async () => {
  const s = new Script([proj(P1, [item("t-A1")]), proj(P2, [item("t-B1")])]);
  await withPage(s, async page => {
    await page.clock.setFixedTime(new Date("2026-11-26T09:05:00"));
    await page.evaluate(() => load());                                  // 両方が 09:05 に読めた
    await page.clock.setFixedTime(new Date("2026-11-26T10:20:00"));
    s.queue.push({ projects: [proj(P1, [], [], false), proj(P2, [item("t-B1")])] });
    await page.evaluate(() => load());
    const banner = page.locator(".banner");
    await banner.first().waitFor({ timeout: 3000 });
    assert.equal(await banner.count(), 1, "読めなかった P1 だけ");
    const text = await banner.textContent();
    assert.match(text, /^読めませんでした \(最後に読めたのは 09:05[^。]*。自動で読み直します\): bd down$/);   // 時刻の書式 (AM など) は環境による
    assert.ok(!/10:20/.test(text));
    // 失敗が続いても、最後に読めた時刻は 09:05 のまま
    s.queue.push({ projects: [proj(P1, [], [], false), proj(P2, [item("t-B1")])] });
    await page.clock.setFixedTime(new Date("2026-11-26T11:30:00"));
    await page.evaluate(() => load());
    assert.match(await banner.textContent(), /最後に読めたのは 09:05/);
  });
  // 一度も読めていないプロジェクト (最初から ok:false)
  const s2 = new Script([proj(P1, [], [], false)]);
  await withPage(s2, async page => {
    const text = await page.locator(".banner").textContent();
    assert.match(text, /^読めませんでした \(自動で読み直します\): bd down$/);
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
    await openAnswered(page);
    await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
    await waitFor(() => s.undos.length === 1, 3000, "POST");
    assert.deepEqual(s.undos[0], { project: P1, id: "t-A1", asig: "asig-t-A1" });   // カードが出している回答の署名
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
    await openAnswered(page);                                   // 次のカード (回答済み) が見えている状態で押す (畳んだ節の中では次のカードにならない)
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
    await waitFor(async () => /読めませんでした/.test(await page.locator("main").textContent()), 3000, "バナー");
    assert.ok(await handle.evaluate(e => e.isConnected && !e.classList.contains("leaving")), "ok:false でカードが消えた");
    await page.evaluate(() => load());
    await waitFor(async () => !/読めませんでした/.test(await page.locator("main").textContent()), 3000, "バナーが消える");
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

test("入力中で作り直しを後回しにしているカードの下のカードを押している最中に、上のカードが作り直されても、click が消えない", async () => {
  // B の補足欄に入力している間に B の背景が長くなって作り直しが後回しになる。入力欄からフォーカスが外れたとき B が作り直されて高くなり、
  // その下の C が押している最中に動くと、離した位置にボタンが無く click が消える (スクロールしていない背の高い画面)
  const s = new Script([proj(P1, [item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "abc");
    const b = s.projects[0].human[0];
    s.projects[0].human[0] = { ...changedQ(b), q: { ...changedQ(b).q, context: Array.from({ length: 8 }, (_, i) => `line${i + 1}`).join("\n") } };
    await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => staleCount), 1, "B の作り直しが後回しになっていない");
    await qcard(page, "t-C3").locator(".opt").first().evaluate(e => e.click());     // C に選択を入れる (フォーカスは B の入力欄のまま)
    await press(page, qcard(page, "t-C3").locator("button.primary"));
    await waitFor(() => s.answers.length === 1, 3000, "C の回答の POST");
    assert.deepEqual(s.answers.map(a => a.id), ["t-C3"]);
    // 離したあとには、後回しにしていた B が作り直される
    await waitFor(async () => /line8/.test(await qcard(page, "t-B2").textContent()), 3000, "押したあとに B が作り直される");
  }, { viewport: { width: 1280, height: 1600 } });
});

test("作り直しを後回しにしているカード (問い以外が変わっただけ) 自身の「回答する」を押している最中に作り直されず、click が届いて送られる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "memo");
    s.projects[0].human[1].description = "NEW BODY";                         // 問いは変わらない
    await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => staleCount), 1, "B の作り直しが後回しになっていない");
    await press(page, qcard(page, "t-B2").locator("button.primary"));
    await waitFor(() => s.answers.length === 1, 3000, "B の回答の POST");
    assert.equal(s.answers[0].id, "t-B2");
    assert.equal(s.answers[0].text, "memo");
  });
});

test("作り直しを後回しにしているカード (問い以外が変わっただけ) の「回答する」へ Tab で移して Enter を押しても、作り直されずに送られる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await typeInto(page, "t-B2", "memo");
    s.projects[0].human[1].description = "NEW BODY";
    await page.evaluate(() => load());
    assert.equal(await page.evaluate(() => staleCount), 1, "B の作り直しが後回しになっていない");
    await page.keyboard.press("Tab");                                         // 入力欄から同じカードの「回答する」へ
    await page.waitForTimeout(60);                                            // focusout の setTimeout 0 が走る (人が Enter を押すまでの間)
    await page.keyboard.press("Enter");
    await waitFor(() => s.answers.length === 1, 3000, "B の回答の POST");
    assert.equal(s.answers[0].id, "t-B2");
  });
});

test("入力中で作り直しを後回しにしているカードは、別のカードのボタンへフォーカスが移ると、読み込みを待たずに作り直される", async () => {
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

test("入力中で作り直しを後回しにしているカードの問いが変わっていたら、「回答する」は送らずに作り直して見直してもらう", async () => {
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
    await openAnswered(page);
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
    await openAnswered(page);
    await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
    await page.locator(".toast").first().waitFor({ timeout: 3000 });
    const t = await page.locator(".toast").first().innerText();
    assert.match(t, /取り消しは記録済みです/);
    assert.match(t, /判断待ちの一覧/);
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに出る");
  });
});

// ---- 0.3.0: 回答の成功の知らせと、そこからの取り消し・トーストの位置 ---------------------------------------

const okToasts = page => page.locator("#toasts .toast.ok:not(.out)");
const undoInToast = page => okToasts(page).getByRole("button", { name: "取り消す" });
// 2 つの矩形が重なるか (辺が接するだけなら重ならない)
const overlaps = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

test("回答が成功すると、成功のトースト (.toast.ok) に題と選んだ選択肢の label が出る。補足だけの回答は (自由記述)", async () => {
  const long = "とても長い題".repeat(6);                                         // 36 字: 24 字に切る
  const longLabel = "とても長い選択肢の名前".repeat(5);                             // 55 字: 40 字に切る
  const c3 = item("t-C3");
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2", { title: long }),
    { ...c3, q: { ...c3.q, recommended: null, options: [{ key: "a", label: longLabel, desc: "" }] } }])]);
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();                  // 案B
    await qcard(page, "t-A1").locator("button.primary").click();
    await okToasts(page).first().waitFor({ timeout: 3000 });
    assert.equal(await okToasts(page).count(), 1);
    assert.equal(await okToasts(page).first().locator(".toast-msg").innerText(), "「title t-A1」に回答しました: 案B");
    assert.equal(await undoInToast(page).count(), 1, "「取り消す」が無い");
    assert.equal(await okToasts(page).first().evaluate(e => getComputedStyle(e).borderLeftColor), "rgb(31, 122, 77)", "成功の色 (--rec) でない");
    await qcard(page, "t-B2").locator("textarea").fill("memo");               // 選択肢を選ばず補足だけ
    await qcard(page, "t-B2").locator("button.primary").click();
    await waitFor(async () => (await okToasts(page).count()) === 2, 3000, "2 件目の成功のトースト");
    assert.equal(await okToasts(page).nth(1).locator(".toast-msg").innerText(), `「${long.slice(0, 23)}…」に回答しました: (自由記述)`);
    await answerFirst(page, "t-C3");                                            // 長い label
    await waitFor(async () => (await okToasts(page).count()) === 3, 3000, "3 件目の成功のトースト");
    assert.equal(await okToasts(page).nth(2).locator(".toast-msg").innerText(), `「title t-C3」に回答しました: ${longLabel.slice(0, 39)}…`);
    assert.equal(await page.locator("#toasts .toast:not(.ok)").count(), 0, "成功なのに失敗のトーストが出た");
  });
});

test("トーストの「取り消す」は、回答済みのカードが作られる前でも /api/undo を {project,id,asig} で送り、カードは空で判断待ちに戻る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    await qcard(page, "t-A1").locator("textarea").fill("memo");
    const releaseItems = s.hold("items");                                      // 成功の後の読み込みを止める: 回答済みのカードはまだ作られない
    await qcard(page, "t-A1").locator("button.primary").click();
    await okToasts(page).first().waitFor({ timeout: 3000 });
    assert.equal(await acard(page, "t-A1").count(), 0, "回答済みのカードがもう作られている");
    await undoInToast(page).click({ timeout: 3000 });
    await waitFor(() => s.undos.length === 1, 3000, "取り消しの POST");
    assert.deepEqual(s.undos[0], { project: P1, id: "t-A1", asig: "asig-t-A1-1" });   // 回答の成功の応答が返した署名
    releaseItems();
    await waitFor(async () => (await qcard(page, "t-A1").count()) >= 1, 3000, "判断待ちに戻る");
    await single(page, "t-A1");
    assert.equal(await acard(page, "t-A1").count(), 0, "回答済みに残っている");
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "", "下書きは戻さない");
    assert.equal(await qcard(page, "t-A1").locator(".opt.sel").count(), 0, "選択は戻さない");
    assert.equal(await page.locator("#toasts .toast:not(.out)").count(), 0, "押したトーストが残っている、または取り消しの成功に知らせが出た");
    assert.equal((await page.locator("#count").textContent()).trim(), "2 件");
  });
});

test("回答済みのカードが出た後でも、トーストの「取り消す」でそのカードは判断待ちに戻る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
    await undoInToast(page).click({ timeout: 3000 });
    await waitFor(() => s.undos.length === 1, 3000, "取り消しの POST");
    assert.deepEqual(s.undos[0], { project: P1, id: "t-A1", asig: "asig-t-A1-1" });
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに戻る");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 0, 3000, "回答済みから消える");
  });
});

test("回答済みのカードで取り消したら、その回答の成功のトーストも閉じる。答え直した後は今の答えのトーストだけが残り、今の答えは取り消されない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");                                          // 案A
    await okToasts(page).first().waitFor({ timeout: 3000 });
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
    await openAnswered(page);
    const releaseUndo = s.hold("undo");                                      // カードの取り消しの POST は止めておく
    await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
    await waitFor(() => s.undos.length === 1, 3000, "カードの取り消しの POST");
    // 送信中でも、取り消した回答の「取り消す」はもう押せない (押せると、取り消し済みのチケットに 2 度目を送る)
    await waitFor(async () => (await okToasts(page).count()) === 0, 3000, "取り消した回答の成功のトースト (「取り消す」付き) が残っている");
    assert.equal(await undoInToast(page).count(), 0, "取り消した回答の「取り消す」が押せる");
    releaseUndo();
    await waitFor(async () => (await qcard(page, "t-A1").count()) >= 1, 3000, "判断待ちに戻る");
    await single(page, "t-A1");
    await qcard(page, "t-A1").locator(".opt").nth(1).click();                // 案B で答え直す
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(async () => (await okToasts(page).count()) === 1, 3000, "答え直しの成功のトースト");
    assert.deepEqual(await okToasts(page).locator(".toast-msg").allInnerTexts(), ["「title t-A1」に回答しました: 案B"]);
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "答え直した回答が回答済みに出る");
    await page.waitForTimeout(300);
    assert.equal(s.undos.length, 1, "取り消しが二重に送られた、または今の答えが取り消された");
    assert.deepEqual(s.answers.map(a => a.option), ["a", "b"]);
    assert.equal(await qcard(page, "t-A1").count(), 0, "今の答えが取り消されて判断待ちに戻った");
  });
});

test("トーストの「取り消す」が 400 gone なら、カードが無くても「ほかで回答・反映済み」と知らせる。ほかの失敗は回答済みのカードを戻して知らせる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.undoResult = () => ({ status: 400, body: { error: "反映待ちに無い", code: "gone", op: "undo" } });   // エージェントがもう拾った
  await withPage(s, async page => {
    const releaseItems = s.hold("items");
    await answerFirst(page, "t-A1");
    await undoInToast(page).click({ timeout: 3000 });
    await waitFor(() => s.undos.length === 1, 3000, "取り消しの POST");
    await page.locator("#toasts .toast:not(.ok)").first().waitFor({ timeout: 3000 });
    assert.equal(await acard(page, "t-A1").count(), 0, "このテストはカードが無いときの知らせを見る");
    assert.equal(await page.locator("#toasts .toast:not(.ok)").first().locator(".toast-msg").innerText(), "「title t-A1」(t-A1): ほかで回答・反映済みでした");
    releaseItems();
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "今の状態 (回答済み) が出る");
    assert.equal(await qcard(page, "t-A1").count(), 0, "判断待ちに戻した");
  });
  const s2 = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s2.undoResult = () => ({ status: 502, body: { error: "bd boom", stage: "comment", written: false, op: "undo" } });
  await withPage(s2, async page => {
    const releaseItems = s2.hold("items");
    await answerFirst(page, "t-A1");
    await undoInToast(page).click({ timeout: 3000 });
    await waitFor(() => s2.undos.length === 1, 3000, "取り消しの POST");
    assert.equal(await acard(page, "t-A1").count(), 0);
    releaseItems();
    await page.locator("#toasts .toast:not(.ok)").first().waitFor({ timeout: 3000 });
    assert.equal(await page.locator("#toasts .toast:not(.ok)").first().locator(".toast-msg").innerText(), "「title t-A1」(t-A1): bd boom。カードを戻しました");
    await waitFor(async () => (await page.locator("section.card.failed:has(.ans)").count()) === 1, 3000, "回答済みのカードが赤い枠で出る");
    await openAnswered(page);                                                // 失敗で戻ったカードも、畳んだ節の中 (トーストに要約は出ている)
    assert.match(await acard(page, "t-A1").locator(".errbox").innerText(), /bd boom/);
  });
});

test("回答が成功した直後に Enter を押し続けても、トーストの「取り消す」は押されない (フォーカスはトーストに移らない)", async () => {
  // 次のカード (回答済み) があるときと、次のカードが無いとき
  for (const projects of [[proj(P1, [item("t-B2")], [answeredItem("t-A1")])], [proj(P1, [item("t-B2")])]]) {
    const s = new Script(projects);
    await withPage(s, async page => {
      await openAnswered(page);                                              // 次のカード (回答済み) が見えている状態で押す (無いプロジェクトでは何もしない)
      await qcard(page, "t-B2").locator(".opt").first().click();
      await qcard(page, "t-B2").locator("button.primary").focus();
      await page.keyboard.press("Enter");
      await page.waitForTimeout(50);
      await page.keyboard.press("Enter");                                    // キーのリピートや押し直し
      await okToasts(page).first().waitFor({ timeout: 3000 });
      await page.keyboard.press("Enter");                                    // トーストが出た後にも押す
      await page.waitForTimeout(300);
      assert.deepEqual(s.answers.map(a => a.id), ["t-B2"]);
      assert.deepEqual(s.undos, [], "取り消しが送られた");
      assert.equal(await okToasts(page).count(), 1, "トーストが閉じた");
      assert.ok(await page.evaluate(() => !document.activeElement || !document.activeElement.closest("#toasts")), "フォーカスがトーストに移った");
    });
  }
});

test("成功のトーストにも、同じ知らせの重ね抑止と、見えている間だけ数える表示時間が効く", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  const setHidden = (page, v) => page.evaluate(h => Object.defineProperty(document, "hidden", { configurable: true, get: () => h }), v);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    // 同じチケットの同じ知らせは重ねない。同じ文でも別のプロジェクトのチケットなら (取り消す先が違うので) 出す
    await page.evaluate(p => answeredToast({ project: p, id: "t-A1", title: "title t-A1" }, "案A"), P1);
    assert.equal(await okToasts(page).count(), 1, "同じ知らせが重なった");
    await page.evaluate(p => answeredToast({ project: p, id: "t-A1", title: "title t-A1" }, "案A"), P2);
    assert.equal(await okToasts(page).count(), 2, "別のチケットの知らせが抑止された");
    await okToasts(page).nth(1).locator(".toast-x").click();                 // 同時に出すのは 3 つまでなので、下の失敗の知らせ 2 つの前に 1 つ閉じる
    // 失敗の知らせ (key を渡さない) は、今までどおり文そのもので重ねない
    await page.evaluate(() => { toast("同じ失敗の知らせ"); toast("同じ失敗の知らせ"); toast("別の失敗の知らせ"); });
    assert.equal(await page.locator("#toasts .toast:not(.ok):not(.out)").count(), 2, "失敗の知らせの重ね抑止が効かない");
    await setHidden(page, true);
    await page.clock.runFor(10000);                                          // 裏のタブで 10 秒
    assert.equal(await okToasts(page).count(), 1, "見えていない間にトーストが消えた");
    await setHidden(page, false);
    await page.clock.runFor(7000);
    await waitFor(async () => (await page.locator("#toasts .toast").count()) === 0, 3000, "見えている間に 6 秒経てば消える");
  }, {}, page => page.clock.install({ time: 0 }));
});

test("トーストの「取り消す」を、閉じる動きの間にもう一度押しても、取り消しは 1 回しか呼ばれない", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    // run の回数を直接数える (回答の取り消しは送信中の止めがあるので、POST の数では 2 度目の run を見分けられない)
    const r = await page.evaluate(() => {
      window.__runs = 0;
      toast("押し直しの確認", { kind: "ok", key: "double", action: { label: "取り消す", run: () => { window.__runs++; } } });
      const t = [...document.querySelectorAll("#toasts .toast")].find(e => e.dataset.key === "double");
      const act = t.querySelector(".toast-act");
      act.click();
      const closing = t.classList.contains("out") && t.isConnected;           // 閉じる動き (300ms) の途中
      act.click();
      return { closing, runs: window.__runs };
    });
    assert.ok(r.closing, "1 回目の click の後、トーストが閉じる動きに入っていない (このテストの前提)");
    assert.equal(r.runs, 1, "閉じる動きの間の 2 回目の click で、もう一度 run が呼ばれた");
    await waitFor(async () => (await page.locator("#toasts .toast").count()) === 0, 3000, "トーストが外れる");
    assert.equal(await page.evaluate(() => window.__runs), 1);
  });
});

test("reduced-motion では、成功のトーストも動かさず、閉じたらすぐ外す", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    assert.equal(await okToasts(page).first().evaluate(e => getComputedStyle(e).animationName), "none");
    await okToasts(page).first().locator(".toast-x").click();
    assert.equal(await page.locator("#toasts .toast").count(), 0, "閉じたのに残っている");
  }, { reducedMotion: "reduce" });
});

test("幅の広い画面では、トーストは右下に出て、カードの「回答する」と横に重ならない。幅 640 以下では下に全幅で出る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    for (const [w, h] of [[1280, 800], [700, 800], [641, 800]]) {
      await page.setViewportSize({ width: w, height: h });
      const box = await page.locator("#toasts").boundingBox();
      assert.ok(Math.abs(box.x + box.width - (w - 16)) <= 1 && Math.abs(box.y + box.height - (h - 16)) <= 1, `幅 ${w}: 右下に無い ${JSON.stringify(box)}`);
      const btns = await page.locator("section.card:not(.leaving) button.primary").all();
      assert.ok(btns.length >= 2, `幅 ${w}: 「回答する」が無い`);
      for (const b of btns) {
        const r = await b.boundingBox();
        assert.ok(r.x + r.width <= box.x, `幅 ${w}: 「回答する」(${r.x}〜${r.x + r.width}) がトーストの横幅 (${box.x}〜) に入る`);
      }
    }
    await page.setViewportSize({ width: 640, height: 800 });
    const box = await page.locator("#toasts").boundingBox();
    assert.ok(box.x <= 8 && box.x + box.width >= 640 - 8, `幅 640: 全幅でない ${JSON.stringify(box)}`);
  });
});

test("幅 375 では、トーストは下に全幅で出て、ページの最後までスクロールすればどの「回答する」とも重ならない", async () => {
  // 長い題の成功のトーストを 2 つ重ねる (低いトーストなら、もとの下の余白に収まってしまう)。最後のカードは別のプロジェクトの C
  const long = "とても長い題".repeat(5);
  const s = new Script([proj(P1, [item("t-A1", { title: long }), item("t-B2", { title: long + "2" })]), proj(P2, [item("t-C3")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    await answerFirst(page, "t-B2");
    await waitFor(async () => (await okToasts(page).count()) === 2, 3000, "2 つ目の成功のトースト");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1 && (await acard(page, "t-B2").count()) === 1, 3000, "回答済みに出る");
    await waitFor(async () => (await page.locator(".leaving, .anim").count()) === 0, 3000, "動きが終わる");
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const vp = page.viewportSize();
    const box = await page.locator("#toasts").boundingBox();
    assert.ok(box.x <= 8 && box.x + box.width >= vp.width - 8, `全幅でない ${JSON.stringify(box)}`);
    assert.ok(box.y + box.height >= vp.height - 8 - 1, `下に無い ${JSON.stringify(box)}`);
    const last = await qcard(page, "t-C3").locator("button.primary").boundingBox();
    assert.ok(last.y >= 0 && last.y + last.height <= vp.height, `最後の「回答する」が画面に入っていない ${JSON.stringify(last)}`);
    // 台本の確かめ: 足した余白 (main の下の padding のうち 64px を超える分) が無ければ、最後の「回答する」はトーストの下に入る高さか
    const added = await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector("main")).paddingBottom) - 64);
    const below = vp.height - (last.y + last.height) - added;          // 余白を足さないときの、最後の「回答する」の下から画面の下まで
    assert.ok(below < vp.height - box.y, `トーストが低く、余白の足しを試せていない (下 ${below}px、トーストの上端まで ${vp.height - box.y}px)`);
    for (const b of await page.locator("section.card button.primary").all()) {
      const r = await b.boundingBox();
      if (r && r.y + r.height > 0 && r.y < vp.height)
        assert.ok(!overlaps(r, box), `「回答する」${JSON.stringify(r)} がトースト ${JSON.stringify(box)} と重なる`);
    }
  }, { viewport: { width: 375, height: 700 } });
});

test("ほかで取り消されて判断待ちに戻ったチケットに答え直したら、前の答えの成功のトーストは閉じ、ほかのチケットのトーストは残る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");                                          // 案A
    await okToasts(page).first().waitFor({ timeout: 3000 });
    await answerFirst(page, "t-B2");
    await waitFor(async () => (await okToasts(page).count()) === 2, 3000, "B の成功のトースト");
    s.moveToHuman({ project: P1, id: "t-A1" });                               // 別のタブで A を取り消した
    await page.evaluate(() => load());
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "A が判断待ちに戻る");
    await qcard(page, "t-A1").locator(".opt").nth(1).click();                // 案B で答え直す
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(() => s.answers.length === 3, 3000, "答え直しの POST");
    await waitFor(async () => (await okToasts(page).locator(".toast-msg").allInnerTexts()).includes("「title t-A1」に回答しました: 案B"), 3000, "答え直しの成功のトースト");
    await waitFor(async () => (await okToasts(page).count()) === 2, 3000, "前の答え (案A) の成功のトーストが残っている");
    assert.deepEqual((await okToasts(page).locator(".toast-msg").allInnerTexts()).sort(),
      ["「title t-A1」に回答しました: 案B", "「title t-B2」に回答しました: 案A"], "ほかのチケットの知らせまで閉じた");
  });
});

test("答え直しが 400 gone (ほかで回答済み) なら、そのチケットの前の答えの成功のトーストも閉じる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    s.moveToHuman({ project: P1, id: "t-A1" });                               // 別のタブで取り消した
    await page.evaluate(() => load());
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "A が判断待ちに戻る");
    assert.equal(await okToasts(page).count(), 1, "このテストは前の答えの知らせが残っている状態から始める");
    s.answerResult = () => ({ status: 400, body: { error: "判断待ちに無い", code: "gone", op: "answer" } });   // その後、別のタブで答えた
    await answerFirst(page, "t-A1");
    await page.locator("#toasts .toast:not(.ok)").first().waitFor({ timeout: 3000 });
    assert.equal(await page.locator("#toasts .toast:not(.ok)").first().locator(".toast-msg").innerText(), "「title t-A1」(t-A1): ほかで回答・反映済みでした");
    await waitFor(async () => (await okToasts(page).count()) === 0, 3000, "前の答えの成功のトースト (今の状態に合わない「取り消す」付き) が残っている");
  });
});

test("トーストは、カードの操作の行のボタン (「回答する」「取り消して判断待ちに戻す」) と重ならない。常時表示のスクロールバーがあっても同じ", async () => {
  const long = "とても長い題".repeat(5);
  const ids = ["t-A1", "t-B2", "t-C3", "t-D4", "t-E5", "t-F6"];
  for (const sb of [0, 15]) {
    const s = new Script([proj(P1, ids.map(id => item(id, { title: `${long} ${id}` })))]);
    await withPage(s, async page => {
      if (sb) await page.addStyleTag({ content: `::-webkit-scrollbar { width: ${sb}px; height: ${sb}px; } ::-webkit-scrollbar-thumb { background: #888; }` });
      await answerFirst(page, "t-A1");
      await okToasts(page).first().waitFor({ timeout: 3000 });
      await answerFirst(page, "t-B2");
      await waitFor(async () => (await acard(page, "t-A1").count()) === 1 && (await acard(page, "t-B2").count()) === 1, 3000, "回答済みに出る");
      await openAnswered(page);                                              // 取り消しのボタンを測るので、節を開く
      for (const w of [481, 560, 640, 641, 700, 1280]) {
        const at = `sb ${sb} 幅 ${w}`;
        await page.setViewportSize({ width: w, height: 800 });
        // 成功のトーストを 2 つ出し直す (表示時間で消えないうちに測る。前の知らせは answeredToast が閉じる)
        await page.evaluate(([p, t]) => { for (const id of ["t-A1", "t-B2"]) answeredToast({ project: p, id, title: `${t} ${id}` }, "案A"); }, [P1, long]);
        await waitFor(async () => (await page.locator("#toasts .toast").count()) === 2 && (await okToasts(page).count()) === 2, 3000, `${at}: 成功のトーストが 2 つ`);
        await waitFor(async () => (await page.locator(".leaving, .anim").count()) === 0, 3000, `${at}: 動きが終わる`);
        const cw = await page.evaluate(() => document.documentElement.clientWidth);
        assert.equal(w - cw, sb, `${at}: スクロールバーの幅が台本と違う (このテストの前提)`);
        const kinds = await page.locator("section.card .row button").evaluateAll(bs => bs.map(b => b.className));
        assert.ok(kinds.includes("primary") && kinds.includes("ghost"), `${at}: 「回答する」と「取り消して判断待ちに戻す」の両方が無い ${kinds}`);
        if (w > 640) {                                                         // 右下に出し、横に重ならない (スクロールの位置によらない)
          const box = await page.locator("#toasts").boundingBox();
          assert.ok(Math.abs(box.x + box.width - (cw - 16)) <= 1 && Math.abs(box.y + box.height - (800 - 16)) <= 1, `${at}: 右下に無い ${JSON.stringify(box)}`);
          for (const b of await page.locator("section.card .row button").all()) {      // 接するだけでなく 8px は空ける
            const r = await b.boundingBox();
            assert.ok(r.x + r.width + 8 <= box.x, `${at}: ボタン (${r.x}〜${r.x + r.width}) とトーストの横幅 (${box.x}〜) の間が 8px ない`);
          }
        } else {                                                               // 下に全幅で出し、ページの最後までスクロールすれば重ならない
          await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
          const box = await page.locator("#toasts").boundingBox();
          assert.ok(Math.abs(box.x - 8) <= 1 && Math.abs(box.x + box.width - (cw - 8)) <= 1, `${at}: 全幅でない ${JSON.stringify(box)}`);
          const last = await acard(page, "t-B2").locator(".row button").boundingBox();
          assert.ok(last.y >= 0 && last.y + last.height <= 800, `${at}: 最後のカードの「取り消して判断待ちに戻す」が画面に入っていない ${JSON.stringify(last)}`);
          for (const b of await page.locator("section.card .row button").all()) {
            const r = await b.boundingBox();
            if (r.y + r.height > 0 && r.y < 800) assert.ok(!overlaps(r, box), `${at}: ボタン ${JSON.stringify(r)} がトースト ${JSON.stringify(box)} と重なる`);
          }
        }
      }
    }, {}, null, sb ? sbBrowser : browser);
  }
});

test("問い直された後に、前の答えの成功のトーストの「取り消す」が 400 gone になっても、新しい問いに書きかけた下書きは消えない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.undoResult = body => {                                                    // サーバーと同じく、回答済みに無ければ gone
    const p = s.project(body.project);
    if (!p.answered.some(i => i.id === body.id)) return { status: 400, body: { error: "反映待ちに無い", code: "gone", op: "undo" } };
    s.moveToHuman(body);
    return { status: 200, body: { ok: true } };
  };
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    const p = s.project(P1);                                                  // エージェントが問い直した
    p.answered = p.answered.filter(i => i.id !== "t-A1");
    p.human.push(changedQ(item("t-A1")));
    await page.evaluate(() => load());
    await single(page, "t-A1");
    await typeInto(page, "t-A1", "new memo");
    await undoInToast(page).first().click();
    await page.locator("#toasts .toast:not(.ok)").first().waitFor({ timeout: 3000 });
    assert.equal(s.undos.length, 1);
    await page.reload();
    await ready(page);
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "new memo", "取り消しの gone で、新しい問いの下書きが消えた");
  });
});

test("問い直された後に、前の答えの成功のトーストの「取り消す」が 500 で失敗しても、新しい問いに書きかけた下書きは消えない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.undoResult = () => ({ status: 500, body: { error: "boom", op: "undo" } });
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    const p = s.project(P1);                                                  // エージェントが問い直した
    p.answered = p.answered.filter(i => i.id !== "t-A1");
    p.human.push(changedQ(item("t-A1")));
    await page.evaluate(() => load());
    await single(page, "t-A1");
    await typeInto(page, "t-A1", "new memo");
    await undoInToast(page).first().click();
    await page.locator("#toasts .toast:not(.ok)").first().waitFor({ timeout: 3000 });
    assert.equal(s.undos.length, 1);
    await page.reload();
    await ready(page);
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "new memo", "取り消しの失敗で、新しい問いの下書きが消えた");
  });
});

test("前の答えの知らせを閉じるのは同じ id のチケットだけ (t-A1 で答えても、t-A10 の成功のトーストは残る)", async () => {
  const s = new Script([proj(P1, [item("t-A10"), item("t-A1")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A10");
    await okToasts(page).first().waitFor({ timeout: 3000 });
    await answerFirst(page, "t-A1");
    await waitFor(async () => (await okToasts(page).locator(".toast-msg").allInnerTexts()).includes("「title t-A1」に回答しました: 案A"), 3000, "t-A1 の成功のトースト");
    await page.waitForTimeout(400);
    assert.deepEqual((await okToasts(page).locator(".toast-msg").allInnerTexts()).sort(),
      ["「title t-A10」に回答しました: 案A", "「title t-A1」に回答しました: 案A"].sort(), "id の頭が同じ別のチケットの知らせを閉じた");
  });
});

test("トーストは同時に 3 つまで。4 つ目を出すと、いちばん古いものが閉じ、その「取り消す」は走らない", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    await page.evaluate(() => {
      window.__runs = 0;
      for (const n of [1, 2, 3, 4]) toast(`知らせ ${n}`, { kind: "ok", key: `limit-${n}`, action: { label: "取り消す", run: () => { window.__runs++; } } });
    });
    const live = () => page.locator("#toasts .toast:not(.out) .toast-msg").allInnerTexts();
    assert.deepEqual(await live(), ["知らせ 2", "知らせ 3", "知らせ 4"], "4 つ目を出しても、いちばん古いトーストが閉じない");
    await waitFor(async () => (await page.locator("#toasts .toast").count()) === 3, 3000, "閉じたトーストが外れる");
    assert.equal(await page.evaluate(() => window.__runs), 0, "上限で閉じたトーストの action が走った");
    await page.evaluate(() => toast("失敗の知らせ"));                         // 失敗の知らせも同じ上限で数える
    assert.deepEqual(await live(), ["知らせ 3", "知らせ 4", "失敗の知らせ"], "失敗の知らせを出しても、いちばん古いトーストが閉じない");
  });
});

test("幅 641・高さ 500・スクロールバー 15px で最長の知らせを 4 つ出しても、残ったトーストのボタンはすべて画面の中にある", async () => {
  const longErr = "bd の書き込みに失敗しました: " + "とても長いエラーの文。".repeat(15) + "\n2 行目の詳しい情報\n3 行目";
  const longTitle = "とても長い題".repeat(5), longLabel = "とても長い選択肢の名前".repeat(5);
  const ids = ["t-A1", "t-B2", "t-C3", "t-D4", "t-E5", "t-F6"];
  const s = new Script([proj(P1, ids.map(id => item(id)))]);
  s.answerResult = () => ({ status: 500, body: { error: longErr, op: "answer" } });
  await withPage(s, async page => {
    await page.addStyleTag({ content: "::-webkit-scrollbar { width: 15px; height: 15px; } ::-webkit-scrollbar-thumb { background: #888; }" });
    await answerFirst(page, "t-A1");                                          // 最長の失敗の知らせ (いちばん古い)
    await page.locator("#toasts .toast:not(.ok)").first().waitFor({ timeout: 3000 });
    await page.evaluate(() => Object.defineProperty(document, "hidden", { configurable: true, get: () => true }));   // 表示時間で消さない
    await page.evaluate(([p, t, l]) => { for (const id of ["t-B2", "t-C3", "t-X9"]) answeredToast({ project: p, id, title: `${t} ${id}` }, l); },
      [P1, longTitle, longLabel]);
    await page.waitForTimeout(100);
    await waitFor(async () => (await page.locator("#toasts .toast.out").count()) === 0, 3000, "閉じたトーストが外れる");
    const cw = await page.evaluate(() => document.documentElement.clientWidth);
    assert.equal(641 - cw, 15, "スクロールバーの幅が台本と違う (このテストの前提)");
    const btns = await page.locator("#toasts .toast button").all();
    assert.ok(btns.length >= 6, `残ったトーストのボタンが足りない (${btns.length})`);
    for (const b of btns) {                                                   // 上限が無いと、いちばん古いトーストのボタンが画面の上に出る
      const r = await b.boundingBox();
      assert.ok(r.x >= 0 && r.y >= 0 && r.x + r.width <= cw && r.y + r.height <= 500, `トーストのボタン ${JSON.stringify(r)} が画面の外にある`);
    }
    assert.equal(await page.locator("#toasts .toast").count(), 3, "残ったトーストが 3 つでない");
    assert.equal(await okToasts(page).count(), 3, "残ったのが新しい成功の知らせ 3 つでない");
  }, { viewport: { width: 641, height: 500 } }, null, sbBrowser);
});

// ---- 0.7.0: 取り消しの照合 (回答の署名 asig) ------------------------------------------------------------
// 台本はサーバーと同じく、取り消しの asig が今の回答の asig と違えば 409 stale を返し、送らなければ検査しない (古い画面)

const failToast = page => page.locator("#toasts .toast:not(.ok):not(.out)");
const undoOnCard = (page, id) => acard(page, id).getByRole("button", { name: "取り消して判断待ちに戻す" });

test("別のタブで取り消して答え直した後に、前の回答のトーストの「取り消す」を押しても、今の回答は取り消されない (409 stale)", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");                                          // 案A で答える
    await okToasts(page).first().waitFor({ timeout: 3000 });
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
    await openAnswered(page);                                                 // 読み直した後のカードの本文を読むので、節を開く (読み直しでも閉じない)
    // 別のタブで取り消して、案B で答え直した (このタブはまだ読み直していない)
    const p = s.project(P1);
    p.answered = p.answered.filter(i => i.id !== "t-A1");
    p.answered.push(answeredItem("t-A1", "[humanq] 回答: 案B (b)\n", "asig-other-tab"));
    await undoInToast(page).click({ timeout: 3000 });
    await waitFor(() => s.undos.length === 1, 3000, "取り消しの POST");
    assert.equal(s.undos[0].asig, "asig-t-A1-1", "トーストの「取り消す」が、その回答の署名を送らない");
    assert.deepEqual(s.project(P1).answered.map(i => i.asig), ["asig-other-tab"], "今の回答 (案B) が取り消された");
    await failToast(page).first().waitFor({ timeout: 3000 });
    assert.equal(await failToast(page).first().locator(".toast-msg").innerText(), `「title t-A1」(t-A1): ${STALE_MSG}`);
    // 読み直した回答済みのカードは今の回答 (案B) を出し、判断待ちには戻さず、失敗の枠も付けない
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1 && /案B/.test(await acard(page, "t-A1").locator(".ans").innerText()),
      3000, "読み直した回答済みのカードが今の回答を出す");
    assert.equal(await qcard(page, "t-A1").count(), 0, "判断待ちに戻した");
    assert.equal(await page.locator("section.card.failed").count(), 0, "失敗の枠でカードを戻した");
  });
});

test("回答済みのカードの取り消しも照合を通る: 読み直す前にほかで答え直されていたら取り消さず、読み直したカードからは今の回答を取り消せる", async () => {
  const s = new Script([proj(P1, [item("t-B2")], [answeredItem("t-A1")])]);
  await withPage(s, async page => {
    await openAnswered(page);
    s.project(P1).answered = [answeredItem("t-A1", "[humanq] 回答: 案B (b)\n", "asig-again")];   // ほかで取り消して案B で答え直した
    await undoOnCard(page, "t-A1").click();
    await waitFor(() => s.undos.length === 1, 3000, "カードの取り消しの POST");
    assert.equal(s.undos[0].asig, "asig-t-A1", "カードが出している回答の署名を送らない");
    await failToast(page).first().waitFor({ timeout: 3000 });
    assert.equal(await failToast(page).first().locator(".toast-msg").innerText(), `「title t-A1」(t-A1): ${STALE_MSG}`);
    assert.deepEqual(s.project(P1).answered.map(i => i.asig), ["asig-again"], "今の回答が取り消された");
    await waitFor(async () => (await acard(page, "t-A1").count()) === 1 && /案B/.test(await acard(page, "t-A1").locator(".ans").innerText()),
      3000, "回答済みのカードが今の回答で出直す");
    await undoOnCard(page, "t-A1").click();
    await waitFor(() => s.undos.length === 2, 3000, "2 回目の取り消しの POST");
    assert.equal(s.undos[1].asig, "asig-again", "出直したカードが今の回答の署名を送らない");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに戻る");
  });
});

test("回答の本文が同じでも署名が変われば、読み直しで回答済みのカードは作り直され、取り消しは今の署名を送る", async () => {
  const s = new Script([proj(P1, [item("t-B2")], [answeredItem("t-A1")])]);
  await withPage(s, async page => {
    await openAnswered(page);
    s.project(P1).answered = [answeredItem("t-A1", undefined, "asig-again")];   // ほかで取り消して、同じ案A で答え直した
    await page.evaluate(() => load());
    await undoOnCard(page, "t-A1").click();
    await waitFor(() => s.undos.length === 1, 3000, "取り消しの POST");
    assert.equal(s.undos[0].asig, "asig-again", "読み直したのに、前の回答の署名で取り消した");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに戻る");
  });
});

test("回答の成功の応答に署名が無ければ、トーストの「取り消す」は空の署名を送る (照合を素通りさせない)", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  s.answerResult = body => { s.moveToAnswered(body); return { status: 200, body: { ok: true } }; };
  await withPage(s, async page => {
    await answerFirst(page, "t-A1");
    await undoInToast(page).click({ timeout: 3000 });
    await waitFor(() => s.undos.length === 1, 3000, "取り消しの POST");
    assert.ok(Object.hasOwn(s.undos[0], "asig"), `署名を付けずに送った (古い画面と同じに読まれ、照合されない): ${JSON.stringify(s.undos[0])}`);
    assert.equal(s.undos[0].asig, "");
    await failToast(page).first().waitFor({ timeout: 3000 });
    assert.equal(s.project(P1).answered.length, 1, "署名の無い取り消しが通った");
  });
});

// ---- 文言の小さな直し (placeholder・題の二重表示・回答済みの key・ラベル・経過日数) ---------------------------

test("選択肢の無い task の補足欄は「結果を書く」、選択肢の無い decision と問いの無いカードは「回答を書く」、選択肢があれば補足の案内", async () => {
  const base = item("t-A1");
  const s = new Script([proj(P1, [
    { ...base, q: { ...base.q, kind: "task", options: [], recommended: null } },
    { ...item("t-B2"), q: { ...base.q, kind: "decision", options: [], recommended: null } },
    { ...item("t-C3"), q: null },
    { ...item("t-D4"), q: { ...base.q, kind: "task" } },
  ])]);
  await withPage(s, async page => {
    const ph = id => qcard(page, id).locator("textarea").getAttribute("placeholder");
    assert.equal(await ph("t-A1"), "結果を書く", "選択肢の無い task");
    assert.equal(await ph("t-B2"), "回答を書く", "選択肢の無い decision");
    assert.equal(await ph("t-C3"), "回答を書く", "問いの無いカード");
    assert.equal(await ph("t-D4"), "補足 (任意)。選択肢に無い答えはここに書く", "選択肢のある task");
  });
});

test("問いの無いカードでは、題の文字列がカードの中に 1 回だけ出る。問いのあるカードは題と問いの両方が出る", async () => {
  const s = new Script([proj(P1, [{ ...item("t-A1", { title: "ユニークな題 XYZ" }), q: null }, item("t-B2")])]);
  await withPage(s, async page => {
    const text = await qcard(page, "t-A1").innerText();
    assert.equal(text.split("ユニークな題 XYZ").length - 1, 1, `題が重なっている: ${text}`);
    assert.equal(await qcard(page, "t-A1").locator("p.title").count(), 0, "問いの無いカードに小さい題の行が出ている");
    assert.equal(await qcard(page, "t-A1").locator("p.ask").innerText(), "ユニークな題 XYZ", "ask の位置の題は残す");
    assert.equal(await qcard(page, "t-B2").locator("p.title").innerText(), "title t-B2");
    assert.equal(await qcard(page, "t-B2").locator("p.ask").innerText(), "ask t-B2");
  });
});

test("回答済みの本文の末尾の (key) は、問いの選択肢の key と一致するときだけ外す", async () => {
  const mk = (id, answer, q) => ({ ...item(id), answer, ...(q === undefined ? {} : { q }) });
  const base = item("x").q;
  const s = new Script([proj(P1, [], [
    mk("t-A1", "[humanq] 回答: 【応急】止める (a)\n"),
    mk("t-B2", "[humanq] 回答: (自由記述)\n補足の本文 (a)\n"),
    mk("t-C3", "[humanq] 回答: B 案 (暫定)\n", { ...base, options: [{ key: "a", label: "B 案 (暫定)", desc: "" }] }),
    mk("t-D4", "[humanq] 回答: 案A (a)\n\n補足 (b)\n2 行目"),
    mk("t-E5", "[humanq] 回答: 案A (a)\n", null),
    mk("t-F6", "[humanq] 回答: 案A (zz)\n"),
  ])]);
  await withPage(s, async page => {
    await openAnswered(page);
    const ans = async id => (await acard(page, id).locator(".ans").innerText()).replace(/\n+$/, "");
    assert.equal(await ans("t-A1"), "回答: 【応急】止める");
    assert.equal(await ans("t-B2"), "回答: (自由記述)\n補足の本文 (a)", "自由記述と補足の中の括弧はそのまま");
    assert.equal(await ans("t-C3"), "回答: B 案 (暫定)", "key でない括弧は残る");
    assert.equal(await ans("t-D4"), "回答: 案A\n\n補足 (b)\n2 行目", "1 行目の key だけ外す");
    assert.equal(await ans("t-E5"), "回答: 案A (a)", "q が無ければ何も外さない");
    assert.equal(await ans("t-F6"), "回答: 案A (zz)", "選択肢に無い key は外さない");
  });
});

test("head には human / answered 以外のラベルが全文で出る。多いときは先頭 4 つと +N (title に全部)", async () => {
  const many = ["human", "plugin:x", "area:ui", "kind:bug", "model:sonnet", "lane:l1", "plain"];
  const s = new Script([proj(P1, [item("t-A1", { labels: ["human", "plugin:x", "area:ui"] }), item("t-B2", { labels: many })],
    [item("t-C3", { labels: ["answered", "plugin:y"] })])]);
  await withPage(s, async page => {
    await openAnswered(page);
    const spans = card => card.locator(".head > span:not(.prio):not(.id)").allInnerTexts();
    const a = await spans(qcard(page, "t-A1"));
    assert.deepEqual(a.slice(1), ["plugin:x", "area:ui"], "全文で、human は出ない");
    const b = await spans(qcard(page, "t-B2"));
    assert.deepEqual(b.slice(1), ["plugin:x", "area:ui", "kind:bug", "model:sonnet", "+2"]);
    assert.equal(await qcard(page, "t-B2").locator('.head > span:text-is("+2")').getAttribute("title"),
      "plugin:x\narea:ui\nkind:bug\nmodel:sonnet\nlane:l1\nplain");
    const c = await spans(acard(page, "t-C3"));
    assert.deepEqual(c.slice(1), ["plugin:y"], "回答済みでも answered は出ない");
  });
});

test("作成日の横に「作成から N 日」が出る。当日は「今日」で、未来の日付や読めない日付には出さない", async () => {
  const s = new Script([proj(P1, [item("t-A1", { created_at: "2026-11-24" }), item("t-B2", { created_at: "2026-11-27" }),
    item("t-C3", { created_at: "2026-11-30" }), item("t-D4", { created_at: "" })], [item("t-E5", { created_at: "2026-11-20" })])]);
  await withPage(s, async page => {
    await openAnswered(page);
    const date = card => card.locator(".head > span:not(.prio):not(.id)").first().innerText();
    assert.equal(await date(qcard(page, "t-A1")), "2026-11-24 (作成から 3 日)");
    assert.equal(await date(qcard(page, "t-B2")), "2026-11-27 (今日)");
    assert.equal(await date(qcard(page, "t-C3")), "2026-11-30", "未来の日付に日数は出さない");
    assert.equal(await date(qcard(page, "t-D4")), "", "日付が無ければ何も出さない");
    assert.equal(await date(acard(page, "t-E5")), "2026-11-20 (作成から 7 日)");
  }, {}, page => page.clock.install({ time: new Date(2026, 10, 27, 12, 0, 0) }));
});

// ---- 0.5.0: 長い背景 (context) は 5 行で畳む ------------------------------------------------------------

const lines = n => Array.from({ length: n }, (_, i) => `ctx line ${i + 1}`).join("\n");
const withCtx = (id, n) => { const it = item(id); return { ...it, q: { ...it.q, context: lines(n) } }; };
const ctxMore = (page, id) => qcard(page, id).locator("button.ctxmore");
const ctxMetrics = (page, id) => qcard(page, id).locator("p.ctx").evaluate(e => ({
  h: e.getBoundingClientRect().height, lh: parseFloat(getComputedStyle(e).lineHeight), sh: e.scrollHeight }));

test("10 行の背景は 5 行分以下に畳まれ、「続きを読む」で全文が見え、「閉じる」で戻る", async () => {
  const s = new Script([proj(P1, [withCtx("t-A1", 10)])]);
  await withPage(s, async page => {
    await waitFor(async () => await ctxMore(page, "t-A1").isVisible(), 3000, "続きを読むが出る");
    assert.equal(await ctxMore(page, "t-A1").textContent(), "続きを読む");
    assert.equal(await ctxMore(page, "t-A1").getAttribute("aria-expanded"), "false");
    assert.equal(await ctxMore(page, "t-A1").getAttribute("type"), "button");
    const m = await ctxMetrics(page, "t-A1");
    assert.ok(m.h <= m.lh * 5 + 1, `畳んだ高さが 5 行を超える: ${m.h} / ${m.lh}`);
    assert.ok(m.sh > m.h + 1, "全文が畳まれていない");
    await ctxMore(page, "t-A1").click();
    const o = await ctxMetrics(page, "t-A1");
    assert.ok(o.h >= o.lh * 10 - 1, `開いても全文が見えない: ${o.h} / ${o.lh}`);
    assert.equal(await ctxMore(page, "t-A1").textContent(), "閉じる");
    assert.equal(await ctxMore(page, "t-A1").getAttribute("aria-expanded"), "true");
    await ctxMore(page, "t-A1").click();
    const c = await ctxMetrics(page, "t-A1");
    assert.ok(c.h <= c.lh * 5 + 1, "閉じても畳まれない");
    assert.equal(await ctxMore(page, "t-A1").textContent(), "続きを読む");
  });
});

test("3 行の背景と、ちょうど 5 行の背景では「続きを読む」が出ない。問いの無いカードにも出ない", async () => {
  const none = { ...item("t-C3"), q: null };
  const s = new Script([proj(P1, [withCtx("t-A1", 3), withCtx("t-B2", 5), none])]);
  await withPage(s, async page => {
    assert.equal(await ctxMore(page, "t-A1").count(), 1);
    assert.equal(await ctxMore(page, "t-A1").isVisible(), false);
    assert.equal(await ctxMore(page, "t-B2").isVisible(), false);
    assert.equal(await ctxMore(page, "t-C3").count(), 0);
    assert.match(await qcard(page, "t-C3").textContent(), /問いがまだ整理されていません/);
    const m = await ctxMetrics(page, "t-A1");
    assert.ok(m.h < m.lh * 3 + 1 && m.h >= m.lh * 3 - 1, "3 行の背景が 3 行で出ていない");
  });
});

test("開いた背景は、データが変わっても開いたまま。ほかのカードは畳んだまま", async () => {
  const s = new Script([proj(P1, [withCtx("t-A1", 10), withCtx("t-B2", 10)])]);
  await withPage(s, async page => {
    await waitFor(async () => await ctxMore(page, "t-A1").isVisible(), 3000, "続きを読む");
    await ctxMore(page, "t-A1").click();
    await page.locator("h1").click();            // フォーカスをカードの外へ (フォーカスのあるカードは作り直しを後回しにするため)
    assert.equal(await page.evaluate(() => document.activeElement === document.body), true);
    await qcard(page, "t-A1").evaluate(e => { e.dataset.mark = "old"; });
    s.projects[0].human[0].title = "changed title";
    await page.evaluate(() => load());
    await waitFor(async () => /changed title/.test(await qcard(page, "t-A1").textContent()), 3000, "A の題が変わる");
    await waitFor(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "A が 1 枚");
    assert.equal(await qcard(page, "t-A1").evaluate(e => e.dataset.mark || ""), "", "A が作り直されていない (旧い要素のまま)");
    const o = await ctxMetrics(page, "t-A1");
    assert.ok(o.h >= o.lh * 10 - 1, "データの更新で畳まれた");
    assert.equal(await ctxMore(page, "t-A1").textContent(), "閉じる");
    assert.equal(await ctxMore(page, "t-B2").textContent(), "続きを読む");
  });
});

test("幅 375・高さ 812 で、10 行の背景のカードの最初の選択肢が最初の画面に入る", async () => {
  const s = new Script([proj(P1, [withCtx("t-A1", 10)])]);
  await withPage(s, async page => {
    await waitFor(async () => await ctxMore(page, "t-A1").isVisible(), 3000, "続きを読む");
    const b = await qcard(page, "t-A1").locator(".opt").first().boundingBox();
    assert.ok(b.y >= 0 && b.y + b.height <= 812, `最初の選択肢が画面の外: y=${b.y} h=${b.height}`);
  }, { viewport: { width: 375, height: 812 } });
});

// ---- 推奨の目立たせ方と、対処の深さのチップ ----------------------------------------------------

const depthOpts = [
  { key: "a", label: "【根本】原因を直す", desc: "" },
  { key: "b", label: "【応急】`設定` で止める", desc: "" },
  { key: "c", label: "【調査】まず再現する", desc: "" },
  { key: "d", label: "【見送り】今はしない", desc: "" },
  { key: "e", label: "【注意】未知の括弧", desc: "" },
  { key: "f", label: "案F 【根本】は途中", desc: "" },
];
const withDepth = (id, recommended = "b") =>
  ({ ...item(id), q: { ...item(id).q, recommended, options: depthOpts } });
const opt = (page, id, key) => qcard(page, id).locator(".opt").nth(depthOpts.findIndex(o => o.key === key));

test("推奨の選択肢にだけ .opt.rec が付き、最初は選ばれておらず、「推奨」は label の前にある", async () => {
  const s = new Script([proj(P1, [withDepth("t-A1")])]);
  await withPage(s, async page => {
    const recs = await qcard(page, "t-A1").locator(".opt.rec").count();
    assert.equal(recs, 1);
    assert.equal(await opt(page, "t-A1", "b").evaluate(e => e.classList.contains("rec")), true);
    assert.equal(await qcard(page, "t-A1").locator(".opt.sel").count(), 0, "最初から選んだ状態になっている");
    const order = await opt(page, "t-A1", "b").evaluate(e => [...e.children].map(c => c.className.split(" ")[0]));
    assert.deepEqual(order.slice(0, 2), ["badge", "l"]);
    assert.equal(await opt(page, "t-A1", "b").locator(".badge").textContent(), "推奨");
    const bl = await opt(page, "t-A1", "b").evaluate(e => getComputedStyle(e).borderLeftWidth);
    assert.equal(bl, "4px");
    assert.notEqual(await opt(page, "t-A1", "a").evaluate(e => getComputedStyle(e).borderLeftWidth), "4px");
    assert.equal(await qcard(page, "t-A1").locator(".badge").count(), 1);
  });
});

test("推奨が無い問いでは .opt.rec も「推奨」も出ない", async () => {
  const it = withDepth("t-A1", null);
  const s = new Script([proj(P1, [it])]);
  await withPage(s, async page => {
    assert.equal(await qcard(page, "t-A1").locator(".opt.rec").count(), 0);
    assert.equal(await qcard(page, "t-A1").locator(".badge").count(), 0);
  });
});

test("先頭が README の 4 語の【…】の label は、チップ (文字は語だけ) と残りの文字に分かれ、title がある", async () => {
  const s = new Script([proj(P1, [withDepth("t-A1")])]);
  await withPage(s, async page => {
    const want = { a: ["根本", "原因を直す"], b: ["応急", "設定 で止める"], c: ["調査", "まず再現する"], d: ["見送り", "今はしない"] };
    for (const [key, [word, rest]] of Object.entries(want)) {
      const chips = opt(page, "t-A1", key).locator(".l > .depth");
      assert.equal(await chips.count(), 1, `${key}: チップが 1 つ`);
      assert.equal(await chips.textContent(), word);
      assert.equal(await chips.evaluate(e => e.classList.contains("depth-" + e.textContent)), true);
      assert.ok((await chips.getAttribute("title") || "").length > 4, `${key}: title が無い`);
      assert.equal(await opt(page, "t-A1", key).locator(".l").textContent(), word + rest, `${key}: 残りの文字`);
    }
    // 残りは md() で描く (` ` はコード表記のまま)
    assert.equal(await opt(page, "t-A1", "b").locator(".l code").textContent(), "設定");
  });
});

test("【】の無い label、README に無い【…】、先頭でない【根本】には .depth が出ず、文字のまま", async () => {
  const s = new Script([proj(P1, [withDepth("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    assert.equal(await qcard(page, "t-B2").locator(".depth").count(), 0);
    assert.equal(await qcard(page, "t-B2").locator(".opt .l").first().textContent(), "案A");
    assert.equal(await opt(page, "t-A1", "e").locator(".depth").count(), 0);
    assert.equal(await opt(page, "t-A1", "e").locator(".l").textContent(), "【注意】未知の括弧");
    assert.equal(await opt(page, "t-A1", "f").locator(".depth").count(), 0);
    assert.equal(await opt(page, "t-A1", "f").locator(".l").textContent(), "案F 【根本】は途中");
  });
});

test("チップを持つ選択肢を選んで送ると、/api/answer の option は元の key のまま", async () => {
  const s = new Script([proj(P1, [withDepth("t-A1")])]);
  await withPage(s, async page => {
    await opt(page, "t-A1", "b").locator(".depth").click();      // チップを押しても選択肢が選ばれる
    assert.equal(await qcard(page, "t-A1").locator(".opt.sel").count(), 1);
    await qcard(page, "t-A1").locator("button.primary").click();
    await waitFor(() => s.answers.length === 1, 3000, "POST");
    assert.deepEqual(s.answers[0], { project: P1, id: "t-A1", option: "b", text: "", qsig: "sig-1" });
  });
});

// 計算済みの色 (rgb / rgba) から WCAG のコントラスト比を出す
const RATIO_FN = () => {
  const parse = c => { const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) throw new Error("色が読めない: " + c); return m[1].split(/[ ,/]+/).filter(Boolean).slice(0, 3).map(Number); };
  const lum = ([r, g, b]) => [r, g, b].map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }).reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
  return (x, y) => { const a = lum(parse(x)), b = lum(parse(y)); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
};

for (const scheme of ["light", "dark"]) {
  test(`${scheme}: 4 種のチップは、枠が選択肢の背景に対して 3:1 以上、チップの中の文字が 4.5:1 以上。4 種の枠色は互いに違い、推奨の緑と混ざらない`, async () => {
    const s = new Script([proj(P1, [withDepth("t-A1")])]);
    await withPage(s, async page => {
      await page.emulateMedia({ colorScheme: scheme });
      const rows = await page.evaluate(() => {
        const out = [];
        const card = document.querySelector("section.card:has(textarea)");
        const rec = getComputedStyle(document.documentElement).getPropertyValue("--rec").trim();
        const recColor = (() => { const t = document.createElement("i"); t.style.color = rec; document.body.append(t); const c = getComputedStyle(t).color; t.remove(); return c; })();
        for (const word of ["根本", "応急", "調査", "見送り"]) {
          const chip = card.querySelector(".depth-" + word);
          const cs = getComputedStyle(chip);
          const optBg = getComputedStyle(chip.closest(".opt")).backgroundColor;
          out.push({ word, fg: cs.color, bg: cs.backgroundColor, bd: cs.borderTopColor, optBg, recColor });
        }
        // 選択中の背景 (.sel) でも枠が埋もれない
        const sel = card.querySelector(".opt.sel") || card.querySelector(".opt");
        sel.classList.add("sel");
        const selBg = getComputedStyle(sel).backgroundColor;
        sel.classList.remove("sel");
        return { out, selBg };
      });
      assert.equal(rows.out.length, 4);
      const bds = new Set();
      for (const r of rows.out) {
        const calc = await page.evaluate(([fnSrc, r, selBg]) => {
          const ratio = eval(`(${fnSrc})()`);
          return { bd: ratio(r.bd, r.optBg), bdSel: ratio(r.bd, selBg), text: ratio(r.fg, r.bg) };
        }, [RATIO_FN.toString(), r, rows.selBg]);
        assert.ok(calc.bd >= 3, `${scheme} ${r.word}: 枠 / 選択肢の背景 ${calc.bd.toFixed(2)}`);
        assert.ok(calc.bdSel >= 3, `${scheme} ${r.word}: 枠 / 選択中の背景 ${calc.bdSel.toFixed(2)}`);
        assert.ok(calc.text >= 4.5, `${scheme} ${r.word}: 文字 / チップの背景 ${calc.text.toFixed(2)}`);
        assert.notEqual(r.bd, r.recColor, `${scheme} ${r.word}: 枠が推奨の緑と同じ`);
        assert.notEqual(r.bg, rows.out[0].recColor);
        bds.add(r.bd + "|" + r.bg);
      }
      assert.equal(bds.size, 4, "4 種のチップが見分けられない");
    });
  });
}

// ---- 0.9.0: プロジェクトの目次の帯と、止まるプロジェクトの見出し -----------------------------------------------------
// 帯 (#toc) はヘッダーの下に置き、sticky にはしない。プロジェクトの見出し (h2) は sticky のヘッダーの下に止まる

const tocChips = page => page.locator("#toc .tocchip");
const chipOf = (page, p) => page.locator(`#toc .tocchip[data-path="${p}"]`);
const projHeadOf = (page, p) => page.locator(`h2:has(.path:text-is("${p}"))`);
const headBottom = page => page.evaluate(() => document.querySelector("header").getBoundingClientRect().bottom);
// 待っても満たされなければ assertion の失敗にする (時間切れを、テストが走っていない例外と分ける)
const until = (fn, ms, what) => waitFor(fn, ms, what).catch(e => assert.fail(e.message));
// 帯のチップの中身 (プロジェクトの順)。P0 の印は表示されているものだけ数える
const chipRows = page => page.$$eval("#toc .tocchip", cs => cs.map(c => {
  const mark = c.querySelector(".tocp0");
  return { path: c.dataset.path, name: (c.querySelector(".tocname") || {}).textContent, n: (c.querySelector(".tocn") || {}).textContent,
    p0: !!mark && getComputedStyle(mark).display !== "none" };
}));
const many = (pfx, n) => Array.from({ length: n }, (_, i) => item(`t-${pfx}${i + 1}`));
// 描画を 2 回進める。ResizeObserver (ヘッダーの高さ → --head-h) は次の描画の手前で届くので、幅を変えた直後の測りはこれを挟む
const frames = page => page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
// path の見出しの位置と、その中心 (名前の上) が他の要素に隠れず見えているか。見出しが無ければ null
const projHeadShown = (page, p) => page.evaluate(p => {
  const h = [...document.querySelectorAll("h2")].find(e => (e.querySelector(".path") || {}).textContent === p);
  if (!h) return null;
  const r = h.getBoundingClientRect();
  const hit = r.top + r.height / 2 >= 0 && r.top + r.height / 2 < innerHeight ? document.elementFromPoint(r.left + 12, r.top + r.height / 2) : null;
  return { top: r.top, bottom: r.bottom, height: r.height, head: document.querySelector("header").getBoundingClientRect().bottom, vh: innerHeight,
    visible: !!hit && h.contains(hit), text: h.textContent };
}, p);
// カード (id) の上端を、ヘッダーの下 off px に置く
const scrollCardTo = (page, id, off) => page.evaluate(([id, off]) => {
  const c = [...document.querySelectorAll("section.card")].find(e => e.querySelector(".id").textContent === id);
  scrollTo(0, c.getBoundingClientRect().top + scrollY - document.querySelector("header").getBoundingClientRect().bottom - off);
}, [id, off]);

test("目次の帯: 判断待ちか回答済みがあるプロジェクトごとに、プロジェクトの順でチップが出る。中身は名前と判断待ちの件数で、判断待ちに P0 があるものにだけ P0 の色の印が付く", async () => {
  const P3 = "/work/gamma", P4 = "/work/delta", P5 = "/work/epsilon";
  const task = { ...item("t-A2"), q: { ...item("t-A2").q, kind: "task" } };
  const s = new Script([
    proj(P1, [item("t-A1"), task]),                                                  // 作業・確認も判断待ちに数える
    proj(P2, [item("t-B1", { priority: 0 }), item("t-B2", { priority: 2 })], [answeredItem("t-B9")]),
    proj(P3),                                                                          // 判断待ちも回答済みも無い
    proj(P4, [], [{ ...answeredItem("t-D1"), priority: 0 }]),                          // 回答済みだけ (回答済みの P0 は印にしない)
    proj(P5, [], [], false),                                                           // 一度も読めていない (バナーだけの入れ物。チップは出さない)
  ]);
  await withPage(s, async page => {
    assert.deepEqual(await chipRows(page), [
      { path: P1, name: "alpha", n: "2", p0: false },
      { path: P2, name: "beta", n: "2", p0: true },
      { path: P4, name: "delta", n: "0", p0: false },
    ]);
    assert.equal(await projHeadOf(page, P5).count(), 1, "前提: 読めなかったプロジェクトの見出しが出ていない");
    // 読み上げの名前は「名前: 判断待ち N 件」(P0 があれば「、P0 あり」)
    assert.equal(await page.getByRole("button", { name: "beta: 判断待ち 2 件、P0 あり", exact: true }).count(), 1);
    assert.equal(await page.getByRole("button", { name: "alpha: 判断待ち 2 件", exact: true }).count(), 1);
    // 帯はヘッダーのすぐ下 (案内が出ていないとき) で、最初のプロジェクトの見出しより上
    const pos = await page.evaluate(() => ({ head: document.querySelector("header").getBoundingClientRect().bottom,
      toc: document.getElementById("toc").getBoundingClientRect().toJSON(), h2: document.querySelector("h2").getBoundingClientRect().top }));
    assert.ok(Math.abs(pos.toc.top - pos.head) <= 1 && pos.toc.height > 10 && pos.toc.bottom <= pos.h2, `帯の位置 ${JSON.stringify(pos)}`);
    // P0 の印の色は --p0
    const colors = await page.evaluate(() => {
      const m = [...document.querySelectorAll("#toc .tocp0")].find(e => getComputedStyle(e).display !== "none");
      if (!m) return null;
      const t = document.createElement("i"); t.style.color = "var(--p0)"; document.body.append(t);
      const c = getComputedStyle(t).color; t.remove();
      return { mark: getComputedStyle(m).color, p0: c, text: m.textContent };
    });
    assert.ok(colors, "P0 の印が無い");
    assert.equal(colors.mark, colors.p0);
    assert.equal(colors.text, "P0");
    assert.equal(await chipOf(page, P2).getAttribute("title"), P2, "チップの title にパスが無い (同じ名前のプロジェクトを見分けられない)");
    // チップは読み込みで作り直さない: フォーカスしたチップは、件数が変わる読み込みの後もフォーカスを持つ
    await chipOf(page, P1).focus();
    s.projects[0].human.push(item("t-A3"));
    await page.evaluate(() => load());
    const after = await chipRows(page);
    assert.equal(after.length, 3);
    assert.equal(after[0].n, "3");
    assert.ok(await chipOf(page, P1).evaluate(e => e === document.activeElement), "読み込みでチップが作り直され、フォーカスが外れた");
  });
});

test("目次の帯: 押した直後にチップの件数と P0 の印が変わり (送信中を除く)、プロジェクトが空になればチップが消え、全部なくなれば帯も隠れる", async () => {
  const s = new Script([proj(P1, [item("t-A1")]), proj(P2, [item("t-B1", { priority: 0 }), item("t-B2")])]);
  await withPage(s, async page => {
    s.hold("answer");                                                                 // POST はずっと返らない (送信中のまま)
    const rows = async () => (await chipRows(page)).map(r => [r.name, r.n, r.p0]);
    assert.deepEqual(await rows(), [["alpha", "1", false], ["beta", "2", true]]);
    assert.equal(await page.locator("#toc").isVisible(), true);
    await answerFirst(page, "t-B1");                                                  // 押した処理の中で描き直す (待たずに測る)
    assert.deepEqual(await rows(), [["alpha", "1", false], ["beta", "1", false]], "押した直後に件数と P0 の印が変わらない");
    await answerFirst(page, "t-A1");
    assert.deepEqual(await rows(), [["beta", "1", false]], "空になったプロジェクトのチップが残っている");
    await answerFirst(page, "t-B2");
    assert.deepEqual(await rows(), []);
    assert.equal(await page.locator("#toc").isHidden(), true, "チップが無いのに帯が出ている");
  });
});

test("目次の帯: データから消えたプロジェクトのチップは消え、戻れば同じ位置に出直す", async () => {
  const s = new Script([proj(P1, [item("t-A1")]), proj(P2, [item("t-B1")])]);
  await withPage(s, async page => {
    const [a, b] = s.projects;
    s.projects = [b];
    await page.evaluate(() => load());
    assert.deepEqual((await chipRows(page)).map(r => r.name), ["beta"]);
    s.projects = [a, b];
    await page.evaluate(() => load());
    assert.deepEqual((await chipRows(page)).map(r => r.name), ["alpha", "beta"]);
  });
});

test("目次のチップを押すと、そのプロジェクトの見出しがヘッダーのすぐ下に来て、フォーカスが見出しへ移る。動きを減らす設定では、押したその場で移る (滑らかにしない)", async () => {
  const s = new Script([proj(P1, many("A", 5)), proj(P2, many("B", 5))]);
  await withPage(s, async page => {
    assert.equal(await tocChips(page).count(), 2);
    assert.equal(await page.locator("#toc").isVisible(), true, "帯が見えていない");
    const before = await projHeadShown(page, P2);
    assert.ok(before && before.top > before.vh, `前提: beta の見出しが最初の画面の外にない ${JSON.stringify(before)}`);
    // 押したのと同じ処理の中で測る。見出しの下の節の見出し (h3) も、止まった見出しに隠れず見えている
    const r = await page.evaluate(p => {
      document.querySelector(`#toc .tocchip[data-path="${p}"]`).click();
      const h = [...document.querySelectorAll("h2")].find(e => e.querySelector(".path").textContent === p);
      const h3 = h.parentElement.querySelector("h3");
      return { top: h.getBoundingClientRect().top, bottom: h.getBoundingClientRect().bottom, vh: innerHeight,
        h3: h3 ? h3.getBoundingClientRect().top : null,
        head: document.querySelector("header").getBoundingClientRect().bottom, focused: document.activeElement === h };
    }, P2);
    assert.ok(Math.abs(r.top - r.head) <= 1 && r.bottom <= r.vh, `押したその場で beta の見出しがヘッダーのすぐ下に来ない ${JSON.stringify(r)}`);
    assert.ok(r.h3 !== null && r.h3 >= r.bottom, `飛んだ先で、見出しの下の「判断 (N)」が止まった見出しの下に隠れる ${JSON.stringify(r)}`);
    assert.ok(r.focused, "フォーカスが見出しに移らない");
    const shown = await projHeadShown(page, P2);
    assert.ok(shown && shown.visible && shown.text.startsWith("beta"), `beta の見出しが見えていない ${JSON.stringify(shown)}`);
    // 上のプロジェクトへも戻れる (帯はスクロールで流れている。Playwright が帯まで戻してから押す)
    await chipOf(page, P1).click();
    const a = await projHeadShown(page, P1);
    assert.ok(a && Math.abs(a.top - a.head) <= 1 && a.visible, `alpha の見出しがヘッダーのすぐ下に来ない ${JSON.stringify(a)}`);
  }, { reducedMotion: "reduce" });
});

test("動きを減らす設定でなければ、チップを押すと滑らかにスクロールして、見出しがヘッダーのすぐ下に止まる", async () => {
  const s = new Script([proj(P1, many("A", 5)), proj(P2, many("B", 5))]);
  await withPage(s, async page => {
    assert.equal(await tocChips(page).count(), 2);
    assert.equal(await page.locator("#toc").isVisible(), true, "帯が見えていない");
    const r0 = await page.evaluate(p => {
      document.querySelector(`#toc .tocchip[data-path="${p}"]`).click();
      return [...document.querySelectorAll("h2")].find(e => e.querySelector(".path").textContent === p).getBoundingClientRect().top;
    }, P2);
    const hb = await headBottom(page);
    assert.ok(r0 - hb > 100, `押したその場で移った (滑らかでない): ${r0} / ヘッダーの下 ${hb}`);
    await until(async () => { const b = await projHeadShown(page, P2); return Math.abs(b.top - b.head) <= 1; }, 4000, "beta の見出しがヘッダーのすぐ下に止まる");
  });
});

test("長いページをスクロールしても、今いるプロジェクトの見出し (名前) がヘッダーのすぐ下に見えている。下まで行けば最後のプロジェクトの見出しになる", async () => {
  const s = new Script([proj(P1, many("A", 4)), proj(P2, many("B", 4))]);
  await withPage(s, async page => {
    for (const [w, h] of [[1280, 720], [375, 700]]) {
      await page.setViewportSize({ width: w, height: h });
      await frames(page);
      await scrollCardTo(page, "t-A3", 200);                                           // alpha のカードの途中
      const a = await projHeadShown(page, P1), b = await projHeadShown(page, P2);
      assert.ok(a && b, `幅 ${w}: 見出しが無い`);
      assert.ok(Math.abs(a.top - a.head) <= 1 && a.visible && a.text.startsWith("alpha"), `幅 ${w}: alpha の途中で alpha の見出しが見えない ${JSON.stringify(a)}`);
      assert.ok(b.top > a.bottom + 100, `幅 ${w}: beta の見出しが上に来ている ${JSON.stringify(b)}`);
      // 止まった見出しは、下を流れるカードが透けない背景を持ち、動いている (transform の付いた) カードより上に描かれる
      const bg = await page.evaluate(() => [getComputedStyle(document.querySelector("h2")).backgroundColor, getComputedStyle(document.body).backgroundColor]);
      assert.equal(bg[0], bg[1], `幅 ${w}: 見出しの背景がページの背景でない`);
      await page.evaluate(() => { for (const c of document.querySelectorAll("section.card")) c.style.transform = "translateX(0px)"; });
      const moving = await projHeadShown(page, P1);
      assert.ok(moving && moving.visible, `幅 ${w}: 動いているカードが止まった見出しの上に描かれる`);
      await page.evaluate(() => { for (const c of document.querySelectorAll("section.card")) c.style.transform = ""; });
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));   // いちばん下 (beta の最後のカード)
      const a2 = await projHeadShown(page, P1), b2 = await projHeadShown(page, P2);
      assert.ok(a2 && b2, `幅 ${w}: 下までスクロールしたら見出しが無い`);
      assert.ok(Math.abs(b2.top - b2.head) <= 1 && b2.visible && b2.text.startsWith("beta"), `幅 ${w}: 下まで行くと beta の見出しが見えない ${JSON.stringify(b2)}`);
      assert.ok(!a2.visible && a2.bottom <= a2.head + 1, `幅 ${w}: beta の中で alpha の見出しが残っている ${JSON.stringify(a2)}`);
    }
  });
});

test("ヘッダーの高さが変わると (幅が狭くなって「(古いデータ)」などで折り返す)、見出しが止まる位置もヘッダーのすぐ下に合わせて動く", async () => {
  const s = new Script([proj(P1, many("A", 5))]);
  await withPage(s, async page => {
    const h0 = await headBottom(page);
    await failLoad(page, s);
    await failLoad(page, s);                                                           // 更新時刻に「(古いデータ)」が付く
    s.itemsFail = false;
    await page.setViewportSize({ width: 320, height: 700 });
    const h1 = await headBottom(page);
    assert.ok(h1 > h0 + 10, `前提: ヘッダーが折り返していない (${h0} → ${h1})`);
    await scrollCardTo(page, "t-A3", 200);
    await until(async () => { const a = await projHeadShown(page, P1); return Math.abs(a.top - a.head) <= 1 && a.visible; }, 2000, "見出しが新しいヘッダーのすぐ下に止まる");
  });
});

test("止まった見出しの下に隠れたカードにフォーカスが移ると、カードの上端は止まった見出しの下に出る (scroll-margin-top)", async () => {
  const s = new Script([proj(P1, many("A", 5))]);
  await withPage(s, async page => {
    await scrollCardTo(page, "t-A3", 12);                                              // カードの上端がヘッダーの 12px 下 (止まった見出しの下)
    const r = await page.evaluate(() => {
      const c = [...document.querySelectorAll("section.card")].find(e => e.querySelector(".id").textContent === "t-A3");
      const h2 = document.querySelector("h2");
      const before = c.getBoundingClientRect().top, h3Bottom = h2.getBoundingClientRect().bottom;
      c.focus();
      return { before, h3Bottom, after: c.getBoundingClientRect().top, h3After: h2.getBoundingClientRect().bottom, focused: document.activeElement === c };
    });
    assert.ok(r.focused && r.before < r.h3Bottom, `前提: カードの上端が止まった見出しに隠れていない ${JSON.stringify(r)}`);
    assert.ok(r.after >= r.h3After, `フォーカスが移ったカードの上端が、止まった見出しの下に隠れる ${JSON.stringify(r)}`);
  });
});

test("キーボードでチップを押すと、フォーカスは見出しへ移り、次の Tab はそのプロジェクトの最初のカードへ進む (帯へ戻ってページが上に戻らない)", async () => {
  const s = new Script([proj(P1, many("A", 4)), proj(P2, many("B", 4))]);
  await withPage(s, async page => {
    assert.equal(await tocChips(page).count(), 2);
    assert.equal(await page.locator("#toc").isVisible(), true, "帯が見えていない");
    await chipOf(page, P2).focus();
    await page.keyboard.press("Enter");
    assert.ok(await projHeadOf(page, P2).evaluate(e => e === document.activeElement), "フォーカスが beta の見出しに無い");
    // キーボードで移ったときは、フォーカスの目印 (アクセントの実線の枠) が見出しに出る
    assert.deepEqual(await projHeadOf(page, P2).evaluate(e => [e.matches(":focus-visible"), getComputedStyle(e).outlineStyle]), [true, "solid"]);
    const y0 = await page.evaluate(() => scrollY);
    await page.keyboard.press("Tab");
    const r = await page.evaluate(() => {
      const c = document.activeElement.closest("section.card");
      return { id: c ? c.querySelector(".id").textContent : null, y: scrollY };
    });
    assert.equal(r.id, "t-B1", "Tab の行き先が beta の最初のカードでない");
    assert.ok(r.y >= y0 - 1, `Tab でページが上に戻った (${y0} → ${r.y})`);
  }, { reducedMotion: "reduce" });
});

test("幅 375 では、プロジェクトが多く名前とパスが長くても、ページは横にスクロールしない。帯は 1 行で帯の中だけ横にスクロールし、見出しは 1 行に収まって短い名前は切れない", async () => {
  const longName = "とても長いプロジェクトの名前".repeat(8);
  const paths = Array.from({ length: 8 }, (_, i) =>
    `/Users/someone/dev/some-long-directory-name/${"nested-folder/".repeat(3)}${i === 0 ? longName : `project-${i}`}`);
  const s = new Script(paths.map((p, i) => proj(p, [item(`t-${i}`)])));
  const measure = page => page.evaluate(() => {
    const inner = document.getElementById("tocin");
    const chips = inner ? [...inner.children] : [];
    return { sw: document.scrollingElement.scrollWidth, iw: innerWidth, n: chips.length,
      rows: new Set(chips.map(c => Math.round(c.getBoundingClientRect().top))).size,
      chipH: Math.max(0, ...chips.map(c => c.getBoundingClientRect().height)),
      chipW: Math.max(0, ...chips.map(c => c.getBoundingClientRect().width)), bandW: inner ? inner.clientWidth : 0,
      nameInside: chips.every(c => { const n = c.querySelector(".tocname"); return !!n && n.getBoundingClientRect().right <= c.getBoundingClientRect().right + 0.5; }),
      scrollable: !!inner && inner.scrollWidth > inner.clientWidth,
      h3s: [...document.querySelectorAll("h2")].map(h => {
        const name = h.querySelector(".pname");
        return { h: h.getBoundingClientRect().height, cut: !!name && name.scrollWidth > name.clientWidth + 1, long: h.textContent.includes("とても長い") };
      }) };
  });
  await withPage(s, async page => {
    const m = await measure(page);
    assert.equal(m.n, 8, "チップが 8 つない");
    assert.ok(m.sw <= m.iw, `幅 375 でページが横にスクロールする (${m.sw} > ${m.iw})`);
    assert.equal(m.rows, 1, "帯が 1 行でない");
    assert.ok(m.chipH > 0 && m.chipH <= 32, `チップが 1 行でない (高さ ${m.chipH}px。長い名前は … で切る)`);
    assert.ok(m.chipW <= m.bandW, `長い名前のチップが帯の幅より広い (${m.chipW} > ${m.bandW})`);
    assert.ok(m.nameInside, "チップの名前がチップの枠からはみ出す");
    assert.ok(m.scrollable, "帯の中が横にスクロールしない (チップが収まっている: 台本の前提)");
    assert.equal(m.h3s.length, 8);
    for (const h of m.h3s) {
      assert.ok(h.h <= 40, `見出しが 1 行に収まらない (${h.h}px)`);
      if (!h.long) assert.equal(h.cut, false, "短い名前が切れている (パスを先に縮めていない)");
    }
    // 帯の中を横にスクロールした先の最後のチップも押せて、そのプロジェクトの見出しがヘッダーの下・画面の中に来る
    await chipOf(page, paths[7]).click();
    const last = await projHeadShown(page, paths[7]);
    assert.ok(last && last.top >= last.head - 1 && last.bottom <= last.vh && last.visible, `最後のプロジェクトへ飛べない ${JSON.stringify(last)}`);
    // 幅の広い画面では帯は折り返し、長い名前のチップも帯の幅に収まる (ページが横にスクロールしない)
    await page.setViewportSize({ width: 1280, height: 720 });
    const w = await measure(page);
    assert.ok(w.sw <= w.iw, `幅 1280 でページが横にスクロールする (${w.sw} > ${w.iw})`);
    assert.ok(w.rows >= 2, "幅 1280 で帯が折り返していない");
    assert.ok(w.chipH > 0 && w.chipH <= 32, `幅 1280 でチップが 1 行でない (高さ ${w.chipH}px)`);
    assert.ok(w.chipW <= w.bandW && w.nameInside, `幅 1280 で長い名前のチップが帯からはみ出す (${w.chipW} > ${w.bandW})`);
  }, { viewport: { width: 375, height: 700 }, reducedMotion: "reduce" });
});

// ---- 0.10.0: アクセシビリティ (見出しの階層・名前・押した状態・ID のコピー・コントラスト) -----------------------------------
// 見出しは h1 (画面) → h2 (プロジェクト) → h3 (節)。カードは問いで名前が付き、選択肢は押した状態を持つ。
// ID はキーボードで届くボタン。トーストの読み上げは入れ物の aria-live だけに任せる。枠と文字の色は WCAG の比を満たす

test("見出しは h1 → h2 (プロジェクト) → h3 (節) の順で、文言と件数は変わらない。カードは問いで名前が付く", async () => {
  const s = new Script([proj(P1, [item("t-A1")]), proj(P2, [item("t-B2"), item("t-C3", { q: null })], [answeredItem("t-D4")])]);
  await withPage(s, async page => {
    const heads = await page.$$eval("h1, h2, h3", es => es.map(e => ({ level: Number(e.tagName[1]), text: e.textContent })));
    assert.deepEqual(heads.map(h => h.level), [1, 2, 3, 2, 3, 3], JSON.stringify(heads));
    assert.deepEqual(heads.filter(h => h.level === 3).map(h => h.text), ["判断 (1)", "判断 (2)", "回答済み・反映待ち (1)"]);
    assert.deepEqual(await page.$$eval("h2 .pname", es => es.map(e => e.textContent)), ["alpha", "beta"]);
    assert.equal(await page.locator(".proj > h2").count(), 2);
    // 節の見出しは各節の入れ物 (.grp) の先頭。回答済みの節は details で、見出しはその summary の中 (.grp > summary > h3)
    assert.equal(await page.locator(".proj .grp > h3, .proj .grp > summary > h3").count(), 3);
    // カードの名前: 問いがあれば問い、問いが無ければタイトル、回答済みはタイトル (回答済みの節は畳んであり、畳んだ中のカードは名前を引けないので開く)
    await openAnswered(page);
    assert.equal(await page.getByRole("region", { name: "ask t-A1" }).count(), 1);
    assert.equal(await page.getByRole("region", { name: "ask t-B2" }).count(), 1);
    assert.equal(await page.getByRole("region", { name: "title t-C3" }).count(), 1);
    assert.equal(await page.getByRole("region", { name: "title t-D4" }).count(), 1);
    const ids = await page.$$eval("section.card", cs => cs.map(c => c.getAttribute("aria-labelledby")));
    assert.equal(ids.length, 4);
    assert.equal(new Set(ids).size, 4, "カードの名前の id が重なる");
    // 見た目は見出しの役に付いている: プロジェクト (h2) は大きく sticky、節 (h3) は小さく sticky でない
    const looks = await page.evaluate(() => ["h2", "h3"].map(t => { const c = getComputedStyle(document.querySelector(t)); return [c.fontSize, c.position]; }));
    assert.deepEqual(looks, [["16px", "sticky"], ["14px", "static"]]);
  });
});

test(".opt は aria-pressed を持ち、押すたびに true と false が切り替わる。ほかの選択肢を押せば移る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    const opts = qcard(page, "t-A1").locator(".opt");
    assert.equal(await opts.count(), 2);
    const states = () => opts.evaluateAll(es => es.map(e => e.getAttribute("aria-pressed")));
    assert.deepEqual(await states(), ["false", "false"]);
    await opts.nth(0).click();
    assert.deepEqual(await states(), ["true", "false"]);
    await opts.nth(0).click();
    assert.deepEqual(await states(), ["false", "false"]);
    await opts.nth(0).click();
    await opts.nth(1).click();
    assert.deepEqual(await states(), ["false", "true"]);
    assert.deepEqual(await opts.evaluateAll(es => es.map(e => e.classList.contains("sel"))), [false, true], "見た目の選択と aria-pressed が食い違う");
  });
});

test("読み込み直した画面でも、下書きで選んだ選択肢は aria-pressed=true で出る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    await page.reload();
    await ready(page);
    assert.deepEqual(await qcard(page, "t-A1").locator(".opt").evaluateAll(es => es.map(e => e.getAttribute("aria-pressed"))), ["false", "true"]);
  });
});

test("textarea に空でない aria-label がある。選択肢があれば「補足」、無ければ「回答」", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2", { q: null }),
    item("t-C3", { q: { kind: "task", ask: "ask t-C3", context: "", recommended: "", options: [] } })])]);
  await withPage(s, async page => {
    const label = id => qcard(page, id).locator("textarea").getAttribute("aria-label");
    assert.equal(await label("t-A1"), "補足");
    assert.equal(await label("t-B2"), "回答");
    assert.equal(await label("t-C3"), "回答");
  });
});

// .copied の文字が want になるのを待つ。待ちが尽きたら、時間切れの例外ではなく assertion で落とす
async function copiedIs(page, want, ms, what) {
  await waitFor(async () => (await page.locator(".copied").innerText()) === want, ms, what).catch(() => {});
  assert.equal(await page.locator(".copied").innerText(), want, what);
}

test(".id はボタンで Tab で届き、Enter でクリップボードに id が入って「コピーしました」が約 1.5 秒出る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    const idBtn = qcard(page, "t-A1").locator(".id");
    assert.deepEqual(await idBtn.evaluate(e => [e.tagName, e.type]), ["BUTTON", "button"]);
    assert.equal(await idBtn.innerText(), "t-A1", "ボタンの文字が ID そのものでない");
    // Tab だけで届く (先頭から数えて十分な回数のうちに)
    let reached = false;
    for (let i = 0; i < 20 && !reached; i++) {
      await page.keyboard.press("Tab");
      reached = await page.evaluate(() => document.activeElement.matches("section.card .id"));
    }
    assert.ok(reached, ".id に Tab で届かない");
    assert.equal(await page.locator(".copied").innerText(), "", "押す前から知らせが出ている");
    await page.keyboard.press("Enter");
    await copiedIs(page, "コピーしました", 2000, "「コピーしました」");
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "t-A1");
    await copiedIs(page, "", 3000, "知らせが消える");
    assert.equal(await idBtn.innerText(), "t-A1", "知らせで ID の文字が変わった");
  }, { permissions: ["clipboard-read", "clipboard-write"] });
});

test("クリップボードに書けないときは「コピーできませんでした」と出る", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    await page.evaluate(() => { Object.defineProperty(navigator, "clipboard", { value: { writeText: () => Promise.reject(new Error("denied")) }, configurable: true }); });
    await qcard(page, "t-A1").locator(".id").click();
    await copiedIs(page, "コピーできませんでした", 2000, "失敗の知らせ");
  });
});

test(".toast に role が無く、読み上げは入れ物 (#toasts) の aria-live だけ", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    assert.equal(await page.locator("#toasts").getAttribute("aria-live"), "polite");
    await page.evaluate(() => { toast("失敗の知らせ"); toast("成功の知らせ", { kind: "ok", action: { label: "取り消す", run: () => {} } }); });
    assert.equal(await page.locator(".toast").count(), 2);
    assert.deepEqual(await page.locator(".toast").evaluateAll(es => es.map(e => e.hasAttribute("role"))), [false, false]);
    assert.equal(await page.locator("#toasts [role]").count(), 0);
  });
});

for (const scheme of ["light", "dark"]) {
  test(`${scheme}: .opt と textarea の枠は背景 (自身の背景とカードの背景) に対して 3:1 以上。P1 の文字はカードの背景に対して 4.5:1 以上`, async () => {
    const s = new Script([proj(P1, [item("t-A1")])]);
    await withPage(s, async page => {
      await page.emulateMedia({ colorScheme: scheme });
      const m = await page.evaluate(() => {
        const card = document.querySelector("section.card:has(textarea)");
        const cs = e => getComputedStyle(e);
        const part = e => ({ bd: cs(e).borderTopColor, bg: cs(e).backgroundColor, bw: cs(e).borderTopWidth });
        return { card: cs(card).backgroundColor, opt: part(card.querySelector(".opt:not(.rec)")), area: part(card.querySelector("textarea")),
          p1: { fg: cs(card.querySelector(".prio.p1")).color }, line: cs(card).borderTopColor };
      });
      const calc = await page.evaluate(([fnSrc, m]) => {
        const ratio = eval(`(${fnSrc})()`);
        return { optOwn: ratio(m.opt.bd, m.opt.bg), optCard: ratio(m.opt.bd, m.card), areaOwn: ratio(m.area.bd, m.area.bg),
          areaCard: ratio(m.area.bd, m.card), p1: ratio(m.p1.fg, m.card) };
      }, [RATIO_FN.toString(), m]);
      assert.ok(parseFloat(m.opt.bw) > 0 && parseFloat(m.area.bw) > 0, "枠が引かれていない");
      assert.ok(calc.optOwn >= 3, `${scheme} .opt の枠 / 自身の背景 ${calc.optOwn.toFixed(2)}`);
      assert.ok(calc.optCard >= 3, `${scheme} .opt の枠 / カードの背景 ${calc.optCard.toFixed(2)}`);
      assert.ok(calc.areaOwn >= 3, `${scheme} textarea の枠 / 自身の背景 ${calc.areaOwn.toFixed(2)}`);
      assert.ok(calc.areaCard >= 3, `${scheme} textarea の枠 / カードの背景 ${calc.areaCard.toFixed(2)}`);
      assert.ok(calc.p1 >= 4.5, `${scheme} .prio.p1 の文字 / カードの背景 ${calc.p1.toFixed(2)}`);
      // 区切り線 (--line) は変えていない: カードの枠は操作部品の枠と別の色
      assert.notEqual(m.line, m.opt.bd, `${scheme} カードの枠が操作部品の枠と同じ色になった (--line を変えた)`);
    });
  });
}

// ---- 絞り込みと検索 ------------------------------------------------------------------------------

// 3 プロジェクト × 判断待ち 12 件 (計 36 件) と、各プロジェクトに回答済み 1 件。
// 件 i (1〜12) の性質: 優先度 (i + プロジェクトの番号) % 4 / 種別 6 の倍数は task・i % 6 === 5 は問い無し・ほかは decision /
// ラベル 3 の倍数は area:ui・4 の倍数は needs-review / 7 番の背景に "Widget Factory" / 9 番の選択肢の label に "Rollback plan" /
// 3 番の選択肢の desc にだけ "hidden-desc-only" (desc は検索の対象外)
const FPATHS = ["/work/alpha", "/work/beta", "/work/gamma"];
function manyScript() {
  const projects = FPATHS.map((path, pi) => {
    const tag = path.split("/").pop();
    const human = Array.from({ length: 12 }, (_, k) => {
      const i = k + 1;
      const kind = i % 6 === 0 ? "task" : i % 6 === 5 ? "none" : "decision";
      const base = item(`${tag[0]}-${String(i).padStart(2, "0")}`, {
        title: `${tag} thing ${i}`, priority: (i + pi) % 4,
        labels: [i % 3 === 0 ? "area:ui" : null, i % 4 === 0 ? "needs-review" : null].filter(Boolean),
      });
      const q = kind === "none" ? null : { ...base.q, kind, ask: `Ask ${tag} number ${i}`,
        context: i === 7 ? "Mentions the Widget Factory" : "",
        options: [{ key: "a", label: i === 9 ? "Use Rollback plan" : "案A", desc: i === 3 ? "hidden-desc-only" : "" },
                  { key: "b", label: "案B", desc: "" }] };
      return { ...base, q };
    });
    return proj(path, human, [answeredItem(`${tag[0]}-ans`)]);
  });
  return new Script(projects);
}
const FALL = s => s.projects.flatMap(p => p.human.map(i => ({ ...i, project: p.path })));
const AALL = s => s.projects.flatMap(p => p.answered.map(i => ({ ...i, project: p.path })));
const pcards = page => page.locator("#main section.card:has(textarea)");
const acards = page => page.locator("#main section.card:has(.ans)");
const lastNum = i => Number(i.title.split(" ").pop());

// 数えた結果を、画面と同じ述語に頼らず、台本の表から別に数える
const FILTER_CASES = [
  { name: "プロジェクト beta", set: { project: "/work/beta" }, pred: i => i.project === "/work/beta", want: 12 },
  { name: "種別 decision", set: { kind: "decision" }, pred: i => i.q !== null && i.q.kind === "decision", want: 24 },
  { name: "種別 task", set: { kind: "task" }, pred: i => i.q !== null && i.q.kind === "task", want: 6 },
  { name: "種別 問いなし", set: { kind: "none" }, pred: i => i.q === null, want: 6 },
  { name: "優先度 P0", set: { prio: "0" }, pred: i => i.priority === 0, want: 9 },
  { name: "優先度 P3", set: { prio: "3" }, pred: i => i.priority === 3, want: 9 },
  { name: "ラベル area:ui", set: { label: "area:ui" }, pred: i => i.labels.includes("area:ui"), want: 12 },
  { name: "ラベル needs-review", set: { label: "needs-review" }, pred: i => i.labels.includes("needs-review"), want: 9 },
  { name: "検索: 題", set: { q: "alpha" }, pred: i => i.title.includes("alpha"), want: 12 },
  { name: "検索: 題 (部分一致)", set: { q: "thing 1" }, pred: i => [1, 10, 11, 12].includes(lastNum(i)), want: 12 },
  { name: "検索: 問い (ask)", set: { q: "number 4" }, pred: i => i.q !== null && i.q.ask.endsWith("number 4"), want: 3 },
  { name: "検索: 背景 (大文字小文字を区別しない)", set: { q: "WIDGET factory" }, pred: i => i.q !== null && i.q.context.includes("Widget Factory"), want: 3 },
  { name: "検索: 選択肢の label", set: { q: "rollback" }, pred: i => i.q !== null && i.q.options.some(o => o.label.includes("Rollback")), want: 3 },
  { name: "検索: 選択肢の desc は対象外", set: { q: "hidden-desc-only" }, pred: () => false, want: 0 },
  { name: "検索: ID", set: { q: "b-07" }, pred: i => i.id === "b-07", want: 1 },
  { name: "検索: ID (大文字)", set: { q: "B-07" }, pred: i => i.id === "b-07", want: 1 },
  { name: "検索: 前後の空白は無視", set: { q: "  b-07  " }, pred: i => i.id === "b-07", want: 1 },
  { name: "検索: 該当なし", set: { q: "zzz-none" }, pred: () => false, want: 0 },
  { name: "組み合わせ: beta × task", set: { project: "/work/beta", kind: "task" }, pred: i => i.project === "/work/beta" && i.q !== null && i.q.kind === "task", want: 2 },
  { name: "組み合わせ: P1 × area:ui × 検索 gamma", set: { prio: "1", label: "area:ui", q: "gamma" },
    pred: i => i.priority === 1 && i.labels.includes("area:ui") && i.title.includes("gamma"), want: 1 },
];

async function applyFilterSet(page, set) {
  if (set.project !== undefined) await page.selectOption("#fproject", set.project);
  if (set.kind !== undefined) await page.selectOption("#fkind", set.kind);
  if (set.prio !== undefined) await page.selectOption("#fprio", set.prio);
  if (set.label !== undefined) await page.selectOption("#flabel", set.label);
  if (set.q !== undefined) await page.fill("#fq", set.q);
}

test("絞り込みと検索: 30 件以上の台本で、条件ごとに判断待ち・回答済みの件数が正しく、「N 件中 M 件を表示」が出て、ヘッダーの件数は変わらない", async () => {
  const s = manyScript();
  assert.equal(FALL(s).length, 36, "台本が 36 件でない");
  await withPage(s, async page => {
    assert.equal((await pcards(page).count()), 36);
    assert.equal(await page.locator("#count").innerText(), "36 件");
    // 何も絞っていないときは、件数の表示も「解除」も出さない
    assert.equal(await page.locator("#fcount").isVisible(), false);
    assert.equal(await page.locator("#fclear").isVisible(), false);
    for (const c of FILTER_CASES) {
      const want = FALL(s).filter(c.pred).length;
      assert.equal(want, c.want, `${c.name}: 台本の表の数え直しと手で数えた件数が違う`);
      await applyFilterSet(page, c.set);
      await waitFor(async () => (await pcards(page).count()) === want, 3000, `${c.name}: 判断待ち ${want} 件`).catch(() => {});
      assert.equal(await pcards(page).count(), want, `${c.name}: 判断待ちの件数`);
      const wantAns = AALL(s).filter(c.pred).length;
      assert.equal(await acards(page).count(), wantAns, `${c.name}: 回答済みの件数`);
      assert.equal(await page.locator("#fcount").innerText(), `36 件中 ${want} 件を表示`, `${c.name}: 件数の表示`);
      assert.equal(await page.locator("#fclear").isVisible(), true, `${c.name}: 解除のボタン`);
      assert.equal(await page.locator("#count").innerText(), "36 件", `${c.name}: ヘッダーの件数は全件のまま`);
      assert.equal(await page.title(), "(36) 判断待ち", `${c.name}: タイトルの件数は全件のまま`);
      await page.click("#fclear");
      assert.equal(await pcards(page).count(), 36, `${c.name}: 解除で全件に戻る`);
      assert.equal(await page.inputValue("#fq"), "", `${c.name}: 解除で検索欄が空に戻る`);
      assert.equal(await page.locator("#fcount").isVisible(), false);
    }
  });
});

test("絞り込み: 全部隠れたら「絞り込みに合うものはありません」を出し、解除で戻る。隠れたプロジェクトの見出しと目次のチップは出さない", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await page.fill("#fq", "zzz-none");
    assert.equal(await pcards(page).count(), 0);
    assert.match(await page.locator("#main .empty").innerText(), /絞り込みに合う/);
    assert.equal(await page.locator("h2").count(), 0);
    assert.equal(await page.locator("#toc").isHidden(), true);
    assert.equal(await page.locator("#count").innerText(), "36 件");
    await page.fill("#fq", "b-07");
    assert.deepEqual(await page.locator("h2 .pname").allInnerTexts(), ["beta"]);
    assert.deepEqual(await page.locator(".tocchip").evaluateAll(es => es.map(e => e.dataset.path)), ["/work/beta"]);
    await page.click("#fclear");
    assert.deepEqual(await page.locator("h2 .pname").allInnerTexts(), ["alpha", "beta", "gamma"]);
    assert.equal(await page.locator("#main .empty").count(), 0);
  });
});

test("絞り込み: 検索の入力中に 15 秒ごとの読み込み (load) が来ても、入力・フォーカス・絞り込みが保たれ、新しい一致が加わる", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await page.locator("#fq").click();
    await page.keyboard.type("widget");
    assert.equal(await pcards(page).count(), 3);
    await page.evaluate(() => { document.getElementById("fq").__mark = "same-node"; });
    s.projects[1].human[0].title = "beta renamed Widget";       // 読み込みの間に、一致する題が 1 件増える
    s.projects[2].human[6].q.context = "no longer";            // 一致していた背景が 1 件消える
    await page.evaluate(() => load());
    await waitFor(async () => (await page.locator("#meta").innerText()).includes("更新"), 3000, "読み込み");
    await waitFor(async () => (await pcards(page).count()) === 3, 3000, "読み込み後も 3 件 (増 1・減 1)").catch(() => {});
    assert.equal(await pcards(page).count(), 3);
    assert.deepEqual(await pcards(page).locator(".id").allInnerTexts().then(a => a.sort()), ["a-07", "b-01", "b-07"]);
    assert.equal(await page.inputValue("#fq"), "widget", "入力した文字が消えた");
    assert.equal(await page.evaluate(() => document.getElementById("fq").__mark), "same-node", "検索欄が作り直された");
    assert.equal(await page.evaluate(() => document.activeElement.id), "fq", "検索欄のフォーカスが外れた");
    await page.keyboard.type("x");                              // 読み込み後もそのまま打ち続けられる
    assert.equal(await page.inputValue("#fq"), "widgetx");
    assert.equal(await pcards(page).count(), 0);
  });
});

test("絞り込み: 選んでいる値が読み込みで変わらず、選択肢の一覧が変わっても選びが保たれる。データから消えた値は「該当なし」で残り、解除できる", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await page.selectOption("#flabel", "needs-review");
    // 選択肢の一覧は全文で、種別・優先度の一覧は決まった順と文言で出る
    assert.deepEqual(await page.locator("#flabel option").allInnerTexts(), ["すべてのラベル", "area:ui", "needs-review"]);
    assert.deepEqual(await page.locator("#fprio option").allInnerTexts(), ["すべての優先度", "P0", "P1", "P2", "P3"]);
    assert.deepEqual(await page.locator("#fkind option").allInnerTexts(), ["すべての種別", "判断 (decision)", "作業・確認 (task)", "問いなし"]);
    assert.deepEqual(await page.locator("#fproject option").allInnerTexts(), ["すべてのプロジェクト", "alpha", "beta", "gamma"]);
    await page.evaluate(() => { document.querySelector("#flabel option:nth-child(2)").__mark = "same-node"; });
    await page.evaluate(() => load());                                // 一覧が変わらないときは、選択肢を作り直さない (開いている一覧を閉じない)
    assert.equal(await page.evaluate(() => document.querySelector("#flabel option:nth-child(2)").__mark), "same-node");
    for (const p of s.projects) for (const i of p.human) i.labels = i.labels.filter(l => l !== "needs-review");   // ラベルがデータから消えた
    await page.evaluate(() => load());
    await waitFor(async () => (await page.locator("#flabel option:checked").innerText()).includes("該当なし"), 3000, "選びが残る").catch(() => {});
    assert.equal(await page.locator("#flabel").inputValue(), "needs-review");
    await waitFor(async () => (await pcards(page).count()) === 0, 3000, "読み込みで一致が無くなった (畳む動きの後)").catch(() => {});
    assert.equal(await pcards(page).count(), 0);
    assert.match(await page.locator("#flabel option:checked").innerText(), /needs-review.*該当なし/);
    await page.click("#fclear");
    assert.equal(await pcards(page).count(), 36);
  });
});

test("絞り込み: URL の query (project / kind / prio / label / q) から復元され、変えると URL に出て、再読み込みしても残り、解除で query から消える (関係ない query は残す)", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    const qs = o => "?" + new URLSearchParams(o).toString();
    await page.goto(baseUrl + qs({ project: "/work/beta", kind: "decision", prio: "2", label: "area:ui", q: "beta", keep: "1" }));
    await ready(page);
    const want = FALL(s).filter(i => i.project === "/work/beta" && i.q !== null && i.q.kind === "decision" && i.priority === 2
      && i.labels.includes("area:ui") && i.title.includes("beta")).length;
    assert.equal(want, 1, "台本: 該当は 1 件のはず");
    assert.equal(await pcards(page).count(), want);
    assert.equal(await page.locator("#fcount").innerText(), `36 件中 ${want} 件を表示`);
    assert.deepEqual(await page.evaluate(() => ["fproject", "fkind", "fprio", "flabel", "fq"].map(id => document.getElementById(id).value)),
      ["/work/beta", "decision", "2", "area:ui", "beta"]);
    // 変えると URL に出る。再読み込みしても残る
    await page.selectOption("#fprio", "0");
    assert.equal(new URL(page.url()).searchParams.get("prio"), "0");
    assert.equal(new URL(page.url()).searchParams.get("keep"), "1", "関係ない query が消えた");
    await page.reload();
    await ready(page);
    assert.deepEqual(await page.evaluate(() => ["fproject", "fkind", "fprio", "flabel", "fq"].map(id => document.getElementById(id).value)),
      ["/work/beta", "decision", "0", "area:ui", "beta"]);
    const want2 = FALL(s).filter(i => i.project === "/work/beta" && i.q !== null && i.q.kind === "decision" && i.priority === 0
      && i.labels.includes("area:ui") && i.title.includes("beta")).length;
    assert.equal(await pcards(page).count(), want2, "再読み込みの後の件数");
    // 解除すると、絞り込みの query だけが消える
    await page.click("#fclear");
    assert.equal(new URL(page.url()).search, "?keep=1");
    assert.equal(await pcards(page).count(), 36);
    // 検索欄を空にしても query から消える
    await page.fill("#fq", "abc");
    assert.equal(new URL(page.url()).searchParams.get("q"), "abc");
    await page.fill("#fq", "");
    assert.equal(new URL(page.url()).searchParams.has("q"), false);
  });
});

test("絞り込み: 隠れたカードの下書きは、戻ったときに残っている。検索欄で Enter を押してもページは読み直されない", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await qcard(page, "b-03").locator(".opt").nth(1).click();
    await qcard(page, "b-03").locator("textarea").fill("memo-keep");
    await page.evaluate(() => { window.__same = "page"; });
    await page.fill("#fq", "alpha");
    assert.equal(await qcard(page, "b-03").count(), 0, "隠れていない");
    await page.press("#fq", "Enter");
    assert.equal(await page.evaluate(() => window.__same), "page", "Enter でページが読み直された");
    assert.equal(await page.locator("#main .leaving").count(), 0, "絞り込みで隠すカードに畳む動きが付いた");
    await page.click("#fclear");
    assert.equal(await page.locator("#main .anim, #main .incoming").count(), 0, "絞り込みを解除して現れるカードに入場の動きが付いた");
    assert.equal(await qcard(page, "b-03").locator("textarea").inputValue(), "memo-keep");
    assert.equal(await qcard(page, "b-03").locator(".opt").nth(1).getAttribute("aria-pressed"), "true");
  });
});

test("絞り込み: 送信に失敗して戻ったカードは、隠して戻しても失敗の表示が残る", async () => {
  const s = manyScript();
  s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
  await withPage(s, async page => {
    await answerFirst(page, "a-01");
    await waitFor(async () => (await qcard(page, "a-01").locator(".errbox").innerText()).includes("bd boom"), 3000, "失敗の表示");
    await page.fill("#fq", "beta");
    assert.equal(await qcard(page, "a-01").count(), 0);
    await page.click("#fclear");
    assert.match(await qcard(page, "a-01").locator(".errbox").innerText(), /bd boom/);
    assert.equal(await qcard(page, "a-01").evaluate(e => e.classList.contains("failed")), true);
  });
});

test("絞り込み: 操作部品は選べる値が 2 つ以上あるときだけ出る (プロジェクト 1 つ・ラベル無し・優先度 1 種類なら検索欄だけ)。URL に値があれば出る", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    assert.equal(await page.locator("#fq").isVisible(), true);
    for (const id of ["fproject", "fkind", "fprio", "flabel"]) assert.equal(await page.locator("#" + id).isVisible(), false, id);
    await page.goto(baseUrl + "?label=ghost");
    await ready(page);
    assert.equal(await page.locator("#flabel").isVisible(), true, "URL に値があるのに解除する手段が出ない");
    assert.equal(await pcards(page).count(), 0);
    await page.click("#fclear");
    assert.equal(await pcards(page).count(), 2);
  });
});

test("絞り込み: 回答済みのカードにも効き、判断待ちが 0 件の絞り込みでも回答済みは出る", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await page.selectOption("#fproject", "/work/gamma");
    await openAnswered(page);                                            // 回答済みのカードの ID の文字を読むので、節を開く
    assert.deepEqual(await acards(page).locator(".id").allInnerTexts(), ["g-ans"]);
    await page.fill("#fq", "g-ans");
    assert.equal(await pcards(page).count(), 0);
    assert.deepEqual(await acards(page).locator(".id").allInnerTexts(), ["g-ans"]);
    assert.equal(await page.locator("#fcount").innerText(), "36 件中 0 件を表示");
    await page.fill("#fq", "a-ans");
    assert.equal(await acards(page).count(), 0, "別のプロジェクトの回答済みは出ない");
  });
});

test("絞り込み: 検索語は項目をまたいで一致しない (ID の末尾と題の頭をつなげた語は 0 件)。URL の q に改行があっても同じ", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await page.goto(baseUrl + "?" + new URLSearchParams({ q: "b-07\nbeta thing 7" }).toString());
    await ready(page);
    assert.equal(await pcards(page).count(), 0, "ID と題を改行でつないだ語が一致した");
    await page.goto(baseUrl + "?" + new URLSearchParams({ q: "beta thing 7" }).toString());
    await ready(page);
    assert.deepEqual(await pcards(page).locator(".id").allInnerTexts(), ["b-07"]);
  });
});

test("絞り込み: 判断待ちも回答済みも無いときは帯を出さない。絞り込みが URL にあれば出す (解除できるように)", async () => {
  const s = new Script([proj(P1, [], [])]);
  await withPage(s, async page => {
    assert.equal(await page.locator("#filters").isHidden(), true);
    await page.goto(baseUrl + "?q=abc");
    await ready(page);
    assert.equal(await page.locator("#filters").isVisible(), true);
    assert.equal(await page.locator("#fq").inputValue(), "abc");
    await page.click("#fclear");
    assert.equal(await page.locator("#filters").isHidden(), true);
  });
});

test("絞り込み: 判断待ちなしのプロジェクトの文は絞り込み中は出さない。目次のチップの件数は表示中の件数。読めなかったプロジェクトのバナーは、別のプロジェクトを選んだときだけ隠れる", async () => {
  const s = manyScript();
  s.projects.push(proj("/work/idle", [], []), proj("/work/broken", [], [], false));
  await withPage(s, async page => {
    const idle = page.locator("#main .empty");
    assert.match(await idle.innerText(), /ほか 1 プロジェクトは判断待ちなし/);
    assert.equal(await page.locator(".banner").count(), 1);
    assert.deepEqual(await page.locator(".tocchip .tocn").allInnerTexts(), ["12", "12", "12"]);
    await page.selectOption("#fkind", "task");
    assert.equal(await idle.count(), 0, "絞り込み中に「ほか N プロジェクトは判断待ちなし」が出た");
    assert.deepEqual(await page.locator(".tocchip .tocn").allInnerTexts(), ["2", "2", "2"], "チップの件数が表示中の件数でない");
    assert.equal(await page.locator(".banner").count(), 1, "ほかの絞り込みで、読めなかったプロジェクトのバナーが隠れた");
    assert.deepEqual(await page.locator("#fproject option").allInnerTexts(), ["すべてのプロジェクト", "alpha", "beta", "gamma", "broken"],
      "読めなかったプロジェクトが選択肢に無い (idle は項目が無いので出ない)");
    await page.selectOption("#fproject", "/work/beta");
    assert.equal(await page.locator(".banner").count(), 0, "別のプロジェクトを選んでいるのにバナーが出た");
    assert.deepEqual(await page.locator(".tocchip .tocn").allInnerTexts(), ["2"]);
    await page.selectOption("#fproject", "/work/broken");
    assert.equal(await page.locator(".banner").count(), 1, "そのプロジェクトを選んだのにバナーが出ない");
    await page.click("#fclear");
    assert.match(await idle.innerText(), /ほか 1 プロジェクトは判断待ちなし/);
  });
});

// ---- 書き込めないプロジェクト (writable:false。br を読みだけで返す 0.13〜0.14 のサーバー) ------------------------------

test("書き込めないプロジェクト (br) のカードは、問いと選択肢を見せ、回答・取り消しの部品の代わりに 1 行の案内を出す。書き込めるかが変われば作り直す", async () => {
  const BR = "/work/rusty";
  const RO_NOTE = "このプロジェクト (br) は画面からはまだ回答できません";
  const roq = item("r-1", { q: { ...item("r-1").q, context: "背景の 1 行",
    options: [{ key: "a", label: "【応急】案A", desc: "説明A" }, { key: "b", label: "案B", desc: "" }] } });
  // null は key を出さない (writable・cli を返さない古いサーバーの形)
  const brProj = (writable, cli = "br") => ({ ...proj(BR, [roq, item("r-3", { q: null })], [answeredItem("r-2")]),
    ...(writable === null ? {} : { writable }), ...(cli === null ? {} : { cli }) });
  const s = new Script([{ ...proj(P1, [item("t-1")], [answeredItem("t-2")]), cli: "bd", writable: true }, brProj(false)]);
  const card = (page, id) => page.locator(`section.card:has(.id:text-is("${id}"))`);
  // 待っても満たされないのは検出 (assertion の失敗) として数える
  const until = async (fn, what) => { try { await waitFor(fn, 3000, what); } catch (e) { assert.fail(e.message); } };
  // 要素がちょうど 1 つあることを確かめてから文字を読む (無い要素の innerText は待ちの時間切れになり、assertion の失敗にならない)
  const text1 = async (loc, what) => { assert.equal(await loc.count(), 1, `${what} がちょうど 1 つでない`); return loc.innerText(); };
  const ro = async page => {
    await openAnswered(page);                                           // 回答済みのカードの本文とボタンを読むので、節を開く (開いた節は押さない)
    for (const id of ["r-1", "r-2", "r-3"]) {
      const c = card(page, id);
      assert.equal(await c.count(), 1, `${id} のカードが 1 枚でない`);
      assert.equal(await c.locator(".opt, textarea, button.primary, button.ghost").count(), 0, `${id} に回答・取り消しの部品がある`);
      assert.deepEqual(await c.locator(".ro").allInnerTexts(), [RO_NOTE], `${id} に案内の 1 行が無い`);
    }
    const q = card(page, "r-1");
    assert.equal(await text1(q.locator(".ask"), "問い"), "ask r-1");
    assert.equal(await text1(q.locator(".ctx"), "背景"), "背景の 1 行");
    assert.equal(await q.locator(".roopts li").count(), 2, "選択肢が押せない一覧で出ていない");
    assert.equal(await text1(q.locator(".roopts li.rec .badge"), "推奨の印"), "推奨");
    assert.equal(await text1(q.locator(".roopts li.rec .depth"), "深さのラベルのチップ"), "応急");
    assert.match(await text1(q.locator(".roopts li.rec"), "推奨の選択肢"), /案A\s*説明A/);
    assert.equal(await text1(card(page, "r-3").locator(".ctx"), "問いの無いカードの文"), "問いがまだ整理されていません。");
    assert.match(await text1(card(page, "r-2").locator(".ans"), "回答の本文"), /^回答: 案A/);
    // 書き込めるプロジェクトのカードは今までどおり (セレクタも変えていない)
    assert.equal(await qcard(page, "t-1").locator(".opt").count(), 2);
    assert.equal(await qcard(page, "t-1").locator("button.primary").count(), 1);
    assert.equal(await text1(acard(page, "t-2").locator("button.ghost"), "取り消しのボタン"), "取り消して判断待ちに戻す");
    assert.equal(await page.locator(".ro").count(), 3, "書き込めるプロジェクトに案内が出た");
  };
  await withPage(s, async page => {
    await ro(page);
    assert.equal(await page.locator("#count").innerText(), "3 件", "書き込めないプロジェクトの判断待ちも数える");
    // 書き込めるようになった (writable:true と、writable を返さない古いサーバー) → 作り直して回答の部品が出る
    for (const flip of [brProj(true), brProj(null, null)]) {
      s.projects[1] = flip;
      await page.evaluate(() => load());
      await until(async () => (await qcard(page, "r-1").count()) === 1, "r-1 が回答できるカードに作り直される");
      assert.equal(await qcard(page, "r-1").locator(".opt").count(), 2);
      assert.equal(await acard(page, "r-2").locator("button.ghost").count(), 1);
      assert.equal(await page.locator(".ro, .roopts").count(), 0);
    }
    // 書き込めなくなった → 部品を外して案内に戻る (cli が無ければ名前を出さない)
    s.projects[1] = brProj(false, null);
    await page.evaluate(() => load());
    await until(async () => (await page.locator(".ro").count()) === 3, "案内の 1 行に戻る");
    assert.deepEqual(await card(page, "r-1").locator(".ro").allInnerTexts(), ["このプロジェクトは画面からはまだ回答できません"]);
    s.projects[1] = brProj(false);
    await page.evaluate(() => load());
    await until(async () => (await card(page, "r-1").locator(".ro").innerText()) === RO_NOTE, "CLI の名前の付いた案内");
    await ro(page);
    assert.deepEqual(s.answers, [], "書き込めないプロジェクトに回答を送った");
    assert.deepEqual(s.undos, []);
  });
});

// ---- キー操作 (Cmd/Ctrl+Enter で送る・カードそのものにフォーカスがあるときの数字キー) -----------------------------------
// Cmd/Ctrl+Enter は補足欄か選択肢にフォーカスがあるとき「回答する」と同じ。カードそのものでは、数字で選んだ後だけ効く。
// 回答の後にフォーカスが移ってきた次のカードでは、押し直しやリピートの Cmd/Ctrl+Enter が下書きの選択を送らない

const MOD_ENTER = ["Control+Enter", "Meta+Enter"];
// フォーカスのあるカードそのもの (中の部品ではなく section) の ID。カードそのものでなければ null
const focusedCard = page => page.evaluate(() => {
  const a = document.activeElement;
  return a && a.matches("section.card") ? a.querySelector(".id").textContent : null;
});
// 選択肢ごとの [.sel か, aria-pressed]
const optStates = (page, id) => qcard(page, id).locator(".opt").evaluateAll(es => es.map(e => [e.classList.contains("sel"), e.getAttribute("aria-pressed")]));
const NONE2 = [[false, "false"], [false, "false"]], FIRST2 = [[true, "true"], [false, "false"]], SECOND2 = [[false, "false"], [true, "true"]];

test("Control+Enter / Meta+Enter: 補足欄か選択肢にフォーカスがあれば、そのカードの回答が 1 回だけ送られる (選択肢は押されない)。選択肢の無いカードの補足欄でも送れる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2"), item("t-C3"), item("t-D4"), item("t-E5", { q: null }), item("t-F6", { q: null })])]);
  await withPage(s, async page => {
    // [カード, キー, フォーカスの位置, 送られる option, 送られる text]
    const cases = [
      ["t-A1", "Control+Enter", "textarea", "a", "memo"], ["t-B2", "Meta+Enter", "textarea", "a", "memo"],
      ["t-C3", "Control+Enter", "opt", "b", ""], ["t-D4", "Meta+Enter", "opt", "b", ""],
      ["t-E5", "Control+Enter", "textarea", "", "memo"], ["t-F6", "Meta+Enter", "textarea", "", "memo"],
    ];
    for (const [id, key, where, option, text] of cases) {
      const n = s.answers.length;
      const c = qcard(page, id);
      if (where === "textarea") {
        if (option) await c.locator(".opt").first().click();
        await c.locator("textarea").click();
        await c.locator("textarea").pressSequentially("memo");
      } else {
        await c.locator(".opt").nth(1).click();                     // フォーカスは押した選択肢に残る
      }
      await page.keyboard.press(key);
      await until(() => s.answers.length === n + 1, 3000, `${id} の回答の POST (${key} / ${where})`);
      await page.waitForTimeout(250);                               // 2 回目が出ないこと
      assert.equal(s.answers.length, n + 1, `${id}: 1 回だけでない`);
      const a = s.answers[n];
      assert.deepEqual([a.id, a.option, a.text], [id, option, text], `${key} / ${where}`);
    }
  });
});

test("Control+Enter / Meta+Enter: 何も選んでいない・補足が空白だけのときは送られない。補足欄に改行は入らず、選択肢も選ばれない", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2", { q: null })])]);
  await withPage(s, async page => {
    const a = qcard(page, "t-A1"), b = qcard(page, "t-B2");
    await a.locator("textarea").click();
    for (const k of MOD_ENTER) await page.keyboard.press(k);
    await a.locator(".opt").first().focus();
    for (const k of MOD_ENTER) await page.keyboard.press(k);
    await b.locator("textarea").click();
    await b.locator("textarea").pressSequentially("   ");
    for (const k of MOD_ENTER) await page.keyboard.press(k);
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers, [], "送れない状態で送られた");
    assert.deepEqual(await optStates(page, "t-A1"), NONE2, "Cmd/Ctrl+Enter で選択肢が選ばれた");
    assert.equal(await a.locator("textarea").inputValue(), "", "補足欄に改行が入った");
    assert.equal(await b.locator("textarea").inputValue(), "   ");
  });
});

test("素の Enter を補足欄で押すと改行が入り、送られない", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    const c = qcard(page, "t-A1");
    await c.locator(".opt").first().click();
    const ta = c.locator("textarea");
    await ta.click();
    await ta.pressSequentially("x");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers, [], "素の Enter で送られた");
    assert.equal(await ta.count(), 1, "補足欄のあるカードが無くなった");   // 送られて畳まれたカードに書き続けない (待ちの時間切れにしない)
    await ta.pressSequentially("y");
    assert.equal(await ta.inputValue(), "x\ny");
  });
});

test("IME の変換中 (isComposing か keyCode 229) の Control+Enter / Meta+Enter では送られない。変換中でない同じイベントなら送られる", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    const c = qcard(page, "t-A1");
    await c.locator(".opt").first().click();
    await c.locator("textarea").click();
    const fire = init => c.locator("textarea").evaluate((e, init) =>
      e.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true, ...init })), init);
    for (const mod of [{ ctrlKey: true }, { metaKey: true }]) {
      await fire({ ...mod, isComposing: true });
      await fire({ ...mod, keyCode: 229 });
    }
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers, [], "変換中のキーで送られた");
    await fire({ ctrlKey: true });                                  // 対照: 変換中でなければ、同じ作り方のイベントで送られる
    await until(() => s.answers.length === 1, 3000, "変換中でない Control+Enter の POST");
  });
});

test("カードそのものにフォーカスがあるとき、数字でその番の選択肢が選ばれ (aria-pressed)、送られない。同じ数字では外れず、範囲外と修飾つきの数字は何もしない。数字で選んだ後の Control+Enter で送られる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2", { q: null })])]);
  await withPage(s, async page => {
    await qcard(page, "t-A1").focus();
    await page.keyboard.press("2");
    assert.deepEqual(await optStates(page, "t-A1"), SECOND2, "2 で 2 番目の選択肢が選ばれない");
    assert.equal(await focusedCard(page), "t-A1", "数字でフォーカスがカードそのものから動いた");
    await page.keyboard.press("2");
    assert.deepEqual(await optStates(page, "t-A1"), SECOND2, "同じ数字で選択が外れた");
    for (const k of ["3", "9", "0", "Control+1", "Meta+1", "Alt+1"]) await page.keyboard.press(k);
    assert.deepEqual(await optStates(page, "t-A1"), SECOND2, "範囲外か修飾つきの数字で選択が変わった");
    await page.keyboard.press("1");
    assert.deepEqual(await optStates(page, "t-A1"), FIRST2, "1 で 1 番目へ移らない");
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers, [], "数字だけで送られた");
    await page.keyboard.press("Control+Enter");
    await until(() => s.answers.length === 1, 3000, "数字で選んだ後の Control+Enter の POST");
    assert.deepEqual([s.answers[0].id, s.answers[0].option], ["t-A1", "a"]);
    // フォーカスは選択肢の無い次のカードそのものへ移る。そこでの数字は何もしない (例外も出さない)
    assert.equal(await focusedCard(page), "t-B2");
    await page.keyboard.press("1");
    await page.keyboard.press("Control+Enter");
    await page.waitForTimeout(300);
    assert.equal(s.answers.length, 1, "選択肢の無いカードで送られた");
    assert.equal(await qcard(page, "t-B2").locator("textarea").inputValue(), "");
  });
});

test("補足欄の中の数字は普通の入力になる。選択肢にフォーカスがあるときの数字も選択を変えない", async () => {
  const s = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s, async page => {
    const c = qcard(page, "t-A1");
    await c.locator("textarea").click();
    await c.locator("textarea").pressSequentially("12");
    assert.equal(await c.locator("textarea").inputValue(), "12");
    assert.deepEqual(await optStates(page, "t-A1"), NONE2, "補足欄の数字で選択肢が選ばれた");
    await c.locator(".opt").first().focus();
    await page.keyboard.press("2");
    assert.deepEqual(await optStates(page, "t-A1"), NONE2, "選択肢にフォーカスがあるときの数字で選ばれた");
    assert.deepEqual(s.answers, []);
  });
});

test("Cmd/Ctrl+Enter で送った後、フォーカスは次のカードそのものへ移り、続けて押した Control+Enter / Meta+Enter は次のカードを送らない (下書きの選択があっても・次が回答済みでも)。続けて数字を押せば次のカードが選ばれ、送れる", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2"), item("t-C3")])]);
  await withPage(s, async page => {
    await qcard(page, "t-B2").locator(".opt").nth(1).click();      // B には下書きの選択 (案B) がある
    await qcard(page, "t-A1").locator(".opt").first().click();
    await qcard(page, "t-A1").locator("textarea").click();
    await page.keyboard.press("Control+Enter");
    await until(() => s.answers.length === 1, 3000, "A の回答の POST");
    assert.equal(await focusedCard(page), "t-B2", "フォーカスが次のカードそのものに移っていない");
    for (const k of MOD_ENTER) { await page.keyboard.press(k); await page.waitForTimeout(50); }   // 押し直し・リピート
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers.map(a => a.id), ["t-A1"], "次のカードの下書きの選択が、押し直しで送られた");
    // 送った直後の数字は無視しない: 次のカードの選択肢を選ぶだけで、送らない
    await page.keyboard.press("1");
    assert.deepEqual(await optStates(page, "t-B2"), FIRST2, "送った直後の数字で次のカードが選ばれない");
    await page.waitForTimeout(300);
    assert.equal(s.answers.length, 1, "数字だけで送られた");
    await page.keyboard.press("Meta+Enter");
    await until(() => s.answers.length === 2, 3000, "B の回答の POST");
    assert.deepEqual([s.answers[1].id, s.answers[1].option], ["t-B2", "a"]);
    assert.equal(await focusedCard(page), "t-C3");
  });
  // 次が回答済みのカード: 押し直しで取り消しは送られない
  const s2 = new Script([proj(P1, [item("t-X1")], [answeredItem("t-Y2")])]);
  await withPage(s2, async page => {
    await openAnswered(page);                                      // 回答済みの節は畳んであるので、開いて次のカードにする (畳んだままだと次のカードが見えず、フォーカスは別へ移る)
    await qcard(page, "t-X1").locator(".opt").first().click();
    await qcard(page, "t-X1").locator("textarea").click();
    await page.keyboard.press("Meta+Enter");
    await until(() => s2.answers.length === 1, 3000, "X の回答の POST");
    assert.equal(await focusedCard(page), "t-Y2", "フォーカスが次の回答済みのカードそのものに移っていない (このテストの前提)");
    for (const k of MOD_ENTER) { await page.keyboard.press(k); await page.waitForTimeout(50); }
    await page.waitForTimeout(300);
    assert.deepEqual(s2.undos, [], "次の回答済みカードの取り消しが送られた");
    assert.equal(s2.answers.length, 1);
  });
});

test("カードそのもので数字を押して選んだ後にフォーカスが離れたら、戻ってきたそのカードでの Control+Enter では送らない (もう一度数字を押せば送れる)", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
  await withPage(s, async page => {
    await qcard(page, "t-B2").focus();
    await page.keyboard.press("2");                                 // B を数字で選ぶ
    await qcard(page, "t-A1").locator(".opt").first().click();      // A へ移って答える
    await qcard(page, "t-A1").locator("textarea").click();
    await page.keyboard.press("Control+Enter");
    await until(() => s.answers.length === 1, 3000, "A の回答の POST");
    assert.equal(await focusedCard(page), "t-B2");
    await page.keyboard.press("Control+Enter");                     // 押し直し
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers.map(a => a.id), ["t-A1"], "前に数字で選んだカードが、押し直しで送られた");
    await page.keyboard.press("2");                                 // 選んだままの数字でも、押し直せば確定できる
    assert.deepEqual(await optStates(page, "t-B2"), SECOND2);
    await page.keyboard.press("Control+Enter");
    await until(() => s.answers.length === 2, 3000, "B の回答の POST");
    assert.deepEqual([s.answers[1].id, s.answers[1].option], ["t-B2", "b"]);
  });
});

test("書き込めないプロジェクト (br) のカードでは、数字も Control+Enter / Meta+Enter も何もしない。キーのヒントは、書き込める判断待ちのカードの「回答する」の隣にだけ出る", async () => {
  const BR = "/work/rusty";
  const s = new Script([{ ...proj(BR, [item("r-1")], [answeredItem("r-2")]), cli: "br", writable: false },
    { ...proj(P1, [item("t-1")], [answeredItem("t-2")]), cli: "bd", writable: true }]);
  const card = (page, id) => page.locator(`section.card:has(.id:text-is("${id}"))`);
  await withPage(s, async page => {
    await openAnswered(page);                                       // 回答済みのカード (r-2) にフォーカスして数字と Enter を押すので、節を開く
    assert.deepEqual(await page.locator(".keyhint").allInnerTexts(), ["⌘↵ / Ctrl+↵ で送信"], "ヒントが 1 枚のカードにだけ出ていない");
    assert.equal(await qcard(page, "t-1").locator(".row .keyhint").count(), 1, "ヒントが「回答する」の行に無い");
    // 読み上げは「回答する」の aria-keyshortcuts に任せ、見た目のヒントは読み上げない (二重に読まない)
    assert.equal(await page.locator(".keyhint").getAttribute("aria-hidden"), "true");
    assert.equal(await qcard(page, "t-1").locator("button.primary").count(), 1);
    assert.equal(await qcard(page, "t-1").locator("button.primary").getAttribute("aria-keyshortcuts"), "Meta+Enter Control+Enter");
    for (const id of ["r-1", "r-2"]) {
      assert.equal(await card(page, id).count(), 1, `${id} のカードが 1 枚でない`);
      await card(page, id).focus();
      for (const k of ["1", "2", ...MOD_ENTER]) await page.keyboard.press(k);
      assert.equal(await focusedCard(page), id, `${id}: キーでフォーカスが動いた`);
    }
    await page.waitForTimeout(300);
    assert.deepEqual(s.answers, [], "書き込めないプロジェクトに回答を送った");
    assert.deepEqual(s.undos, []);
    assert.equal(await page.locator(".opt.sel").count(), 0, "書き込めるカードの選択肢が選ばれた");
  });
});

test("書き込める br のプロジェクト (cli:br・writable:true) のカードは bd と同じに答えられる: 案内を出さず、数字と Cmd/Ctrl+Enter・補足欄・トーストと回答済みのカードの取り消しが、そのプロジェクトに送られる", async () => {
  const BR = "/work/rusty";
  const s = new Script([{ ...proj(BR, [item("r-1"), item("r-2"), item("r-3")], [answeredItem("r-9")]), cli: "br", writable: true }]);
  await withPage(s, async page => {
    assert.equal(await page.locator(".ro, .roopts").count(), 0, "書き込める br のカードに読み取りだけの案内が出た");
    for (const id of ["r-1", "r-2", "r-3"]) {
      assert.equal(await qcard(page, id).count(), 1, `${id} に回答の部品が無い`);
      assert.equal(await qcard(page, id).locator(".opt").count(), 2);
    }
    assert.equal(await page.locator(".keyhint").count(), 3, "キーのヒントが判断待ちのカードごとに出ていない");
    assert.equal(await acard(page, "r-9").locator("button.ghost").count(), 1, "回答済みのカードに取り消しのボタンが無い");
    // 数字で選んで Control+Enter
    await qcard(page, "r-1").focus();
    await page.keyboard.press("2");
    assert.deepEqual(await optStates(page, "r-1"), SECOND2, "数字で選択肢が選ばれない");
    await page.keyboard.press("Control+Enter");
    await until(() => s.answers.length === 1, 3000, "r-1 の回答の POST");
    // フォーカスは次のカードそのものへ移る。補足欄から Meta+Enter
    assert.equal(await focusedCard(page), "r-2");
    await qcard(page, "r-2").locator(".opt").first().click();
    await qcard(page, "r-2").locator("textarea").click();
    await qcard(page, "r-2").locator("textarea").pressSequentially("補足");
    await page.keyboard.press("Meta+Enter");
    await until(() => s.answers.length === 2, 3000, "r-2 の回答の POST");
    assert.equal(s.answers.length, 2);
    assert.deepEqual(s.answers.map(a => [a.project, a.id, a.option, a.text, a.qsig]),
      [[BR, "r-1", "b", "", "sig-1"], [BR, "r-2", "a", "補足", "sig-1"]]);
    // トーストの「取り消す」は、最後の回答 (r-2) をそのプロジェクトに、応答の署名つきで送る
    await waitFor(async () => (await undoInToast(page).count()) === 2, 3000, "回答のトーストが 2 つ");
    await undoInToast(page).last().click();
    await until(() => s.undos.length === 1, 3000, "トーストからの取り消しの POST");
    assert.deepEqual([s.undos[0].project, s.undos[0].id, s.undos[0].asig], [BR, "r-2", "asig-r-2-2"]);
    // 回答済みのカードの「取り消して判断待ちに戻す」(畳んだ節を開いてから)
    await openAnswered(page);
    await undoOnCard(page, "r-9").click();
    await until(() => s.undos.length === 2, 3000, "回答済みのカードからの取り消しの POST");
    assert.deepEqual([s.undos[1].project, s.undos[1].id, s.undos[1].asig], [BR, "r-9", "asig-r-9"]);
  });
});

for (const scheme of ["light", "dark"]) {
  test(`${scheme}: キーのヒントは「回答する」より小さい文字で、カードの背景に対して 4.5:1 以上`, async () => {
    const s = new Script([proj(P1, [item("t-A1")])]);
    await withPage(s, async page => {
      await page.emulateMedia({ colorScheme: scheme });
      assert.equal(await page.locator(".keyhint").count(), 1, "ヒントがちょうど 1 つでない");
      const m = await page.evaluate(() => {
        const card = document.querySelector("section.card:has(textarea)"), cs = e => getComputedStyle(e);
        const hint = cs(card.querySelector(".keyhint")), send = cs(card.querySelector("button.primary"));
        return { fg: hint.color, bg: cs(card).backgroundColor, size: parseFloat(hint.fontSize), sendSize: parseFloat(send.fontSize) };
      });
      const ratio = await page.evaluate(([fnSrc, m]) => eval(`(${fnSrc})()`)(m.fg, m.bg), [RATIO_FN.toString(), m]);
      assert.ok(m.size < m.sendSize, `${scheme}: ヒントの文字 ${m.size}px が「回答する」の ${m.sendSize}px より小さくない`);
      assert.ok(ratio >= 4.5, `${scheme}: ヒントの文字 / カードの背景 ${ratio.toFixed(2)}`);
    });
  });
}

// ---- 並び: プロジェクトごと / 全体を優先度順 ---------------------------------------------------------------
// 目次の帯の切り替えで、判断待ち (判断と作業・確認) を全プロジェクト通して「優先度 → 作成日」の 1 列にし、回答済みはその下に 1 節。
// 選んだほうは localStorage に覚える。カードは作り直さずに入れ物を移すだけ (入力・フォーカス・下書きを保つ)

const viewBtn = (page, v) => page.locator(`#toc .viewbtn[data-view="${v}"]`);
// 切り替えのボタンを押す。ボタンがちょうど 1 つあって見えていることを先に確かめる (無い・隠れたボタンを押す待ちの時間切れではなく、
// assertion で落とす)
async function setView(page, v) {
  assert.equal(await viewBtn(page, v).count(), 1, `並びの切り替え (${v}) のボタンがちょうど 1 つでない`);
  assert.equal(await viewBtn(page, v).isVisible(), true, `並びの切り替え (${v}) のボタンが見えていない (帯が隠れた)`);
  await viewBtn(page, v).click();
}
// 判断待ちのカード (id) がちょうど 1 枚あることを確かめる (無いカードを押す待ちの時間切れを、assertion の失敗にする)
const oneQ = async (page, id) => assert.equal(await qcard(page, id).count(), 1, `${id} の判断待ちのカードがちょうど 1 枚でない`);
const answerOne = async (page, id) => { await oneQ(page, id); await answerFirst(page, id); };
const viewPressed = page => page.$$eval("#toc .viewbtn", bs => bs.map(b => [b.dataset.view, b.getAttribute("aria-pressed")]));
// main の中のカードの並び (畳んでいる最中のものは除く): [ID, head に見えているプロジェクトの名前 (見えていなければ null)]
const cardOrder = page => page.$$eval("#main section.card:not(.leaving)", cs => cs.map(c => {
  const pj = c.querySelector(".head .pj");
  return [c.querySelector(".id").textContent, pj && pj.getClientRects().length ? pj.textContent : null];
}));
const ids = rows => rows.map(r => r[0]);
// 見出しの並び。プロジェクトの見出しは名前だけ
const headings = page => page.$$eval("h1, h2, h3", es => es.map(e => {
  const n = e.querySelector(".pname");
  return `${e.tagName.toLowerCase()}:${n ? n.textContent : e.textContent}`;
}));

// alpha: 作業・確認の P1 (11-22) と P3 (11-24)。beta: P0・P1 (11-20)・作成日の無い P3・P3 (alpha の P3 と同じ日)・優先度なし (P9 として扱う)。
// 回答済み: alpha の P1 と beta の P0。各プロジェクトの判断待ちはサーバーと同じく優先度 → 作成日 (無ければ空の文字列) の順で返す
function orderScript() {
  const task = { ...item("t-A2", { priority: 1, created_at: "2026-11-22" }), q: { ...item("t-A2").q, kind: "task" } };
  return new Script([
    proj(P1, [task, item("t-A1", { priority: 3 })], [answeredItem("t-A9")]),
    proj(P2, [item("t-B1", { priority: 0 }), item("t-B2", { priority: 1, created_at: "2026-11-20" }), item("t-B5", { priority: 3, created_at: null }),
      item("t-B4", { priority: 3 }), item("t-B3", { priority: null })], [{ ...answeredItem("t-B9"), priority: 0 }]),
  ]);
}
// プロジェクトごと: alpha (判断 → 作業・確認 → 回答済み) → beta。head にプロジェクトの名前は出ない
const PROJ_ORDER = [["t-A1", null], ["t-A2", null], ["t-A9", null], ["t-B1", null], ["t-B2", null], ["t-B5", null], ["t-B4", null], ["t-B3", null], ["t-B9", null]];
// 全体: P0 → P1 (作成日の古い beta の 11-20 が先。プロジェクトの順と逆) → P3 (作成日の無いものが先 (サーバーと同じ)、同じ日はプロジェクトの順)
// → 優先度なし → 回答済み
const FLAT_ORDER = [["t-B1", "beta"], ["t-B2", "beta"], ["t-A2", "alpha"], ["t-B5", "beta"], ["t-A1", "alpha"], ["t-B4", "beta"], ["t-B3", "beta"],
  ["t-B9", "beta"], ["t-A9", "alpha"]];

test("並び: 「全体を優先度順」では判断待ち (判断と作業・確認) を全プロジェクト通して優先度 → 作成日の 1 列にし (alpha の P3 より beta の P0 が先)、head にプロジェクトの名前を出す。回答済みはその下に 1 節。既定はプロジェクトごとで、戻せば元の並び", async () => {
  const s = orderScript();
  await withPage(s, async page => {
    assert.deepEqual(await viewPressed(page), [["proj", "true"], ["prio", "false"]], "既定がプロジェクトごとでない");
    assert.equal(await page.getByRole("group", { name: "並び" }).count(), 1, "切り替えに名前の付いた group が無い");
    assert.deepEqual(await cardOrder(page), PROJ_ORDER);
    assert.deepEqual(await headings(page), ["h1:判断待ち", "h2:alpha", "h3:判断 (1)", "h3:作業・確認 (1)", "h3:回答済み・反映待ち (1)",
      "h2:beta", "h3:判断 (5)", "h3:回答済み・反映待ち (1)"]);
    // 帯は 2 行: チップが少なく切り替えの横に収まる幅でも、チップの行は切り替えの行の下から始まる
    const rows = await page.evaluate(() => {
      const v = document.querySelector("#toc .viewbtn:last-child").getBoundingClientRect(), c = [...document.querySelectorAll("#toc .tocchip")].map(e => e.getBoundingClientRect());
      return { viewBottom: v.bottom, viewRight: v.right, chipTops: c.map(r => r.top), chipsW: c.reduce((n, r) => n + r.width + 8, 0), innerW: innerWidth };
    });
    assert.equal(rows.chipTops.length, 2);
    assert.ok(rows.viewRight + 12 + rows.chipsW < Math.min(rows.innerW, 860) - 32, `前提: チップが切り替えの横に収まる幅でない ${JSON.stringify(rows)}`);
    assert.ok(rows.chipTops.every(t => t >= rows.viewBottom), `チップの行が切り替えの行の下にない ${JSON.stringify(rows)}`);
    await setView(page, "prio");
    assert.deepEqual(await viewPressed(page), [["proj", "false"], ["prio", "true"]]);
    assert.deepEqual(await cardOrder(page), FLAT_ORDER);
    // 見出しは h1 → h2 (節)。プロジェクトの見出しと入れ物は出さない。節の見出しは h3 と同じ見た目で、止まらない
    assert.deepEqual(await headings(page), ["h1:判断待ち", "h2:判断待ち (7)", "h2:回答済み・反映待ち (2)"]);
    assert.equal(await page.locator(".proj").count(), 0, "プロジェクトの入れ物が残っている");
    assert.deepEqual(await page.evaluate(() => { const c = getComputedStyle(document.querySelector("h2.sec")); return [c.fontSize, c.position]; }), ["14px", "static"]);
    // 帯 (切り替え) は出したまま、チップは出さない (空のチップの行で縦に場所を取らない)。ヘッダーの件数は変わらない
    assert.equal(await page.locator("#toc").isVisible(), true, "帯が隠れた (プロジェクトごとに戻せない)");
    assert.equal(await tocChips(page).count(), 0, "全体の表示に目次のチップが出ている");
    const band = await page.evaluate(() => [document.getElementById("toc").getBoundingClientRect().height, document.querySelector("#toc .view").getBoundingClientRect().height]);
    assert.ok(Math.abs(band[0] - (band[1] + 12)) <= 1, `帯の高さが切り替えの行 + 上下の余白でない (空のチップの行が場所を取る) ${JSON.stringify(band)}`);
    assert.equal(await page.locator("#count").innerText(), "7 件");
    assert.equal(await qcard(page, "t-B1").locator(".head .pj").count(), 1, "head にプロジェクトの名前の要素がちょうど 1 つでない");
    assert.equal(await qcard(page, "t-B1").locator(".head .pj").getAttribute("title"), P2, "プロジェクトの名前の title がパスでない");
    await setView(page, "proj");
    assert.deepEqual(await cardOrder(page), PROJ_ORDER, "プロジェクトごとに戻らない");
    assert.deepEqual((await chipRows(page)).map(r => r.name), ["alpha", "beta"], "戻したのにチップが出ない");
    assert.equal(await page.locator(".flat, h2.sec").count(), 0, "全体の表示の入れ物が残っている");
  });
});

test("並び: 選んだ並びは再読み込みしても同じ (localStorage)。プロジェクトごとに戻せば、再読み込みでもプロジェクトごと", async () => {
  const s = orderScript();
  await withPage(s, async page => {
    await setView(page, "prio");
    assert.equal(await page.evaluate(() => localStorage.getItem("humanq-view")), "prio");
    await page.reload();
    await ready(page);
    assert.deepEqual(await viewPressed(page), [["proj", "false"], ["prio", "true"]], "再読み込みで切り替えの状態が戻らない");
    assert.deepEqual(await cardOrder(page), FLAT_ORDER, "再読み込みで全体の並びに戻らない");
    await setView(page, "proj");
    assert.equal(await page.evaluate(() => localStorage.getItem("humanq-view")), "proj");
    await page.reload();
    await ready(page);
    assert.deepEqual(await viewPressed(page), [["proj", "true"], ["prio", "false"]]);
    assert.deepEqual(await cardOrder(page), PROJ_ORDER);
  });
});

test("並び: localStorage が使えなくても既定のプロジェクトごとで出て、切り替えは効く (読み書きが例外・書き込みだけが例外)", async () => {
  // localStorage そのものに触れると例外 (保存を禁じた設定など)
  const s = orderScript();
  await withPage(s, async page => {
    assert.ok(await page.evaluate(() => { try { void window.localStorage; return false; } catch { return true; } }), "前提: localStorage に触れても例外にならない");
    assert.deepEqual(await cardOrder(page), PROJ_ORDER);
    await setView(page, "prio");
    assert.deepEqual(await cardOrder(page), FLAT_ORDER, "覚えられないと切り替わらない");
    await setView(page, "proj");
    assert.deepEqual(await cardOrder(page), PROJ_ORDER);
  }, {}, page => page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", { configurable: true, get() { throw new DOMException("denied", "SecurityError"); } });
  }));
  // 読めるが書けない (容量切れなど): 覚えていた全体の表示で出て、プロジェクトごとへの切り替えも効く
  const s2 = orderScript();
  await withPage(s2, async page => {
    assert.ok(await page.evaluate(() => { try { localStorage.setItem("x", "1"); return false; } catch { return true; } }), "前提: localStorage に書けてしまう");
    assert.deepEqual(await cardOrder(page), FLAT_ORDER, "読めた値 (prio) で出ない");
    await setView(page, "proj");
    assert.deepEqual(await cardOrder(page), PROJ_ORDER, "書けないと切り替わらない");
  }, {}, page => page.addInitScript(() => {
    const set = Storage.prototype.setItem;
    set.call(window.localStorage, "humanq-view", "prio");
    Storage.prototype.setItem = function (k, v) {
      if (this === window.localStorage) throw new DOMException("full", "QuotaExceededError");
      return set.call(this, k, v);
    };
  }));
});

test("並び: 全体の表示で回答すると、カードが畳まれ (送信中は読み込みがあっても隠したまま)、フォーカスは全体の並びでの次のカードそのものへ移る。回答済みの節に出て、そこから取り消すと判断待ちの並びに戻る", async () => {
  // 全体の並びは t-B1 (P0) → t-A1 (P1) → t-B2 (P2)。プロジェクトごとなら t-B1 の次は同じプロジェクトの t-B2
  const s = new Script([proj(P1, [item("t-A1", { priority: 1 })]), proj(P2, [item("t-B1", { priority: 0 }), item("t-B2", { priority: 2 })])]);
  await withPage(s, async page => {
    await setView(page, "prio");
    assert.deepEqual(ids(await cardOrder(page)), ["t-B1", "t-A1", "t-B2"]);
    await page.evaluate(() => {
      window.__left = [];
      new MutationObserver(ms => { for (const m of ms) if (m.target.matches("section.card.leaving")) window.__left.push(m.target.querySelector(".id").textContent); })
        .observe(document.getElementById("main"), { subtree: true, attributes: true, attributeFilter: ["class"] });
    });
    const release = s.hold("answer");
    await answerOne(page, "t-B1");
    await until(async () => (await focusedCard(page)) === "t-A1", 3000, "フォーカスが全体の並びでの次 (alpha の t-A1) のカードそのものへ移る");
    assert.deepEqual(await page.evaluate(() => [...new Set(window.__left)]), ["t-B1"], "押したカードが畳む動き (.leaving) を通らない");
    await until(async () => (await qcard(page, "t-B1").count()) === 0, 3000, "畳み終わって外れる");
    assert.deepEqual(await page.locator("h2.sec").allInnerTexts(), ["判断待ち (2)"], "節の件数が送信中を除いていない");
    await page.evaluate(() => load());                                    // まだ書き込まれていないデータ (t-B1 は判断待ちに居る)
    await page.waitForTimeout(300);
    assert.equal(await qcard(page, "t-B1").count(), 0, "送信中のカードが全体の表示に戻ってきた");
    release();
    await until(async () => (await acard(page, "t-B1").count()) === 1, 3000, "回答済みの節に出る");
    assert.deepEqual(await cardOrder(page), [["t-A1", "alpha"], ["t-B2", "beta"], ["t-B1", "beta"]]);
    assert.deepEqual(await page.locator("h2.sec").allInnerTexts(), ["判断待ち (2)", "回答済み・反映待ち (1)"]);
    await openAnswered(page);                                             // 回答済みの節 (h2.sec の summary) を開く (畳んだ中のボタンは getByRole で数えられない)
    assert.equal(await undoOnCard(page, "t-B1").count(), 1, "回答済みのカードに取り消しのボタンが無い");
    await undoOnCard(page, "t-B1").click();
    await until(() => s.undos.length === 1, 3000, "取り消しの POST");
    assert.deepEqual([s.undos[0].project, s.undos[0].id], [P2, "t-B1"]);
    // 台本は取り消したチケットを P1 で戻す: t-A1 と同じ優先度・作成日なので、プロジェクトの順 (alpha が先)
    await until(async () => (await qcard(page, "t-B1").count()) === 1, 3000, "判断待ちに戻る");
    assert.deepEqual(await cardOrder(page), [["t-A1", "alpha"], ["t-B1", "beta"], ["t-B2", "beta"]]);
    await until(async () => (await page.locator("h2.sec").count()) === 1, 3000, "空になった回答済みの節が畳み終わって外れる");
    assert.deepEqual(await page.locator("h2.sec").allInnerTexts(), ["判断待ち (3)"]);
  });
});

test("並び: 動きを減らす設定の全体の表示では、回答したカードはその場で外れ、切り替えにも動きを付けない", async () => {
  const s = new Script([proj(P1, [item("t-A1", { priority: 1 })]), proj(P2, [item("t-B1", { priority: 0 })])]);
  await withPage(s, async page => {
    await setView(page, "prio");
    assert.equal(await page.locator("#main .anim, #main .incoming, #main .leaving").count(), 0, "切り替えで動きが付いた");
    s.hold("answer");
    await answerOne(page, "t-B1");                                       // 押した処理の中で外れる (待たずに測る)
    assert.equal(await page.locator("section.card").count(), 1, "動きを減らす設定で、回答したカードがその場で外れない");
    assert.equal(await page.locator("#main .anim").count(), 0);
    assert.equal(await focusedCard(page), "t-A1");
  }, { reducedMotion: "reduce" });
});

test("並び: 全体の表示でも、入力中のカードはデータが変わっても作り直さず (ほかのカードは更新する)、離れたあとの読み込みで作り直す。優先度が変わって並びの位置が動いても、入力のフォーカスと値は保たれる", async () => {
  const s = new Script([proj(P1, [item("t-A1", { priority: 2 })]), proj(P2, [item("t-B1", { priority: 0 }), item("t-B2", { priority: 3 })])]);
  await withPage(s, async page => {
    await setView(page, "prio");
    assert.deepEqual(ids(await cardOrder(page)), ["t-B1", "t-A1", "t-B2"]);
    await oneQ(page, "t-B2");
    await qcard(page, "t-B2").locator(".opt").first().click();
    const ta = qcard(page, "t-B2").locator("textarea");
    await ta.click();
    await ta.pressSequentially("abc");
    const handle = await ta.elementHandle();
    s.projects[0].human[0].title = "new title";                         // ほかのカード (alpha) の題
    s.projects[1].human[1].description = "NEW BODY";                    // 入力中のカード自身の本文
    s.projects[1].human[1].priority = 0;                                // 並びの位置も動く (t-B1 と同じ P0・同じ日なので t-B1 の次)
    await page.evaluate(() => load());
    await until(async () => /new title/.test(await qcard(page, "t-A1").textContent()), 3000, "ほかのカードの題が更新される");
    assert.deepEqual(ids(await cardOrder(page)), ["t-B1", "t-B2", "t-A1"]);
    assert.ok(!/NEW BODY/.test(await qcard(page, "t-B2").textContent()), "入力中のカードが作り直された");
    assert.ok(await handle.evaluate(e => e.isConnected && e === document.activeElement && e.value === "abc"), "入力中の補足のフォーカスか値が失われた");
    assert.match(await page.locator("#meta").innerText(), /入力中のカードの更新は後回し/);
    await page.locator("h1").click();                                    // フォーカスを外す
    await page.evaluate(() => load());
    await until(async () => /NEW BODY/.test(await qcard(page, "t-B2").textContent()), 3000, "離れたあとの読み込みで作り直される");
    assert.equal(await qcard(page, "t-B2").locator("textarea").inputValue(), "abc");
    assert.equal(await qcard(page, "t-B2").locator(".opt.sel").count(), 1, "選択が下書きから戻る");
    assert.deepEqual(await cardOrder(page), [["t-B1", "beta"], ["t-B2", "beta"], ["t-A1", "alpha"]], "作り直したカードの head にプロジェクトの名前が無い");
    assert.equal(await qcard(page, "t-B2").locator(".prio").innerText(), "P0");
  });
});

test("並び: 切り替えてもカードは作り直さない: 入力中の補足と選んだ選択肢・開いた本文・下書き (sessionStorage)・フォーカスが残る。押した切り替えのボタンにはフォーカスが残る (キーボードでも)", async () => {
  const s = orderScript();
  await withPage(s, async page => {
    await oneQ(page, "t-A1");
    await qcard(page, "t-A1").locator("summary").click();                // 本文を開く
    await qcard(page, "t-A1").locator(".opt").nth(1).click();
    const ta = qcard(page, "t-A1").locator("textarea");
    await ta.click();
    await ta.pressSequentially("draft");
    const handle = await ta.elementHandle();
    const drafts0 = await page.evaluate(() => sessionStorage.getItem("humanq-drafts"));
    assert.match(drafts0 || "", /draft/, "前提: 下書きが sessionStorage に無い");
    for (const v of ["prio", "proj", "prio"]) {
      // 入力中に切り替わる (ボタンの click() はフォーカスを動かさない)
      assert.equal(await viewBtn(page, v).count(), 1, `並びの切り替え (${v}) のボタンがちょうど 1 つでない`);
      await page.evaluate(v => document.querySelector(`#toc .viewbtn[data-view="${v}"]`).click(), v);
      assert.deepEqual(ids(await cardOrder(page)), ids(v === "prio" ? FLAT_ORDER : PROJ_ORDER), `${v}: 並びが切り替わらない`);
      assert.ok(await handle.evaluate(e => e.isConnected && e === document.activeElement && e.value === "draft"), `${v}: 入力中の補足のフォーカスか値が失われた (作り直された)`);
      assert.deepEqual(await qcard(page, "t-A1").locator(".opt").evaluateAll(es => es.map(e => e.getAttribute("aria-pressed"))), ["false", "true"], `${v}: 選んだ選択肢が外れた`);
      assert.equal(await qcard(page, "t-A1").locator("details").evaluate(e => e.open), true, `${v}: 開いた本文が閉じた`);
      assert.equal(await page.evaluate(() => sessionStorage.getItem("humanq-drafts")), drafts0, `${v}: 下書きが変わった`);
    }
    await page.keyboard.type("+");                                       // そのまま打ち続けられる
    assert.equal(await ta.inputValue(), "draft+");
    await setView(page, "proj");
    assert.ok(await viewBtn(page, "proj").evaluate(e => e === document.activeElement), "押した切り替えのボタンからフォーカスが外れた");
    await viewBtn(page, "prio").focus();
    await page.keyboard.press("Enter");
    assert.deepEqual(await viewPressed(page), [["proj", "false"], ["prio", "true"]], "キーボードで切り替わらない");
    assert.ok(await viewBtn(page, "prio").evaluate(e => e === document.activeElement), "キーボードで押した切り替えのボタンからフォーカスが外れた");
    // キーボードのフォーカスの目印は、押した側の塗り (アクセント) の外に引くアクセントの実線
    assert.deepEqual(await viewBtn(page, "prio").evaluate(e => [e.matches(":focus-visible"), getComputedStyle(e).outlineStyle, getComputedStyle(e).outlineOffset]),
      [true, "solid", "2px"]);
    await page.keyboard.press("Space");                                  // 押してある側をもう一度押しても何も変わらない
    assert.deepEqual(await viewPressed(page), [["proj", "false"], ["prio", "true"]]);
    assert.deepEqual(ids(await cardOrder(page)), ids(FLAT_ORDER));
    assert.equal(await qcard(page, "t-A1").locator("textarea").inputValue(), "draft+");
  });
});

test("並び: 全体の表示では、読めなかったプロジェクトのバナーを全体の先頭に名前つきで出し、前に読めたカードは並びに残す。読めればバナーは消える", async () => {
  const s = new Script([proj(P1, [item("t-A1", { priority: 1 })]), proj(P2, [item("t-B1", { priority: 0 })])]);
  await withPage(s, async page => {
    await setView(page, "prio");
    s.queue.push({ projects: [proj(P1, [item("t-A1", { priority: 1 })]), proj(P2, [], [], false)] });
    await page.evaluate(() => load());
    await until(async () => (await page.locator("#main .banner").count()) === 1, 3000, "バナーが出る");
    assert.match(await page.locator("#main .banner").textContent(), /^beta: 読めませんでした \(最後に読めたのは [^。]+。自動で読み直します\): bd down$/);
    assert.equal(await page.locator("#main .banner").getAttribute("title"), P2);
    assert.deepEqual(await cardOrder(page), [["t-B1", "beta"], ["t-A1", "alpha"]], "読めなかったプロジェクトの前のカードが並びから消えた");
    assert.ok(await page.evaluate(() => {
      const b = document.querySelector("#main .banner"), h = document.querySelector("#main h2.sec");
      return !!(b && h && b.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_FOLLOWING);
    }), "バナーが判断待ちの節より上にない");
    await page.evaluate(() => load());
    await until(async () => (await page.locator("#main .banner").count()) === 0, 3000, "読めればバナーが消える");
    // 一度も読めていないプロジェクト (カードの無いバナーだけ) も出す
    s.projects.push(proj("/work/gamma", [], [], false));
    await page.evaluate(() => load());
    assert.deepEqual(await page.locator("#main .banner").allTextContents(), ["gamma: 読めませんでした (自動で読み直します): bd down"]);
  });
});

test("並び: 全体の表示にも絞り込みが効き (ほかの条件の件数も同じ)、全部隠れたら「絞り込みに合うものはありません」を出す", async () => {
  const s = manyScript();
  await withPage(s, async page => {
    await setView(page, "prio");
    assert.equal(await pcards(page).count(), 36);
    // 全体の並びは優先度 → 作成日 (台本は全件が同じ日なので、同じ優先度ではプロジェクトの順 → サーバーの並び)
    const want = [...FALL(s)].sort((x, y) => x.priority - y.priority).map(i => i.id);
    assert.deepEqual(await pcards(page).locator(".id").allInnerTexts(), want);
    await page.selectOption("#fprio", "0");
    assert.deepEqual(await pcards(page).locator(".id").allInnerTexts(), want.filter(id => FALL(s).find(i => i.id === id).priority === 0));
    assert.equal(await page.locator("#fcount").innerText(), "36 件中 9 件を表示");
    await page.selectOption("#fproject", "/work/beta");
    assert.deepEqual(await pcards(page).locator(".id").allInnerTexts(), FALL(s).filter(i => i.priority === 0 && i.project === "/work/beta").map(i => i.id));
    assert.deepEqual(await page.locator("h2.sec").allInnerTexts(), ["判断待ち (3)"], "回答済み (優先度 1) が絞り込みで隠れていない");
    await page.fill("#fq", "zzz-none");
    assert.match((await page.locator("#main .empty").allInnerTexts()).join("\n"), /絞り込みに合う/);
    assert.equal(await page.locator(".flat").count(), 0, "全部隠れたのに全体の入れ物が残っている");
    await page.click("#fclear");
    assert.equal(await pcards(page).count(), 36);
    assert.deepEqual(await page.locator("h2.sec").allInnerTexts(), ["判断待ち (36)", "回答済み・反映待ち (3)"]);
  });
});

test("並び: 幅 375 でも切り替えは帯の 1 行目に収まり、ページは横にスクロールしない。全体の表示の head のプロジェクトの名前も 1 行", async () => {
  const longName = "とても長いプロジェクトの名前".repeat(6);
  const s = new Script([proj(`/work/${longName}`, [item("t-A1")]), proj(P2, [item("t-B1", { priority: 0 })])]);
  await withPage(s, async page => {
    assert.equal(await page.locator("#toc .view").count(), 1, "並びの切り替えがちょうど 1 つでない");
    // pj: 見えているプロジェクトの名前ごとに [高さ, 名前の上端と ID の上端の差] (長い名前は … で切り、優先度・ID と同じ行に収める)
    const m = () => page.evaluate(() => {
      const v = document.querySelector("#toc .view").getBoundingClientRect();
      const pj = [...document.querySelectorAll("#main .head .pj")].filter(e => e.getClientRects().length).map(e =>
        [e.getBoundingClientRect().height, Math.abs(e.getBoundingClientRect().top - e.parentElement.querySelector(".id").getBoundingClientRect().top)]);
      return { sw: document.scrollingElement.scrollWidth, iw: innerWidth, left: v.left, right: v.right, h: v.height, pj };
    });
    const a = await m();
    assert.ok(a.sw <= a.iw && a.left >= 0 && a.right <= a.iw && a.h <= 32, `プロジェクトごと: ${JSON.stringify(a)}`);
    await setView(page, "prio");
    const b = await m();
    assert.ok(b.sw <= b.iw && b.left >= 0 && b.right <= b.iw && b.h <= 32, `全体: ${JSON.stringify(b)}`);
    assert.equal(b.pj.length, 2, "head にプロジェクトの名前が出ていない");
    assert.ok(b.pj.every(([h, dy]) => h <= 24 && dy <= 4), `head のプロジェクトの名前が 1 行でないか、ID と同じ行に無い ${JSON.stringify(b.pj)}`);
  }, { viewport: { width: 375, height: 700 } });
});

test("並び: プロジェクトの名前が変わると、全体の表示の head の名前も変わる (判断待ち・回答済みとも)", async () => {
  const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A9")])]);
  await withPage(s, async page => {
    await setView(page, "prio");
    assert.deepEqual(await cardOrder(page), [["t-A1", "alpha"], ["t-A9", "alpha"]]);
    s.projects[0].name = "renamed";
    await page.evaluate(() => load());
    await until(async () => JSON.stringify(await cardOrder(page)) === JSON.stringify([["t-A1", "renamed"], ["t-A9", "renamed"]]), 3000,
      "名前が変わったカードが作り直されて、head に新しい名前が出る");
  });
});

test("並び: 全体の表示で、判断待ちも回答済みも無く読めなかったプロジェクトだけのときは、バナーを名前つきで出し、帯は出さない。プロジェクトごとでも帯は出さない", async () => {
  for (const v of ["prio", "proj"]) {
    const s = new Script([proj(P1, [], [], false)]);
    await withPage(s, async page => {
      assert.equal(await page.evaluate(() => localStorage.getItem("humanq-view")), v, "前提: 覚えた並びが入っていない");
      assert.deepEqual(await page.locator("#main .banner").allTextContents(),
        [`${v === "prio" ? "alpha: " : ""}読めませんでした (自動で読み直します): bd down`], `${v}: バナー`);
      assert.equal(await page.locator("#main .empty").count(), 0, `${v}: バナーがあるのに「判断待ちはありません」が出た`);
      assert.equal(await page.locator("#toc").isHidden(), true, `${v}: 出すカードが無いのに帯が出ている`);
    }, {}, page => page.addInitScript(v => { localStorage.setItem("humanq-view", v); }, v));
  }
});

test("並び: 全体の表示で最後の 1 枚を押すと、空になる節 (か入れ物) ごと畳まれ、送信が失敗すれば節と入れ物ごと元に戻る", async () => {
  // 1 枚だけ: 全体の入れ物ごと畳まれる / 判断待ち 1 枚と回答済み 1 枚: 判断待ちの節だけが畳まれる
  for (const [human, ans, heads, folds] of [[[item("t-A1")], [], ["判断待ち (1)"], ["flat"]],
    [[item("t-A1")], [answeredItem("t-A9")], ["判断待ち (1)", "回答済み・反映待ち (1)"], ["wait"]]]) {
    const s = new Script([proj(P1, human, ans)]);
    s.answerResult = () => ({ status: 502, body: { error: "bd boom" } });
    await withPage(s, async page => {
      await setView(page, "prio");
      // 畳む動き (.leaving) が付いた入れ物 (全体の入れ物は "flat"、節は data-kind) を記録する
      await page.evaluate(() => {
        window.__folded = [];
        new MutationObserver(ms => { for (const m of ms) if (m.target.matches(".flat.leaving, .grp.leaving"))
          window.__folded.push(m.target.matches(".flat") ? "flat" : m.target.dataset.kind); })
          .observe(document.getElementById("main"), { subtree: true, attributes: true, attributeFilter: ["class"] });
      });
      const release = s.hold("answer");
      await answerOne(page, "t-A1");
      await until(async () => (await page.evaluate(() => window.__folded.length)) > 0, 3000, "空になる節か入れ物が畳まれる");
      assert.deepEqual(await page.evaluate(() => [...new Set(window.__folded)]), folds, "畳まれた入れ物が違う");
      release();
      await until(async () => (await page.locator("section.card.failed").count()) === 1, 3000, "カードが戻る");
      await page.waitForTimeout(500);
      assert.deepEqual(await page.locator("h2.sec").allInnerTexts(), heads, "節の見出しが戻らない");
      assert.equal(await page.locator(".flat").count(), 1);
      assert.equal(await page.locator(".leaving, .anim").count(), 0, "畳みの状態が残っている");
      assert.ok(await qcard(page, "t-A1").evaluate(e => e.offsetHeight > 100 && !e.inert), "戻ったカードが見えない・押せない");
      assert.equal(await page.locator("#main .empty").count(), 0);
    });
  }
});

for (const scheme of ["light", "dark"]) {
  test(`${scheme}: 並びの切り替えの枠は帯の背景に対して 3:1 以上、文字は自身の背景に対して 4.5:1 以上 (押した側・押していない側とも)`, async () => {
    const s = new Script([proj(P1, [item("t-A1")])]);
    await withPage(s, async page => {
      await page.emulateMedia({ colorScheme: scheme });
      const m = await page.evaluate(() => {
        const cs = e => getComputedStyle(e);
        return { band: cs(document.body).backgroundColor, btns: [...document.querySelectorAll("#toc .viewbtn")].map(b =>
          ({ pressed: b.getAttribute("aria-pressed"), fg: cs(b).color, bg: cs(b).backgroundColor, bd: cs(b).borderTopColor })) };
      });
      assert.equal(m.btns.length, 2, "切り替えのボタンが 2 つでない");
      // 押した側 (aria-pressed=true) は塗りで見分けられる
      assert.deepEqual(m.btns.map(b => b.pressed), ["true", "false"]);
      assert.notEqual(m.btns[0].bg, m.btns[1].bg, `${scheme}: 押した側と押していない側の背景が同じ`);
      const r = await page.evaluate(([fnSrc, m]) => {
        const ratio = eval(`(${fnSrc})()`);
        return m.btns.map(b => ({ pressed: b.pressed, text: ratio(b.fg, b.bg), edge: ratio(b.bd, m.band) }));
      }, [RATIO_FN.toString(), m]);
      for (const x of r) {
        assert.ok(x.text >= 4.5, `${scheme} aria-pressed=${x.pressed} の文字 ${x.text.toFixed(2)}`);
        assert.ok(x.edge >= 3, `${scheme} aria-pressed=${x.pressed} の枠 / 帯の背景 ${x.edge.toFixed(2)}`);
      }
    });
  });
}

// ---- 保留 (snooze) ------------------------------------------------------------------------------

const scard = (page, id) => page.locator(`#main section.snzcard:has(.id:text-is("${id}"))`);
const snzPanel = (page, id) => qcard(page, id).locator(".snzbox");
const snzSection = page => page.locator("#main details.snz");
const snzSummary = page => snzSection(page).locator("summary");
const snoozedItem = (id, until = null, note = "", o = {}) => ({ ...item(id, o), snooze: { until, note, expired: false, changed: false } });
const withSnoozed = (p, snoozed) => ({ ...p, snoozed });
const DATE_LIKE = /^\d{4}-\d{2}-\d{2}$/;
// ブラウザのローカルの日付で、今日から n 日後 (画面が数えるのと同じ暦)
const localDay = (page, n) => page.evaluate(n => {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}, n);
// 通知 (Notification) を許可済みの偽物にして、出した通知を window.__notes に残す
const fakeNotification = page => page.addInitScript(() => {
  window.__notes = [];
  window.Notification = class {
    constructor(title, o) { window.__notes.push([title, o && o.body]); }
    static get permission() { return "granted"; }
    static requestPermission() { return Promise.resolve("granted"); }
  };
});
const notes = page => page.evaluate(() => window.__notes);

// 操作する前に、ちょうど 1 つあって見えていることを assert する (無い・隠れた要素を押す待ちの時間切れにしない)
async function one(loc, what) {
  assert.equal(await loc.count(), 1, `${what} がちょうど 1 つでない`);
  assert.equal(await loc.isVisible(), true, `${what} が見えていない`);
  return loc;
}
// カードの「保留」を押して欄を開く
async function openSnooze(page, id) {
  await (await one(qcard(page, id).locator("button.snz"), `${id} の「保留」`)).click();
  assert.equal(await snzPanel(page, id).isVisible(), true, `${id} の保留の欄が開かない`);
}
// 欄で期間 (data-kind) か日付 (YYYY-MM-DD) を選び、補足を書いて「保留する」を押す
async function snoozeIt(page, id, pick, note = "") {
  await openSnooze(page, id);
  const box = snzPanel(page, id);
  if (DATE_LIKE.test(pick)) await (await one(box.locator('input[type="date"]'), "日付の入力")).fill(pick);
  else await (await one(box.locator(`.snzopt[data-kind="${pick}"]`), `期間 ${pick} のボタン`)).click();
  if (note) await (await one(box.locator("input.snznote"), "補足の入力")).fill(note);
  const go = await one(box.locator("button.snzgo"), "「保留する」");
  assert.equal(await go.isEnabled(), true, "選んだのに「保留する」が押せない");
  await go.click();
}
// 保留中の節を開く (畳んである)
async function openSnzSection(page) {
  await (await one(snzSummary(page), "保留中の節の見出し")).click();
  assert.equal(await snzSection(page).evaluate(d => d.open), true, "保留中の節が開かない");
}

test("保留: 「保留」は書き込める判断待ちのカードにだけ出る (回答済み・書き込めないプロジェクトには出ない)。押すと欄が開き、選ぶまで「保留する」は押せず、今日以前の日付も選べない (戻る日は明日以降)。「やめる」と Escape で閉じる", async () => {
  const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A9")]),
    { ...withSnoozed(proj(P2, [item("t-B1")]), [snoozedItem("t-B8", "2999-12-31")]), cli: "br", writable: false }]);
  await withPage(s, async page => {
    await openAnswered(page);
    await one(acard(page, "t-A9"), "回答済みのカード");
    assert.equal(await acard(page, "t-A9").locator("button.snz").count(), 0, "回答済みのカードに「保留」がある");
    const ro = await one(page.locator(`#main section.card:has(.id:text-is("t-B1"))`), "書き込めないプロジェクトのカード");
    assert.equal(await ro.locator("button.snz").count(), 0, "書き込めないプロジェクトのカードに「保留」がある");
    await openSnzSection(page);
    await one(scard(page, "t-B8"), "書き込めないプロジェクトの保留中のカード");
    assert.equal(await scard(page, "t-B8").getByRole("button", { name: "保留を解除" }).count(), 0, "書き込めないプロジェクトに「保留を解除」がある");
    assert.equal(await scard(page, "t-B8").locator("p.ro").count(), 1, "書き込めない旨の 1 行が無い");
    assert.equal(await snzPanel(page, "t-A1").count(), 1, "保留の欄がちょうど 1 つでない");
    assert.equal(await snzPanel(page, "t-A1").isVisible(), false, "保留の欄が最初から開いている");
    await openSnooze(page, "t-A1");
    const box = snzPanel(page, "t-A1");
    assert.equal(await qcard(page, "t-A1").locator("button.snz").getAttribute("aria-expanded"), "true");
    assert.deepEqual(await box.locator(".snzopt").allInnerTexts(), ["明日", "3 日後", "1 週間後", "期限なし"]);
    const go = await one(box.locator("button.snzgo"), "「保留する」");
    assert.equal(await go.isEnabled(), false, "何も選ばずに「保留する」を押せる");
    const d3 = await one(box.locator('.snzopt[data-kind="d3"]'), "期間 3 日後");
    await d3.click();
    assert.equal(await d3.getAttribute("aria-pressed"), "true");
    assert.equal(await go.isEnabled(), true);
    const dateIn = await one(box.locator('input[type="date"]'), "日付の入力");
    assert.equal(await dateIn.getAttribute("min"), await localDay(page, 1), "日付の下限が明日でない");
    await dateIn.fill(await localDay(page, -1));
    assert.equal(await d3.getAttribute("aria-pressed"), "false", "日付を選んだのに期間の選びが残った");
    assert.equal(await go.isEnabled(), false, "過去の日付で「保留する」を押せる");
    await dateIn.fill(await localDay(page, 0));
    assert.equal(await go.isEnabled(), false, "今日 (戻る日の当日から判断待ちなので保留にならない) で「保留する」を押せる");
    await dateIn.fill(await localDay(page, 1));
    assert.equal(await go.isEnabled(), true, "明日を選べない");
    await dateIn.fill(await localDay(page, -1));
    await (await one(box.locator(".snzcancel"), "「やめる」")).click();
    assert.equal(await box.isVisible(), false, "「やめる」で閉じない");
    await openSnooze(page, "t-A1");
    await (await one(box.locator("input.snznote"), "補足の入力")).press("Escape");
    assert.equal(await box.isVisible(), false, "Escape で閉じない");
    assert.deepEqual(s.snoozes, [], "選ぶだけで送った");
  });
});

test("保留: 明日・3 日後・1 週間後・期限なし・日付を選ぶは、判断待ちに戻る日 (今日から N 日後。today を返さない古いサーバーではブラウザのローカルの日付で数える) の until (期限なしは null) と補足と qsig を送る。カードは判断待ちから消え、ページの下の畳んだ「保留中 (N)」に戻る日の早い順で出る。ヘッダーの件数とタイトルには数えない", async () => {
  const ids = ["t-A1", "t-A2", "t-A3", "t-A4", "t-A5"];
  const s = new Script([proj(P1, [...ids.map(i => item(i)), item("t-B9")])]);
  await withPage(s, async page => {
    const plan = [["t-A1", "d1", "", 1], ["t-A2", "d3", "仕様の確認待ち", 3], ["t-A3", "d7", "", 7], ["t-A4", "none", "あとで", null],
      ["t-A5", await localDay(page, 10), "", 10]];
    assert.equal((await page.locator("#count").textContent()).trim(), "6 件");
    for (const [id, pick, note] of plan) {
      await snoozeIt(page, id, pick, note);
      await until(async () => (await qcard(page, id).count()) === 0, 3000, `${id} が判断待ちから消える`);
    }
    const want = [];
    for (const [id, , note, days] of plan) want.push({ project: P1, id, qsig: "sig-1", until: days === null ? null : await localDay(page, days), note });
    assert.deepEqual(s.snoozes, want);
    await until(async () => (await page.locator("#count").textContent()).trim() === "1 件", 3000, "ヘッダーの件数が保留を除いた 1 件");
    assert.equal(await page.title(), "(1) 判断待ち");
    await until(async () => (await scard(page, "t-A5").count()) === 1, 3000, "保留中に出る");
    assert.equal(await snzSection(page).count(), 1, "保留中の節が 1 つでない");
    assert.equal(await snzSection(page).evaluate(d => d.open), false, "保留中の節が開いている (既定は畳む)");
    assert.equal(await snzSummary(page).innerText(), "保留中 (5)");
    assert.equal(await scard(page, "t-A1").isVisible(), false, "畳んだ節の中のカードが見えている");
    await openSnzSection(page);
    assert.equal(await scard(page, "t-A1").isVisible(), true, "開いた節の中のカードが見えない");
    assert.deepEqual(await snzSection(page).locator("section.snzcard .id").allInnerTexts(), ["t-A1", "t-A2", "t-A3", "t-A5", "t-A4"],
      "期日の早い順 (期限なしは最後) でない");
    assert.equal(await scard(page, "t-A1").locator(".snzinfo").innerText(), `保留: ${await localDay(page, 1)} に戻る (明日)`);
    assert.equal(await scard(page, "t-A2").locator(".snzinfo").innerText(), `保留: ${await localDay(page, 3)} に戻る (あと 3 日)`);
    assert.equal(await scard(page, "t-A2").locator(".snznotetext").innerText(), "補足: 仕様の確認待ち");
    assert.equal(await scard(page, "t-A4").locator(".snzinfo").innerText(), "保留: 期限なし");
    assert.equal(await scard(page, "t-A3").locator(".snznotetext").count(), 0, "補足が無いのに補足の行を出した");
    assert.equal(await scard(page, "t-A1").locator("textarea, .opt, button.primary, button.snz").count(), 0, "保留中のカードに回答の部品がある");
  });
});

test("保留: 成功のトーストの「解除」と保留中のカードの「保留を解除」は /api/unsnooze を {project,id} で送り、判断待ちに戻す。自分で戻したものは通知しない。戻る日が来て戻ったもの・問いが書き換わって戻ったもの (読み込みのデータ) は通知し、カードに戻った理由と補足を出す", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1")]), [snoozedItem("t-S1", "2999-12-31"), snoozedItem("t-S2", "2999-12-30", "見積もり待ち")])]);
  await withPage(s, async page => {
    await snoozeIt(page, "t-A1", "none");
    const act = okToasts(page).getByRole("button", { name: "解除" });
    await until(async () => (await act.count()) === 1, 3000, "保留の成功のトーストに「解除」が出る");
    assert.match(await okToasts(page).first().innerText(), /「title t-A1」を保留しました \(期限なし\)/);
    await until(async () => (await scard(page, "t-A1").count()) === 1, 3000, "保留中に出る");
    assert.equal((await page.locator("#count").textContent()).trim(), "0 件");
    await (await one(act, "トーストの「解除」")).click();
    await until(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "トーストの「解除」で判断待ちに戻る");
    assert.deepEqual(s.unsnoozes, [{ project: P1, id: "t-A1" }]);
    await openSnzSection(page);
    await (await one(scard(page, "t-S1").getByRole("button", { name: "保留を解除" }), "t-S1 の「保留を解除」")).click();
    await until(async () => (await qcard(page, "t-S1").count()) === 1, 3000, "「保留を解除」で判断待ちに戻る");
    assert.deepEqual(s.unsnoozes, [{ project: P1, id: "t-A1" }, { project: P1, id: "t-S1" }]);
    await until(async () => (await okToasts(page).filter({ hasText: "「title t-S1」の保留を解除しました" }).count()) === 1, 3000, "解除の知らせ");
    assert.equal(await page.evaluate(() => load()), true);
    assert.deepEqual(await notes(page), [], "自分で保留・解除したものを通知した");
    assert.equal(await qcard(page, "t-A1").locator(".snzback").count(), 0, "解除で戻したカードに期日の旨を出した");
    // 戻る日の来た t-S2 と、問いが書き換わった t-S3 は、サーバーの読みで判断待ちに戻る (snooze.expired / snooze.changed)
    const p = s.projects[0];
    p.snoozed = p.snoozed.filter(i => i.id !== "t-S2");
    p.human.push({ ...item("t-S2"), snooze: { until: "2026-11-01", note: "見積もり待ち", expired: true, changed: false } });
    p.human.push({ ...item("t-S3"), snooze: { until: "2999-12-31", note: "", expired: false, changed: true } });
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await qcard(page, "t-S2").count()) === 1 && (await qcard(page, "t-S3").count()) === 1, 3000,
      "保留から戻ったものが判断待ちに出る");
    assert.deepEqual(await notes(page), [["判断待ちが増えました", "2 件"]]);
    assert.equal(await (await one(qcard(page, "t-S2").locator(".snzback"), "戻る日が来た旨")).innerText(),
      "保留の戻る日 (2026-11-01) が来たので、判断待ちに戻りました。補足: 見積もり待ち");
    assert.equal(await (await one(qcard(page, "t-S3").locator(".snzback"), "問いが書き換わった旨")).innerText(),
      "保留の後に問いが書き換わったので、判断待ちに戻りました");
    await until(async () => (await snzSection(page).count()) === 0, 3000, "保留中が無くなれば節ごと外れる");
    assert.equal((await page.locator("#count").textContent()).trim(), "4 件");
    // 戻った旨は、データの snooze が消えれば (ほかで解除された) カードから消える
    p.human = p.human.map(i => (i.id === "t-S2" ? { ...i, snooze: null } : i));
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await qcard(page, "t-S2").locator(".snzback").count()) === 0, 3000, "snooze の消えたカードが作り直される");
  }, {}, fakeNotification);
});

test("保留: サーバーの入れ替え直後に、戻る日を読み残した保留 (snooze: null) が判断待ちに出ても「判断待ちが増えました」を出さない", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1")]), [snoozedItem("t-S1", "2999-12-31", "", { labels: ["human", "snoozed"] }), snoozedItem("t-S2", null, "", { labels: ["human", "snoozed"] })])]);
  await withPage(s, async page => {
    assert.equal(await page.evaluate(() => load()), true);
    const p = s.projects[0];
    p.human.push(...p.snoozed.map(i => ({ ...i, snooze: null })));
    p.snoozed = [];
    assert.equal(await page.evaluate(() => load()), true);
    assert.deepEqual(await notes(page), [], "サーバーの入れ替えだけで「判断待ちが増えました」を出した");
  }, {}, fakeNotification);
});

test("保留: 「保留する」を続けて 2 回押しても、「保留を解除」を 2 回押しても、トーストの「解除」と保留中のカードの「保留を解除」を重ねて押しても、送るのは 1 回", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1"), item("t-B2")]), [snoozedItem("t-S1", "2999-12-31")])]);
  await withPage(s, async page => {
    const release = s.hold("snooze");
    await openSnooze(page, "t-A1");
    await (await one(snzPanel(page, "t-A1").locator('.snzopt[data-kind="d1"]'), "期間 明日まで")).click();
    const go = await one(snzPanel(page, "t-A1").locator("button.snzgo"), "「保留する」");
    await go.evaluate(b => { b.click(); b.click(); });
    await page.waitForTimeout(100);
    release();
    await until(async () => (await scard(page, "t-A1").count()) === 1, 3000, "保留中に出る");
    assert.equal(s.snoozes.length, 1, "2 回押しで 2 回送った");
    const releaseU = s.hold("unsnooze");
    await openSnzSection(page);
    await (await one(scard(page, "t-S1").getByRole("button", { name: "保留を解除" }), "t-S1 の「保留を解除」")).evaluate(x => { x.click(); x.click(); });
    const act = await one(okToasts(page).getByRole("button", { name: "解除" }), "トーストの「解除」");
    const onCard = await (await one(scard(page, "t-A1").getByRole("button", { name: "保留を解除" }), "t-A1 の「保留を解除」")).elementHandle();
    await act.click();
    await onCard.evaluate(x => x.click());       // 押した後に外れていく (畳む) 要素でも、押せば送りかける
    await page.waitForTimeout(100);
    releaseU();
    await until(async () => (await qcard(page, "t-S1").count()) === 1 && (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに戻る");
    assert.deepEqual(s.unsnoozes.map(b => b.id).sort(), ["t-A1", "t-S1"], "解除を重ねて押して 2 回以上送った");
  });
});

test("保留: 書けなかった失敗 (502) ではカードが戻って .failed が付き、欄の選択と補足が残る。ラベルだけ失敗 (記録済み) なら「保留は記録済み」と知らせ、判断待ちに残っていればカードを戻し、保留中に居ればそこに出す (回答の下書きは残す)。gone は「もう判断待ちにありませんでした」と知らせて今の状態を出す", async () => {
  const s = new Script([proj(P1, [item("t-A1"), item("t-A2"), item("t-A3"), item("t-A4"), item("t-B9")])]);
  s.snoozeResult = body => {
    if (body.id === "t-A1") return { status: 502, body: { error: "bd boom", stage: "comment", written: false, op: "snooze" } };
    // ラベルが付かなかった (サーバーはラベルで保留中と読むので、判断待ちに残る)
    if (body.id === "t-A2") return { status: 502, body: { error: "label boom", stage: "label", written: true, op: "snooze" } };
    // 応答は失敗だが、保留中に居る (ラベルは付いた)
    if (body.id === "t-A4") { s.moveToSnoozed(body); return { status: 502, body: { error: "label boom", stage: "label", written: true, op: "snooze" } }; }
    s.moveToAnswered(body);
    return { status: 400, body: { error: "このチケットは判断待ちに無い", code: "gone", op: "snooze" } };
  };
  await withPage(s, async page => {
    await snoozeIt(page, "t-A1", "d3", "keep me");
    await until(async () => (await page.locator("section.card.failed").count()) === 1, 3000, "カードが戻る");
    await one(qcard(page, "t-A1"), "戻った t-A1");
    assert.equal(await snzPanel(page, "t-A1").isVisible(), true, "戻ったカードの保留の欄が閉じた");
    assert.equal(await snzPanel(page, "t-A1").locator("input.snznote").inputValue(), "keep me");
    assert.equal(await snzPanel(page, "t-A1").locator('.snzopt[data-kind="d3"]').getAttribute("aria-pressed"), "true");
    await snoozeIt(page, "t-A2", "d1");
    await until(async () => (await failToast(page).filter({ hasText: "保留は記録済みですが、ラベルの付け替えだけ失敗しました" }).count()) === 1,
      3000, "判断待ちに残ったときの「保留は記録済み」の知らせ");
    await until(async () => (await page.locator(`section.card.failed:has(.id:text-is("t-A2"))`).count()) === 1, 3000, "t-A2 のカードが戻る");
    await (await one(qcard(page, "t-A4").locator("textarea"), "t-A4 の補足欄")).fill("下書き A4");
    await snoozeIt(page, "t-A4", "d1");
    await until(async () => (await failToast(page).filter({ hasText: "保留は記録済みです (ラベルの付け替えだけ失敗)。保留中の一覧を確認してください" }).count()) === 1,
      3000, "保留中に居たときの「保留は記録済み」の知らせ");
    await until(async () => (await scard(page, "t-A4").count()) === 1, 3000, "保留中に出る");
    await until(async () => (await qcard(page, "t-A4").count()) === 0, 3000, "判断待ちには戻さない (畳み終わって消える)");
    s.moveFromSnoozed({ project: P1, id: "t-A4" });                 // ほかのタブで解除された
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await qcard(page, "t-A4").count()) === 1, 3000, "判断待ちに戻る");
    assert.equal(await qcard(page, "t-A4").locator("textarea").inputValue(), "下書き A4", "保留の失敗で回答の下書きを消した");
    await snoozeIt(page, "t-A3", "none");
    await until(async () => (await failToast(page).filter({ hasText: "もう判断待ちにありませんでした" }).count()) === 1, 3000, "gone の知らせ");
    await until(async () => (await acard(page, "t-A3").count()) === 1 && (await qcard(page, "t-A3").count()) === 0, 3000, "今の状態 (回答済み) を出す");
  });
});

test("保留: 絞り込みの「保留中: 含めない」で保留中の節ごと出さず (URL に snoozed=hide)、戻せば出る。検索は保留中のカードにも効く。保留中が無ければ選択肢を出さない", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1")]), [snoozedItem("t-S1", "2999-12-31"), snoozedItem("t-S2")])]);
  await withPage(s, async page => {
    const fld = page.locator("label.fld:has(#fsnoozed)");
    await one(fld, "「保留中」の選択肢");
    assert.equal(await snzSummary(page).innerText(), "保留中 (2)");
    await page.selectOption("#fsnoozed", "hide");
    await until(async () => (await snzSection(page).count()) === 0, 3000, "保留中の節が消える");
    assert.match(page.url(), /[?&]snoozed=hide(&|$)/);
    assert.equal(await qcard(page, "t-A1").count(), 1, "判断待ちまで隠した");
    await page.selectOption("#fsnoozed", "");
    await until(async () => (await snzSection(page).count()) === 1, 3000, "保留中の節が戻る");
    assert.doesNotMatch(page.url(), /snoozed=/);
    await (await one(page.locator("#fq"), "検索欄")).fill("t-S2");
    await until(async () => (await snzSummary(page).innerText()) === "保留中 (1)", 3000, "検索が保留中に効く");
    assert.deepEqual([await scard(page, "t-S2").count(), await scard(page, "t-S1").count()], [1, 0]);
    await page.locator("#fq").fill("");
    s.projects[0].snoozed = [];
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await snzSection(page).count()) === 0, 3000, "保留中の節が消える");
    assert.equal(await fld.isVisible(), false, "保留中が無いのに「保留中」の選択肢が出ている");
  });
});

test("保留: 全体を優先度順の表示でも、保留中はページの最後の 1 つの節に出て、どちらの表示でも head にプロジェクトの名前が出る", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1")]), [snoozedItem("t-S1", "2999-12-31")]),
    withSnoozed(proj(P2, [item("t-B1")]), [snoozedItem("t-S2", "2999-12-30")])]);
  const shown = page => snzSection(page).locator("section.snzcard").evaluateAll(cs => cs.map(c => {
    const pj = c.querySelector(".head .pj");
    return [c.querySelector(".id").textContent, pj && pj.offsetWidth > 0 ? pj.textContent : null];
  }));
  await withPage(s, async page => {
    await one(snzSection(page), "保留中の節");
    await openSnzSection(page);
    assert.deepEqual(await shown(page), [["t-S2", "beta"], ["t-S1", "alpha"]], "プロジェクトごとの表示");
    await setView(page, "prio");
    await until(async () => (await snzSection(page).count()) === 1, 2000, "全体の表示で保留中の節が 1 つ");
    assert.deepEqual(await shown(page), [["t-S2", "beta"], ["t-S1", "alpha"]], "全体の表示");
    assert.equal(await page.evaluate(() => document.getElementById("main").lastElementChild.matches("details.snz")), true, "節がページの最後に無い");
  });
});

test("保留: snoozed の key を返さない古いサーバーでも出て、snoozed が配列でない・要素が壊れているデータは使わない。保留の情報の形が崩れていても、読める値だけで出す", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1")]), [snoozedItem("t-S1", "2999-12-31", "メモ")])]);
  await withPage(s, async page => {
    assert.equal(await snzSummary(page).innerText(), "保留中 (1)");
    for (const bad of ["x", { a: 1 }, 5, true, [{ id: 5 }], [null], [{ ...item("t-S9"), q: "not an object" }]]) {
      s.queue.push({ projects: [{ ...s.snapshot().projects[0], snoozed: bad }] });
      assert.equal(await page.evaluate(() => load()), false, `snoozed=${JSON.stringify(bad)} を読めたことにした`);
      assert.equal(await snzSummary(page).innerText(), "保留中 (1)", "使わないデータで保留中を書き換えた");
      if (bad === "x") assert.equal(await failToast(page).filter({ hasText: "想定外のデータ (プロジェクトの形)" }).count(), 1,
        "配列でない snoozed を、プロジェクトの形の誤りとして知らせていない");
    }
    s.queue.push({ projects: [proj(P1, [item("t-A1")])] });             // 古いサーバー: snoozed の key が無い
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await snzSection(page).count()) === 0, 3000, "保留中の無いデータで節が外れる");
    await one(qcard(page, "t-A1"), "t-A1");
    s.queue.push({ projects: [{ ...proj(P1, [{ ...item("t-A1"), snooze: { until: "2026-01-01", note: "x", expired: "yes", changed: 1 } }]),
      snoozed: [{ ...item("t-S1"), snooze: { until: 5, note: ["x"] } }, { ...item("t-S2"), snooze: "garbage" },
        { ...item("t-S3"), snooze: { until: "2999-12-31", note: "a\u0000b" } }] }] });
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await snzSection(page).count()) === 1, 3000, "保留中の節が出る");
    await openSnzSection(page);
    assert.equal(await scard(page, "t-S1").locator(".snzinfo").innerText(), "保留: 期限なし", "読めない期日を期限なしとして出さない");
    assert.equal(await scard(page, "t-S1").locator(".snznotetext").count(), 0, "文字列でない補足を出した");
    assert.equal(await scard(page, "t-S2").locator(".snzinfo").innerText(), "保留: 期限なし");
    assert.equal(await scard(page, "t-S3").locator(".snznotetext").textContent(), "補足: a\u0000b");
    assert.equal(await qcard(page, "t-A1").locator(".snzback").count(), 0, "expired・changed が true でないのに戻った旨を出した");
  });
});

test("保留: 保留中が無くなると節ごと外れ、次に保留したときは畳んだ節で出直す (前に開いていても)", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1")]), [snoozedItem("t-S1", "2999-12-31")])]);
  await withPage(s, async page => {
    await openSnzSection(page);
    await (await one(scard(page, "t-S1").getByRole("button", { name: "保留を解除" }), "t-S1 の「保留を解除」")).click();
    await until(async () => (await snzSection(page).count()) === 0, 3000, "保留中が無くなれば節ごと外れる");
    await snoozeIt(page, "t-A1", "none");
    await until(async () => (await scard(page, "t-A1").count()) === 1, 3000, "保留中に出る");
    await one(snzSection(page), "保留中の節");
    assert.equal(await snzSection(page).evaluate(d => d.open), false, "出直した節が開いている (前の節を使い回した)");
  });
});

test("保留: 判断待ちの最後のカードに答えたとき、フォーカスは畳んだ保留中の節の中のカードではなく、前のカードへ移る", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A0"), item("t-A1")]), [snoozedItem("t-S1", "2999-12-31")])]);
  await withPage(s, async page => {
    await one(qcard(page, "t-A1"), "t-A1");
    await answerFirst(page, "t-A1");
    await until(async () => (await focusedCard(page)) === "t-A0", 3000, "フォーカスが前のカード (t-A0) へ移る");
  });
});

test("保留: 期間の選択・日付の下限・残りの日数は、ブラウザの TZ ではなくサーバーの今日 (/api/items の today) で数える。today の形が崩れていればブラウザの日付で数える", async () => {
  const s = new Script([withSnoozed(proj(P1, [item("t-A1"), item("t-A2")]), [snoozedItem("t-S1", "2031-02-02")])]);
  s.today = "2031-01-30";                            // ブラウザの今日 (テストを流す日) と違う日
  await withPage(s, async page => {
    assert.notEqual(await localDay(page, 0), s.today, "前提: ブラウザの今日がサーバーの今日と違う");
    await openSnzSection(page);
    assert.equal(await scard(page, "t-S1").locator(".snzinfo").innerText(), "保留: 2031-02-02 に戻る (あと 3 日)");
    await openSnooze(page, "t-A1");
    const dateIn = await one(snzPanel(page, "t-A1").locator('input[type="date"]'), "日付の入力");
    assert.equal(await dateIn.getAttribute("min"), "2031-01-31", "日付の下限がサーバーの明日でない");
    await dateIn.fill("2031-01-30");
    assert.equal(await snzPanel(page, "t-A1").locator("button.snzgo").isEnabled(), false, "サーバーの今日を選べる");
    await (await one(snzPanel(page, "t-A1").locator(".snzcancel"), "「やめる」")).click();
    await snoozeIt(page, "t-A1", "d1");
    await until(async () => (await okToasts(page).filter({ hasText: "「title t-A1」を保留しました (2031-01-31 に戻る)" }).count()) === 1,
      3000, "保留の知らせに戻る日が出る");
    await snoozeIt(page, "t-A2", "d7");
    assert.deepEqual(s.snoozes.map(b => b.until), ["2031-01-31", "2031-02-06"], "期間をサーバーの今日から数えていない (月をまたぐ)");
    s.today = "2031-02-30";                          // 形は合うが日付でない: 使わない
    assert.equal(await page.evaluate(() => load()), true);
    assert.match(await scard(page, "t-A2").locator(".snzinfo").innerText(), /^保留: 2031-02-06 に戻る \(あと \d+ 日\)$/);
    assert.notEqual(await scard(page, "t-A2").locator(".snzinfo").innerText(), "保留: 2031-02-06 に戻る (あと 7 日)",
      "日付でない today で数えた");
  });
});

test("保留: snoozed のラベルは、カードの head のラベルの印にも「ラベル」の絞り込みにも出さない (保留中の節と「保留中」の絞り込みで出す)", async () => {
  const back = { ...item("t-A1", { labels: ["snoozed", "area:ui"] }), snooze: { until: "2026-11-01", note: "", expired: true, changed: false } };
  const s = new Script([{ ...withSnoozed(proj(P1, [back], [answeredItem("t-A9")]), [snoozedItem("t-S1", "2999-12-31", "", { labels: ["snoozed", "x:y"] })]) }]);
  s.projects[0].answered[0].labels = ["snoozed", "answered"];
  await withPage(s, async page => {
    const spans = card => card.locator(".head > span:not(.prio):not(.id)").allInnerTexts();
    const qs = await spans(await one(qcard(page, "t-A1"), "戻ったカード"));
    assert.equal(qs.length, 2, qs);
    assert.equal(qs[1], "area:ui", "戻ったカードに snoozed の印を出した");
    await openAnswered(page);
    assert.equal((await spans(await one(acard(page, "t-A9"), "回答済みのカード"))).length, 1, "回答済みのカードに snoozed の印を出した");
    await openSnzSection(page);
    const ss = await spans(await one(scard(page, "t-S1"), "保留中のカード"));
    assert.equal(ss.length, 2, ss);
    assert.equal(ss[1], "x:y", "保留中のカードに snoozed の印を出した");
    assert.deepEqual(await page.locator("#flabel option").allInnerTexts(), ["すべてのラベル", "area:ui", "x:y"], "「ラベル」の絞り込みに snoozed を出した");
  });
});

// ---- 回答済み・反映待ちの節は、初めは畳む -------------------------------------------------------------------------
// 節の中身 (カード) を details で包み、初めは閉じる。summary に「回答済み・反映待ち (n)」を出し、見出しはその中。開いたかどうかは
// プロジェクトの path (全体を優先度順の表示は固定の key) で覚え、読み込みでも、節が空になって外れた後の作り直しでも閉じない。
// 回答の後のフォーカスは、次のカードが畳んだ節の中なら、そこへ移さず、ほかの見えるカードか畳んだ節の summary へ移す

const ansSections = page => page.locator("#main details.ansgrp");
// プロジェクトごとの表示で、そのプロジェクトの回答済みの節 (details)
const ansOf = (page, path) => page.locator(`#main .proj:has(> h2 .path[title="${path}"]) details.ansgrp`);
// 節 (details) が開いているか。節が 1 つでなければ assert の失敗にする (無い要素を読む待ちの時間切れにしない)
async function isOpen(loc, what) {
  assert.equal(await loc.count(), 1, `${what} がちょうど 1 つでない`);
  return loc.evaluate(d => d.open);
}
// 節の見出しの行 (summary)。ちょうど 1 つあることを確かめてから返す (無い要素を押す・読む待ちの時間切れ (ERROR) を assertion の失敗にする)。
// path が無ければ、ページに 1 つだけの節 (1 プロジェクトだけの表示か、全体の表示) の summary
async function ansSum(page, path) {
  const sum = (path ? ansOf(page, path) : ansSections(page)).locator(":scope > summary");
  assert.equal(await sum.count(), 1, `${path || "回答済みの節"} の summary がちょうど 1 つでない`);
  return sum;
}
const ansText = async (page, path) => (await (await ansSum(page, path)).innerText()).trim();
// 判断待ちのカードに答える。押す前に「回答する」が見えていることを assert する (畳んだ節の中などで見えないと、押す待ちの時間切れ (ERROR) でなく assertion の失敗にする)
async function answerVisible(page, id) {
  await oneQ(page, id);
  assert.equal(await qcard(page, id).locator("button.primary").isVisible(), true, `${id} の「回答する」が見えていない`);
  await answerFirst(page, id);
}
// フォーカスのある要素: 畳んだ節の summary なら {kind: "summary", sec, proj}、カードそのものなら {kind: "card", id}
const focusInfo = page => page.evaluate(() => {
  const a = document.activeElement;
  if (!a || a === document.body) return { kind: "body" };
  if (a.matches("details.ansgrp > summary, details.snz > summary")) {
    const pr = a.closest(".proj");
    return { kind: "summary", sec: a.parentElement.classList.contains("snz") ? "snz" : "ans", proj: pr ? pr.querySelector("h2 .path").title : null, text: a.textContent.trim() };
  }
  if (a.matches("section.card")) return { kind: "card", id: a.querySelector(".id").textContent };
  return { kind: a.tagName.toLowerCase() };
});

test("回答済みの節: 回答済みが 2 件あるデータで、初めは畳んであり (.ans が見えない)、summary に件数が出る。summary を押す (Enter でも) と見え、読み込みでも開いたまま。もう一度押すと畳み、読み込みでも畳んだまま", async () => {
  const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A8"), answeredItem("t-A9")])]);
  await withPage(s, async page => {
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), false, "最初から開いている");
    assert.equal(await ansSections(page).getAttribute("data-kind"), "ans", "節の種類 (data-kind) が ans でない");
    assert.equal(await ansSections(page).evaluate(d => d.classList.contains("grp")), true, "節の入れ物の class (grp) が無い");
    assert.equal(await (await ansSum(page)).isVisible(), true, "畳んだ節の summary が見えていない");
    assert.equal(await ansText(page), "回答済み・反映待ち (2)");
    assert.equal(await (await ansSum(page)).locator("h3").count(), 1, "見出し (h3) が summary の中に無い");
    assert.equal(await page.locator("#main .ans").count(), 2, "回答済みのカードの本文が 2 つでない");
    assert.equal(await acard(page, "t-A8").count(), 1);
    assert.equal(await acard(page, "t-A9").count(), 1);
    for (const id of ["t-A8", "t-A9"]) {
      assert.equal(await acard(page, id).locator(".ans").isVisible(), false, `${id}: 畳んだ節の中の .ans が見えている`);
      assert.equal(await acard(page, id).locator("button.ghost").count(), 1, `${id}: 取り消しのボタンがちょうど 1 つでない`);
      assert.equal(await acard(page, id).locator("button.ghost").isVisible(), false, `${id}: 畳んだ節の中の取り消しが見えている`);
    }
    assert.equal(await qcard(page, "t-A1").locator("button.primary").isVisible(), true, "判断待ちのカードまで畳まれた");
    await (await ansSum(page)).click();
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), true, "summary を押しても開かない");
    for (const id of ["t-A8", "t-A9"]) {
      assert.equal(await acard(page, id).locator(".ans").isVisible(), true, `${id}: 開いたのに .ans が見えない`);
      assert.equal(await acard(page, id).getByRole("button", { name: "取り消して判断待ちに戻す" }).isVisible(), true, `${id}: 開いたのに取り消しが見えない`);
    }
    // 読み込みでは閉じない (件数が変わっても、カードが作り直されても)
    assert.equal(await page.evaluate(() => load()), true);
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), true, "読み込みで閉じた");
    s.projects[0].answered.push(answeredItem("t-A7"));
    s.projects[0].answered[0] = answeredItem("t-A8", "[humanq] 回答: 案B (b)\n", "asig-again");
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await ansText(page)) === "回答済み・反映待ち (3)", 3000, "件数が 3 になる");
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), true, "件数が変わる読み込みで閉じた");
    assert.equal(await acard(page, "t-A7").locator(".ans").isVisible(), true, "開いた節に入った新しいカードが見えない");
    assert.match(await acard(page, "t-A8").locator(".ans").innerText(), /案B/);
    // もう一度押すと畳む。畳んだまま読み込んでも開かない。キーボード (summary にフォーカスして Enter) でも開く
    await (await ansSum(page)).click();
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), false, "もう一度押しても畳まない");
    assert.equal(await page.evaluate(() => load()), true);
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), false, "畳んだ節が読み込みで開いた");
    await (await ansSum(page)).focus();
    await page.keyboard.press("Enter");
    assert.equal(await isOpen(ansSections(page), "回答済みの節"), true, "Enter で開かない");
    assert.equal(await acard(page, "t-A9").locator(".ans").isVisible(), true);
  });
});

test("回答済みの節: 開いたかどうかはプロジェクトごと (全体を優先度順の表示は別の 1 つ) に覚え、節が空になって外れた後の作り直しや、表示の切り替えでも保たれる。覚えているのは開いた節だけで、ほかの節は畳んだまま", async () => {
  const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A9")]), proj(P2, [item("t-B1")], [answeredItem("t-B9")])]);
  await withPage(s, async page => {
    assert.equal(await ansSections(page).count(), 2);
    assert.deepEqual([await isOpen(ansOf(page, P1), "alpha の節"), await isOpen(ansOf(page, P2), "beta の節")], [false, false]);
    await (await ansSum(page, P1)).click();                                   // alpha だけ開く
    assert.deepEqual([await isOpen(ansOf(page, P1), "alpha の節"), await isOpen(ansOf(page, P2), "beta の節")], [true, false], "ほかのプロジェクトの節まで開いた");
    assert.equal(await acard(page, "t-A9").locator(".ans").isVisible(), true);
    assert.equal(await acard(page, "t-B9").locator(".ans").isVisible(), false, "開いていないプロジェクトの回答済みが見えている");
    // alpha の回答済みが 0 件になって節が外れ、また回答済みが出たとき: alpha の節は開いたまま、beta の節は畳んだまま
    s.projects[0].answered = [];
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await ansOf(page, P1).count()) === 0, 3000, "alpha の空になった節が外れる");
    s.projects[0].answered = [answeredItem("t-A9")];
    assert.equal(await page.evaluate(() => load()), true);
    await until(async () => (await ansOf(page, P1).count()) === 1, 3000, "alpha の節が作り直される");
    assert.deepEqual([await isOpen(ansOf(page, P1), "alpha の節"), await isOpen(ansOf(page, P2), "beta の節")], [true, false], "作り直した節の開閉が覚えと違う");
    // 全体を優先度順の表示: 別の節なので、初めは畳む。そこで開いても、プロジェクトごとの表示の節は変わらない
    await setView(page, "prio");
    await until(async () => (await page.locator(".proj").count()) === 0, 3000, "プロジェクトごとの入れ物が外れる");
    assert.equal(await isOpen(page.locator("#main .flat details.ansgrp"), "全体の表示の回答済みの節"), false, "全体の表示の節が、プロジェクトごとの開閉を引き継いだ");
    assert.equal(await ansText(page), "回答済み・反映待ち (2)");              // 全体の表示では、節はページに 1 つ
    await (await ansSum(page)).click();
    assert.equal(await isOpen(page.locator("#main .flat details.ansgrp"), "全体の表示の回答済みの節"), true);
    assert.equal(await page.evaluate(() => load()), true);
    assert.equal(await isOpen(page.locator("#main .flat details.ansgrp"), "全体の表示の回答済みの節"), true, "全体の表示の節が読み込みで閉じた");
    await setView(page, "proj");
    await until(async () => (await page.locator(".flat").count()) === 0 && (await ansSections(page).count()) === 2, 3000, "プロジェクトごとの表示に戻る");
    assert.deepEqual([await isOpen(ansOf(page, P1), "alpha の節"), await isOpen(ansOf(page, P2), "beta の節")], [true, false], "表示を切り替えて戻ると、開閉が変わった");
    await setView(page, "prio");
    await until(async () => (await page.locator(".proj").count()) === 0, 3000, "プロジェクトごとの入れ物が外れる");
    assert.equal(await isOpen(page.locator("#main .flat details.ansgrp"), "全体の表示の回答済みの節"), true, "全体の表示の開閉を覚えていない");
    // 閉じた節は覚えから外れる: alpha の節を畳んでから作り直すと、畳んだまま (開いたままの覚えが残らない)。全体の表示の節は開いたまま
    await setView(page, "proj");
    await until(async () => (await page.locator(".flat").count()) === 0 && (await ansSections(page).count()) === 2, 3000, "プロジェクトごとの表示に戻る");
    await (await ansSum(page, P1)).click();
    assert.equal(await isOpen(ansOf(page, P1), "alpha の節"), false, "前提: alpha の節が畳まれていない");
    await setView(page, "prio");
    await until(async () => (await page.locator(".proj").count()) === 0, 3000, "プロジェクトごとの入れ物が外れる");
    await setView(page, "proj");
    await until(async () => (await page.locator(".flat").count()) === 0 && (await ansSections(page).count()) === 2, 3000, "プロジェクトごとの表示に戻る");
    assert.deepEqual([await isOpen(ansOf(page, P1), "alpha の節"), await isOpen(ansOf(page, P2), "beta の節")], [false, false], "畳んだ節が、作り直したときに開いた");
    await setView(page, "prio");
    await until(async () => (await page.locator(".proj").count()) === 0, 3000, "プロジェクトごとの入れ物が外れる");
    assert.equal(await isOpen(page.locator("#main .flat details.ansgrp"), "全体の表示の回答済みの節"), true, "プロジェクトごとの節を畳んだら、全体の表示の節まで畳んだ");
  });
});

test("回答済みの節: 見出しは summary の中に置く (プロジェクトごとは h3、全体を優先度順は h2)。見出しの階層・文言・件数と、見出しの見た目 (小さい字・止まらない・summary の行に収まる) は変えない。節の上の余白は 28px、開いたときだけ summary の下に 10px", async () => {
  const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A8"), answeredItem("t-A9")]), proj(P2, [], [answeredItem("t-B9")])]);
  await withPage(s, async page => {
    assert.deepEqual(await headings(page), ["h1:判断待ち", "h2:alpha", "h3:判断 (1)", "h3:回答済み・反映待ち (2)", "h2:beta", "h3:回答済み・反映待ち (1)"]);
    assert.equal(await page.locator(".proj .grp > summary > h3").count(), 2, "回答済みの見出しが summary の中の h3 でない");
    assert.equal(await page.locator(".proj .grp > h3").count(), 1, "判断の節の見出しが入れ物の直下の h3 でない");
    const look = () => ansOf(page, P1).evaluate(d => {
      const cs = e => getComputedStyle(e), sum = d.querySelector(":scope > summary"), h = sum.querySelector("h3");
      return { marginTop: cs(d).marginTop, sumMb: cs(sum).marginBottom, sumFs: cs(sum).fontSize, hDisplay: cs(h).display, hMargin: cs(h).margin,
        hFs: cs(h).fontSize, hPos: cs(h).position, oneLine: sum.getBoundingClientRect().height < 2 * parseFloat(cs(h).lineHeight) };
    });
    assert.deepEqual(await look(), { marginTop: "28px", sumMb: "0px", sumFs: "14px", hDisplay: "inline", hMargin: "0px", hFs: "14px", hPos: "static", oneLine: true });
    // キーボードで届いた summary には、カードと同じ色の 2px の枠が出る (ブラウザの既定の枠ではない)
    await (await ansSum(page, P1)).focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.matches("details.ansgrp > summary")), true, "前提: Tab で summary にフォーカスが届かない");
    assert.deepEqual(await page.evaluate(() => { const c = getComputedStyle(document.activeElement); return [c.outlineStyle, c.outlineWidth, c.outlineOffset]; }),
      ["solid", "2px", "2px"], "キーボードで届いた summary の枠が、カードと同じ形でない");
    await (await ansSum(page, P1)).click();
    assert.equal((await look()).sumMb, "10px", "開いた節の summary の下に余白が無い");
    await setView(page, "prio");
    await until(async () => (await page.locator(".proj").count()) === 0, 3000, "プロジェクトごとの入れ物が外れる");
    assert.deepEqual(await headings(page), ["h1:判断待ち", "h2:判断待ち (1)", "h2:回答済み・反映待ち (3)"]);
    assert.equal(await page.locator(".flat details.ansgrp > summary > h2.sec").count(), 1, "全体の表示の回答済みの見出しが summary の中の h2 でない");
    assert.equal(await page.locator(".flat > .grp > h2.sec").count(), 1, "判断待ちの見出しが入れ物の直下の h2 でない");
    const flat = await page.locator(".flat details.ansgrp").evaluate(d => {
      const cs = e => getComputedStyle(e), sum = d.querySelector(":scope > summary"), h = sum.querySelector("h2");
      return { marginTop: cs(d).marginTop, sumMb: cs(sum).marginBottom, hDisplay: cs(h).display, hMargin: cs(h).margin, hFs: cs(h).fontSize, hPos: cs(h).position };
    });
    assert.deepEqual(flat, { marginTop: "28px", sumMb: "0px", hDisplay: "inline", hMargin: "0px", hFs: "14px", hPos: "static" });
  });
});

test("回答済みの節: 回答の後のフォーカスは、次のカードが畳んだ節の中なら、そこへ移さず、見えるカード (次、無ければ前) か、畳んだ節の summary へ移る。見えているものが移り先になる (どちらの表示でも)", async () => {
  const answerOne = answerVisible;       // このテストの答える操作は、押す前に「回答する」が見えていることを assert するもの (見えなければ時間切れでなく assertion の失敗)
  // 1. 判断待ちの最後の 1 件に答える: 残りは畳んだ回答済みの節だけ → その summary (見えている) へ。カードの中の「チケット本文」の summary ではない
  for (const view of ["proj", "prio"]) {
    const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A9")])]);
    await withPage(s, async page => {
      if (view === "prio") await setView(page, "prio");
      assert.equal(await isOpen(ansSections(page), "回答済みの節"), false, `${view}: 前提: 回答済みの節が畳んでない`);
      s.hold("answer");
      await answerOne(page, "t-A1");
      const f = await focusInfo(page);
      assert.deepEqual([f.kind, f.sec], ["summary", "ans"], `${view}: フォーカスが畳んだ節の summary へ移らない ${JSON.stringify(f)}`);
      assert.equal(await page.locator(":focus").count(), 1);
      assert.equal(await page.locator(":focus").isVisible(), true, `${view}: フォーカスが見えていない要素にある`);
      assert.equal(await isOpen(ansSections(page), "回答済みの節"), false, `${view}: 移しただけで節が開いた`);
      // Enter の押し直し・リピートで取り消しは送られない (畳んだ中のカードの取り消しにフォーカスは移っていない)
      await page.keyboard.press("Enter");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(300);
      assert.deepEqual(s.undos, [], `${view}: 取り消しが送られた`);
      assert.deepEqual(s.answers.map(a => a.id), ["t-A1"]);
    });
  }
  // 2. 次が畳んだ節の中でも、判断待ちのカードがほかにあれば、そのカード (前) そのものへ。節の summary へは移らない。次の見えるカードがあれば次へ
  const s2 = new Script([proj(P1, [item("t-A1"), item("t-A2")], [answeredItem("t-A9")])]);
  await withPage(s2, async page => {
    s2.hold("answer");
    await answerOne(page, "t-A2");
    assert.deepEqual(await focusInfo(page), { kind: "card", id: "t-A1" }, "最後の判断待ちに答えたとき、前の見えるカードへ移らない");
    await answerOne(page, "t-A1");
    assert.equal((await focusInfo(page)).kind, "summary", "残りが畳んだ節だけになったとき、summary へ移らない");
  });
  const s3 = new Script([proj(P1, [item("t-A1"), item("t-A2")], [answeredItem("t-A9")])]);
  await withPage(s3, async page => {
    s3.hold("answer");
    await answerOne(page, "t-A1");
    assert.deepEqual(await focusInfo(page), { kind: "card", id: "t-A2" }, "次の見えるカードへ移らない");
  });
  // 3. 畳んだ節が、カードの前だけにあるとき (別のプロジェクトの回答済み) は、その summary へ。前後の両方にあるときは、後ろ (同じプロジェクトの) のほうへ
  const s4 = new Script([proj(P1, [], [answeredItem("t-A9")]), proj(P2, [item("t-B1")])]);
  await withPage(s4, async page => {
    s4.hold("answer");
    await answerOne(page, "t-B1");
    assert.deepEqual(await focusInfo(page), { kind: "summary", sec: "ans", proj: P1, text: "回答済み・反映待ち (1)" }, "前にだけある畳んだ節の summary へ移らない");
  });
  const s5 = new Script([proj(P1, [], [answeredItem("t-A9")]), proj(P2, [item("t-B1")], [answeredItem("t-B9")])]);
  await withPage(s5, async page => {
    s5.hold("answer");
    await answerOne(page, "t-B1");
    const f = await focusInfo(page);
    assert.deepEqual([f.kind, f.sec, f.proj], ["summary", "ans", P2], `前後に畳んだ節があるとき、後ろの (同じプロジェクトの) summary へ移らない ${JSON.stringify(f)}`);
  });
  // 3'. 畳んだ節が 3 つ: 後ろにあるもののうち最も近いもの (最後のものではない) / 後ろに無ければ、前にあるもののうち最も近いもの (最初のものではない)
  const s7 = new Script([proj(P1, [], [answeredItem("t-A9")]), proj(P2, [item("t-B1")], [answeredItem("t-B9")]), proj("/work/gamma", [], [answeredItem("t-C9")])]);
  await withPage(s7, async page => {
    s7.hold("answer");
    await answerOne(page, "t-B1");
    const f = await focusInfo(page);
    assert.deepEqual([f.kind, f.sec, f.proj], ["summary", "ans", P2], `後ろにある畳んだ節のうち、最も近い (同じプロジェクトの) summary へ移らない ${JSON.stringify(f)}`);
  });
  const s8 = new Script([proj(P1, [], [answeredItem("t-A9")]), proj(P2, [], [answeredItem("t-B9")]), proj("/work/gamma", [item("t-C1")])]);
  await withPage(s8, async page => {
    s8.hold("answer");
    await answerOne(page, "t-C1");
    const f = await focusInfo(page);
    assert.deepEqual([f.kind, f.sec, f.proj], ["summary", "ans", P2], `前にある畳んだ節のうち、最も近い summary へ移らない ${JSON.stringify(f)}`);
  });
  // 3''. 保留中の畳んだ節も同じ (残りが畳んだ節だけなら、その summary へ)
  const s9 = new Script([{ ...proj(P1, [item("t-A1")]), snoozed: [{ ...item("t-S1"), snooze: { until: "2999-12-31", note: "", expired: false, changed: false } }] }]);
  await withPage(s9, async page => {
    s9.hold("answer");
    await answerOne(page, "t-A1");
    const f = await focusInfo(page);
    assert.deepEqual([f.kind, f.sec], ["summary", "snz"], `残りが畳んだ保留中の節だけのとき、その summary へ移らない ${JSON.stringify(f)}`);
    assert.equal(await page.locator(":focus").isVisible(), true);
  });
  // 3'''. 移り先が何も無い (見えるカードも畳んだ節も無い) ときは、何も移さない (例外を出さない)。withPage がページの例外の有無を検査する
  const s10 = new Script([proj(P1, [item("t-A1")])]);
  await withPage(s10, async page => {
    s10.hold("answer");
    await answerOne(page, "t-A1");
    // フォーカスは、畳んでいるカードの中の押したボタンに残る (畳み終わって外れるまで。inert でも activeElement のまま)。別の要素へは移らない
    const f = await focusInfo(page);
    assert.equal(["summary", "card"].includes(f.kind), false, `移り先が無いのに、フォーカスが ${JSON.stringify(f)} へ移った`);
    // 移り先を探す処理が例外で止まると、描き直しが途中で終わる (件数が 0 件に変わらない)。例外は画面側が握るので、ページの例外では分からない
    assert.equal((await page.locator("#count").textContent()).trim(), "0 件", "回答した直後の描き直しが終わっていない (移り先を探す処理が止まった)");
  });
  // 4. 節を開いてあれば、次のカードは回答済みのカードそのもの (今までどおり)
  const s6 = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A9")])]);
  await withPage(s6, async page => {
    await openAnswered(page);
    s6.hold("answer");
    await answerOne(page, "t-A1");
    assert.deepEqual(await focusInfo(page), { kind: "card", id: "t-A9" }, "開いた節の回答済みのカードへ移らない");
  });
});

test("回答済みの節: 畳んだ節の中のカードでも、トーストの「取り消す」は効き、節の件数が変わり、空になった節は外れる。動きを減らす設定でも同じ", async () => {
  for (const opts of [{}, { reducedMotion: "reduce" }]) {
    const s = new Script([proj(P1, [item("t-A1"), item("t-B2")])]);
    await withPage(s, async page => {
      await answerVisible(page, "t-A1");
      await until(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
      assert.equal(await isOpen(ansSections(page), "回答済みの節"), false, "回答済みの節が畳んでない");
      assert.equal(await ansText(page), "回答済み・反映待ち (1)");
      assert.equal(await okToasts(page).count(), 1, "成功のトーストが 1 つでない");
      assert.equal(await undoInToast(page).count(), 1, "トーストに「取り消す」が 1 つでない");
      await undoInToast(page).click({ timeout: 3000 });
      await until(() => s.undos.length === 1, 3000, "取り消しの POST");
      await until(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに戻る");
      await until(async () => (await ansSections(page).count()) === 0, 3000, "空になった回答済みの節が外れる");
    }, opts);
  }
});

test("回答済みの節: 畳んだ節の中に取り消しに失敗したカードがあると、節を開かなくても見出しに「(失敗 n 件)」が出る。失敗が無いときの見出しと開閉は変わらない (プロジェクトごとの表示・全体の表示)", async () => {
  for (const flat of [false, true]) {
    const s = new Script([proj(P1, [item("t-A1")], [answeredItem("t-A9")])]);
    const okUndo = s.undoResult;
    await withPage(s, async page => {
      if (flat) { await page.locator('#toc .viewbtn[data-view="prio"]').click(); await until(async () => (await page.locator(".flat details.ansgrp").count()) === 1, 3000, "全体の表示"); }
      const sec = ansSections(page);
      const sum = () => sec.locator(":scope > summary");
      assert.equal(await ansText(page), "回答済み・反映待ち (1)");
      assert.equal(await sum().evaluate(e => e.classList.contains("hasfail")), false, "失敗が無いのに警告の色");
      await answerVisible(page, "t-A1");
      await until(async () => (await acard(page, "t-A1").count()) === 1, 3000, "回答済みに出る");
      assert.equal(await ansText(page), "回答済み・反映待ち (2)", "失敗が無いのに見出しが変わった");
      // 畳んだまま、トーストの「取り消す」を、サーバーが断る (ラベルの付け替えの失敗)
      s.undoResult = () => ({ status: 502, body: { error: "label add boom", stage: "label", written: true, op: "undo" } });
      assert.equal(await isOpen(sec, "回答済みの節"), false, "前提: 節が開いている");
      await undoInToast(page).click({ timeout: 3000 });
      await until(() => s.undos.length === 1, 3000, "取り消しの POST");
      await until(async () => (await sec.locator("section.card.failed").count()) === 1, 3000, "失敗したカードが戻る");
      assert.equal(await isOpen(sec, "回答済みの節"), false, "失敗で節が開いた (開閉の覚えは変えない)");
      assert.equal(await ansText(page), "回答済み・反映待ち (2) (失敗 1 件)", "節を開かずに失敗が分からない");
      assert.equal(await sum().evaluate(e => e.classList.contains("hasfail")), true, "警告の色が付かない");
      // 開いても、見出しは同じ。開閉は押したとおり
      await openAnswered(page);
      assert.equal(await isOpen(sec, "回答済みの節"), true);
      assert.equal(await ansText(page), "回答済み・反映待ち (2) (失敗 1 件)");
      // 取り消しをやり直して成功すると、失敗の表示は消え、見出しは元の形に戻る
      s.undoResult = okUndo;
      await acard(page, "t-A1").getByRole("button", { name: "取り消して判断待ちに戻す" }).click();
      await until(async () => (await qcard(page, "t-A1").count()) === 1, 3000, "判断待ちに戻る");
      assert.equal(await ansText(page), "回答済み・反映待ち (1)", "成功した後も失敗の表示が残る");
      assert.equal(await sum().evaluate(e => e.classList.contains("hasfail")), false, "警告の色が残る");
    });
  }
});
