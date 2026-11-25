// Optional browser regression: NODE_PATH must point to an installed Playwright module.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { parseEnv } = require("node:util");

const root = path.resolve(__dirname, "..");
const live = process.argv.includes("--live");
const variables = live ? { ...parseEnv(fs.readFileSync(path.join(root, ".env"), "utf8")), ...process.env } : {};
const base = live ? variables.RAG_SKILL_BASE_URL : process.env.UI_TEST_URL || "http://127.0.0.1:8766";
const key = live ? variables.RAG_APP_KEY : "browser-fixture-key";
const fixture = {
  mode: "azure", provider: "fixture",
  collections: [{ id: "support", label: "Support" }, { id: "policies", label: "Policies" }],
  suggestions: { support: ["What fixed checkout?"], policies: ["Explain the policy."] },
  notice: "Browser test fixture, not a live Azure response.",
};

async function waitReady(page) {
  await page.waitForFunction(() => !document.querySelector("#send").disabled, null, { timeout: 90000 });
}

(async () => {
  const browser = await chromium.launch({ channel: process.env.UI_BROWSER_CHANNEL || "msedge", headless: true });
  let page;
  const errors = [];
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    let chatRequests = 0;
    page.on("request", request => {
      if (new URL(request.url()).pathname === "/api/chat" && request.method() === "POST") chatRequests++;
    });
    page.on("pageerror", error => errors.push(error.message));
    let failDelete = false, sequence = 0, lastCollection;
    const sessions = new Map();
    if (!live) {
      await page.route("**/api/**", async route => {
        const request = route.request();
        const url = new URL(request.url());
        const send = (status, json) => route.fulfill({ status, json });
        if (request.headers()["x-app-key"] !== key) return send(401, { detail: "Application key required" });
        if (url.pathname === "/api/config") return send(200, fixture);
        if (url.pathname === "/api/sessions" && request.method() === "POST") {
          const id = `fixture-session-${++sequence}`;
          sessions.set(id, request.postDataJSON().collection);
          return send(200, { session_id: id });
        }
        if (url.pathname.startsWith("/api/sessions/") && request.method() === "DELETE") {
          if (failDelete) { failDelete = false; return send(502, { detail: "Conversation cleanup unavailable" }); }
          return route.fulfill({ status: 204 });
        }
        if (url.pathname.endsWith("/status")) return send(200, { status: "ready" });
        if (url.pathname === "/api/chat") {
          lastCollection = sessions.get(request.postDataJSON().session_id);
          return send(200, {
            answer_id: "fixture-answer-id-123456789",
            answer: "HTTP 504: reduce concurrency.", sanitized_answer: "HTTP 504: reduce concurrency.",
            sanitized_question: request.postDataJSON().question,
            evidence: [{ id: "1", content: "Reduce concurrency.", collection: lastCollection }],
            restored: false,
            privacy: { answer_placeholders: 0, evidence_placeholders: 2, restored_occurrences: 0,
              blocked_occurrences: 0, output_scan_redactions: 0, model_redaction_markers: 0, question_redactions: 0 },
          });
        }
        return send(404, { detail: "Unexpected test request" });
      });
    }

    await page.goto(base, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.locator("#unlock").waitFor({ state: "visible" });
    assert.ok(await page.locator("#workspace").isHidden());
    assert.ok((await page.locator("#key-help").innerText()).includes("RAG_APP_KEY"));
    assert.equal(await page.locator("#connection-error").innerText(), "");
    assert.equal(await page.locator("#mode").innerText(), "ACCESS KEY REQUIRED");
    assert.ok(await page.locator("#connect").isDisabled());

    await page.locator("#app-key").fill("incorrect-fixture-key");
    await page.locator("#connect").click();
    await page.waitForFunction(() => document.querySelector("#connection-error").textContent.includes("not accepted"));
    assert.ok(await page.locator("#workspace").isHidden());

    await page.locator("#app-key").fill(key);
    await page.locator("#connect").click();
    await waitReady(page);
    assert.ok(await page.locator("#connection-panel").isHidden());
    assert.ok(await page.locator("#workspace").isVisible());
    assert.equal(await page.locator("#error").innerText(), "");
    if (live) {
      assert.match(await page.locator("#example-status").innerText(), /Azure AI Search/);
      const original = await page.locator("#example-original").innerText();
      const indexed = await page.locator("#example-indexed").innerText();
      for (const canary of ["Alice Martin", "alice@example.test", "123 Example Avenue", "4111111111111111"]) {
        assert.ok(original.includes(canary));
        assert.ok(!indexed.includes(canary));
      }
      assert.ok(await page.locator("#example-indexed mark.pii-token").count() > 0);
      console.log("PASS: live original / actual Search comparison");
    }
    await page.locator("#question").fill("What caused the checkout HTTP 504 failures and how were they fixed?");
    await page.locator("#send").click();
    await page.waitForFunction(() => (document.querySelectorAll(".message.assistant").length === 1 &&
      !document.querySelector("#send").disabled) || document.querySelector("#error").textContent,
    null, { timeout: 120000 });
    assert.equal(await page.locator("#error").innerText(), "");
    assert.match(await page.locator(".message.assistant").innerText(), /concurr|pool|connection|backoff/i);
    assert.ok(await page.locator("#evidence details").count() > 0);
    console.log("PASS: actual chat answer and evidence rendered");
    if (live) {
      assert.match(await page.locator("#privacy-status").innerText(), /No reversible PII placeholders/);
      assert.ok(await page.locator("#restore").isDisabled());
      await page.locator("#walkthrough").evaluate(element => { element.open = true; });
      console.log("Running the real PII-visible retrieval/generation example...");
      await page.locator("#ask-example").click();
      await page.waitForFunction(() => (document.querySelectorAll(".message.assistant").length === 2 &&
        !document.querySelector("#send").disabled) || document.querySelector("#error").textContent,
      null, { timeout: 300000 });
      assert.equal(await page.locator("#error").innerText(), "");
      const protectedAnswer = await page.locator(".message.assistant .message-body").last().innerText();
      assert.ok(protectedAnswer.includes("[PII_"));
      assert.ok(!await page.locator("#restore").isDisabled());
      const turns = chatRequests;
      await page.locator("#restore").check();
      await page.waitForFunction(() => document.querySelectorAll(".answer-view").item(
        document.querySelectorAll(".answer-view").length - 1).textContent.includes("Original PII shown"),
      null, { timeout: 60000 });
      const revealed = await page.locator(".message.assistant .message-body").last().innerText();
      assert.notEqual(revealed, protectedAnswer);
      assert.ok(/Alice Martin|alice@example.test|4111111111111111/.test(revealed));
      assert.equal(chatRequests, turns);
      assert.equal(await page.locator("#protected-answer").innerText(), protectedAnswer);
      await page.locator("#restore").uncheck();
      await page.waitForFunction(() => !document.querySelector("#restore").disabled);
      assert.equal(await page.locator(".message.assistant .message-body").last().innerText(), protectedAnswer);
      assert.equal(chatRequests, turns);
      console.log("PASS: same answer revealed and hidden with no new chat request");
    }

    if (!live) {
      failDelete = true;
      await page.locator("#collection").selectOption("policies");
      await page.waitForFunction(() => document.querySelector("#error").textContent.includes("cleanup unavailable"));
      assert.ok(await page.locator("#send").isDisabled());
      assert.ok(await page.locator("#question").isDisabled());
      assert.equal(await page.locator(".message").count(), 0);
      await page.locator("#reset").click();
      await waitReady(page);
      await page.locator("#question").fill("Explain the policy.");
      await page.locator("#send").click();
      await page.waitForFunction(() => document.querySelectorAll(".message.assistant").length === 1 &&
        !document.querySelector("#send").disabled);
      assert.equal(lastCollection, "policies");

      const failed = await browser.newPage();
      let first = true;
      await failed.route("**/api/config", route => {
        const status = first ? 503 : 401;
        first = false;
        return route.fulfill({ status, contentType: "text/html", body: "Service unavailable" });
      });
      await failed.goto(base);
      await failed.locator("#retry-connection").waitFor({ state: "visible" });
      assert.equal(await failed.locator("#connection-title").innerText(), "Unable to connect");
      assert.ok(await failed.locator("#workspace").isHidden());
      await failed.locator("#retry-connection").click();
      await failed.locator("#unlock").waitFor({ state: "visible" });
      await failed.close();
    }

    fs.mkdirSync(path.join(root, ".data", "ui-check"), { recursive: true });
    await page.screenshot({ path: path.join(root, ".data", "ui-check", live ? "live-connected.png" : "fixture-connected.png"), fullPage: true });
    await page.evaluate(async () => {
      if (sessionId) await api(`/api/sessions/${sessionId}`, undefined, "DELETE");
    });
    await page.reload();
    await page.locator("#unlock").waitFor({ state: "visible" });
    assert.ok(await page.locator("#workspace").isHidden());
    assert.equal(await page.locator("#app-key").inputValue(), "");
    await page.screenshot({ path: path.join(root, ".data", "ui-check", live ? "live-access-gate.png" : "fixture-access-gate.png"), fullPage: true });
    assert.deepEqual(errors, []);
    console.log(live
      ? "PASS: real Azure login, Search comparison, chat, immediate reveal/hide, citations, and refresh locking"
      : "PASS: fixture login/reset/retry; missing/wrong/valid keys; no stale session; refresh relocks");
  } catch (error) {
    if (page && !page.isClosed()) {
      console.error(await page.evaluate(() => ({
        uiError: document.querySelector("#error")?.textContent,
        privacyStatus: document.querySelector("#privacy-status")?.textContent,
        assistantMessages: document.querySelectorAll(".message.assistant").length,
        sendDisabled: document.querySelector("#send")?.disabled,
      })));
      console.error({ pageErrors: errors });
      fs.mkdirSync(path.join(root, ".data", "ui-check"), { recursive: true });
      await page.screenshot({ path: path.join(root, ".data", "ui-check", "failure.png"), fullPage: true });
    }
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
