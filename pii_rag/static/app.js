"use strict";
const $ = (id) => document.getElementById(id);
let config, sessionId, appKey = "", busy = false, latestAnswer;

function protectedText(element, text) {
  element.replaceChildren();
  const pattern = /\[PII_([A-Z_]+)_[0-9a-f]{24}\]|\[REDACTED(?:_[A-Z_]+)?\]/g;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    element.append(document.createTextNode(text.slice(start, match.index)));
    const mark = document.createElement("mark");
    mark.className = "pii-token";
    mark.textContent = match[0];
    mark.title = match[1] ? `${match[1]} value hidden behind an opaque token` : "Safety redaction; no reversible value is available here";
    element.append(mark);
    start = match.index + match[0].length;
  }
  element.append(document.createTextNode(text.slice(start)));
}

class APIError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, body, method = "POST") {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-App-Key": appKey },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    const error = response.headers.get("content-type")?.includes("application/json")
      ? await response.json() : null;
    throw new APIError(response.status, error?.detail || `Service request failed (HTTP ${response.status}).`);
  }
  return response.status === 204 ? null : response.json();
}

function clearConversation() {
  sessionId = null;
  latestAnswer = null;
  $("messages").replaceChildren();
  $("question").value = "";
  $("original-question").textContent = "No turn yet.";
  $("question-expectation").textContent = "";
  $("protected-question").textContent = "No turn yet.";
  $("protected-answer").textContent = "No turn yet.";
  $("evidence").replaceChildren();
  $("privacy-status").textContent = "Ask a question first. A technical summary may contain no PII to restore.";
  $("privacy-counts").textContent = "No answer yet.";
  $("restore").checked = false;
}

function showConnection(message, needsKey, error = "") {
  $("workspace").hidden = true;
  $("connection-panel").hidden = false;
  $("connection-title").textContent = needsKey ? "Connect to the Azure demo" : "Unable to connect";
  $("connection-message").textContent = message;
  $("connection-error").textContent = error;
  $("unlock").hidden = !needsKey;
  $("retry-connection").hidden = needsKey;
  $("mode").textContent = needsKey ? "ACCESS KEY REQUIRED" : "CONNECTION FAILED";
  if (needsKey) $("app-key").focus();
}

function showError(error) {
  if (error.status === 401) {
    appKey = "";
    config = null;
    clearConversation();
    showConnection("The service is reachable, but your access key was not accepted.", true,
      "Use the RAG_APP_KEY value for this deployment, then connect again.");
  } else {
    $("error").textContent = error.message;
  }
}

function setBusy(value) {
  busy = value;
  const unlocked = Boolean(config) && !$("workspace").hidden;
  for (const id of ["collection", "reset", "refresh"]) $(id).disabled = value || !unlocked;
  for (const id of ["send", "question"]) $(id).disabled = value || !unlocked || !sessionId;
  for (const id of ["file", "upload"]) $(id).disabled = value || !unlocked || config.mode !== "azure";
  for (const id of ["example", "load-example", "ask-example"]) $(id).disabled = value || !unlocked;
  $("restore").disabled = value || !latestAnswer || latestAnswer.privacy.answer_placeholders === 0;
  $("send").textContent = value ? "Retrieving evidence..." : "Ask collection";
}

async function reset() {
  if (busy) return;
  setBusy(true);
  $("error").textContent = "";
  const previousSession = sessionId;
  clearConversation();
  try {
    if (previousSession) await api(`/api/sessions/${previousSession}`, undefined, "DELETE");
    const session = await api("/api/sessions", { collection: $("collection").value });
    sessionId = session.session_id;
    $("privacy-questions").replaceChildren();
    for (const example of config.privacy_questions?.[$("collection").value] || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = example.label;
      button.title = example.question;
      button.onclick = () => {
        if (busy) return;
        $("question").value = example.question;
        $("question-expectation").textContent = example.expect;
        $("question").focus();
      };
      $("privacy-questions").append(button);
    }
    $("suggestions").replaceChildren();
    for (const text of config.suggestions[$("collection").value]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = text;
      button.onclick = () => {
        if (busy) return;
        $("question").value = text;
        $("question-expectation").textContent = "Technical-only question: masked sources may produce an answer without any PII placeholders.";
        $("question").focus();
      };
      $("suggestions").append(button);
    }
    $("example").replaceChildren();
    for (const id of config.privacy_examples?.[$("collection").value] || []) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      $("example").append(option);
    }
    if ($("collection").value === "support") $("example").value = "INC-104";
    await refresh();
    if ($("example").value) await loadExample();
  } catch (error) { showError(error); }
  finally {
    setBusy(false);
  }
}

function message(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role === "You" ? "user" : "assistant"}`;
  const title = document.createElement("strong");
  title.textContent = role;
  const view = document.createElement("div");
  view.className = "answer-view";
  const body = document.createElement("div");
  body.className = "message-body";
  protectedText(body, text);
  node.append(title, view, body);
  $("messages").append(node);
  node.scrollIntoView({ block: "nearest" });
  return { body, view };
}

function updatePrivacy(result) {
  const p = result.privacy;
  latestAnswer.privacy = p;
  protectedText(latestAnswer.body, result.answer);
  latestAnswer.view.textContent = p.restored_occurrences
    ? `Original PII shown: ${p.restored_occurrences} value occurrence(s). Model history remains protected.`
    : "Protected answer";
  $("restore").checked = result.restored && p.answer_placeholders > 0;
  $("privacy-counts").textContent = `${p.evidence_placeholders} hidden placeholder occurrence(s) in retrieved evidence; ` +
    `${p.answer_placeholders} in this answer; ${p.question_redactions} redaction(s) in your question.`;
  let status = p.answer_placeholders === 0
    ? "No reversible PII placeholders in this answer, so there is nothing to restore. Technical summaries normally omit personal details. Use the PII-visible example above to demonstrate the toggle."
    : result.restored
      ? `${p.restored_occurrences} PII occurrence(s) restored for display only. Uncheck to hide them again.`
      : `${p.answer_placeholders} PII placeholder occurrence(s) are hidden in this answer. Check the box to reveal evidence-backed originals.`;
  if (p.output_scan_redactions) status += ` Final privacy scan removed ${p.output_scan_redactions} un-tokenized span(s); these removals cannot be reversed.`;
  if (p.model_redaction_markers) status += ` The model also returned ${p.model_redaction_markers} non-reversible redaction marker(s).`;
  if (p.blocked_occurrences) status += ` ${p.blocked_occurrences} unsupported or stale token occurrence(s) remain redacted.`;
  $("privacy-status").textContent = status;
}

$("restore").onchange = async () => {
  if (busy || !latestAnswer) return;
  const restore = $("restore").checked;
  setBusy(true);
  $("error").textContent = "";
  try {
    const result = await api("/api/answer/display", {
      session_id: sessionId, answer_id: latestAnswer.id, restore,
    });
    updatePrivacy(result);
  } catch (error) {
    $("restore").checked = !restore;
    showError(error);
  } finally { setBusy(false); }
};

$("chat").onsubmit = async (event) => {
  event.preventDefault();
  if (busy || !sessionId) return;
  const question = $("question").value.trim();
  if (!question) return;
  setBusy(true);
  $("error").textContent = "";
  message("You", question);
  try {
    const result = await api("/api/chat", {
      session_id: sessionId, question, restore: $("restore").checked,
    });
    const displayed = message(config.mode === "demo" ? "Local extractive demo" : "Foundry prompt agent", result.answer);
    latestAnswer = { id: result.answer_id, ...displayed, privacy: result.privacy };
    updatePrivacy(result);
    $("original-question").textContent = question;
    protectedText($("protected-question"), result.sanitized_question);
    protectedText($("protected-answer"), result.sanitized_answer);
    $("evidence").replaceChildren();
    for (const doc of result.evidence) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `[source:${doc.id}] Sanitized excerpt`;
      const content = document.createElement("pre");
      protectedText(content, doc.content);
      details.append(summary, content);
      details.open = $("evidence").childElementCount === 0;
      $("evidence").append(details);
    }
    if (result.warning) $("error").textContent = result.warning;
    $("question").value = "";
  } catch (error) { showError(error); }
  finally { setBusy(false); $("question").focus(); }
};

async function refresh() {
  const status = await api(`/api/collections/${$("collection").value}/status`, undefined, "GET");
  $("ingestion-status").textContent = Object.entries(status).map(([k, v]) => `${k}: ${v}`).join(" | ");
}
async function loadExample() {
  $("example-status").textContent = "Reading protected sample from the index...";
  try {
    const result = await api(`/api/examples/${$("collection").value}/${$("example").value}`, undefined, "GET");
    $("example-original").textContent = result.original;
    $("example-indexed").replaceChildren();
    for (const text of result.indexed_chunks) {
      const pre = document.createElement("pre");
      protectedText(pre, text);
      $("example-indexed").append(pre);
    }
    const counts = Object.entries(result.pii_counts).map(([type, count]) => `${type}: ${count}`).join(" | ");
    $("example-status").textContent = `${result.source} · ${result.sample_id} · ${counts || "No detected PII tokens"}`;
  } catch (error) {
    $("example-original").textContent = "Sample unavailable.";
    $("example-indexed").replaceChildren();
    $("example-status").textContent = error.message;
    if (error.status === 401) showError(error);
  }
}
$("load-example").onclick = async () => {
  if (busy) return;
  setBusy(true);
  try { await loadExample(); } finally { setBusy(false); }
};
$("ask-example").onclick = () => {
  if (busy || !sessionId) return;
  $("question").value = $("collection").value === "support"
    ? "Quote the full checkout HTTP 429 incident evidence verbatim. Preserve every opaque placeholder exactly. Do not infer original values."
    : "Quote the incident documentation policy evidence verbatim. Preserve every opaque placeholder exactly. Do not infer original values.";
  $("question").focus();
  $("walkthrough").open = false;
  $("chat").requestSubmit();
};
$("refresh").onclick = () => refresh().catch(showError);
$("collection").onchange = reset;
$("reset").onclick = reset;
$("upload").onclick = async () => {
  if (busy) return;
  const file = $("file").files[0];
  if (!file) return showError(new Error("Select an English text file first."));
  if (file.size > 16000) return showError(new Error("File too large for this lean text sample."));
  setBusy(true);
  try {
    const text = await file.text();
    const result = await api(`/api/collections/${$("collection").value}/documents`, { text });
    $("ingestion-status").textContent = `${result.status}: ${result.document_id}. Check status before querying.`;
  } catch (error) { showError(error); }
  finally { setBusy(false); }
};
$("theme").onclick = () => {
  document.documentElement.dataset.theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
};
$("unlock").onsubmit = async (event) => {
  event.preventDefault();
  if ($("connect").disabled) return;
  appKey = $("app-key").value;
  $("app-key").value = "";
  await start(true);
};
$("app-key").oninput = () => { $("connect").disabled = !$("app-key").value.trim(); };
$("retry-connection").onclick = () => start();

async function start(submittedKey = false) {
  $("connect").disabled = true;
  $("retry-connection").disabled = true;
  $("connection-error").textContent = "";
  $("connection-message").textContent = "Connecting...";
  try {
    config = await api("/api/config", undefined, "GET");
    $("connection-panel").hidden = true;
    $("workspace").hidden = false;
    $("mode").textContent = config.mode === "demo" ? "SYNTHETIC DEMO / NO LLM" : "LIVE AZURE";
    $("notice").textContent = config.notice;
    $("provider").textContent = `Detector: ${config.provider}. Provider changes require reindexing.`;
    $("collection").replaceChildren();
    for (const collection of config.collections) {
      const option = document.createElement("option");
      option.value = collection.id;
      option.textContent = collection.label;
      $("collection").append(option);
    }
    await reset();
  } catch (error) {
    config = null;
    clearConversation();
    if (error.status === 401) {
      appKey = "";
      showConnection("The service is running. Enter the demo access key to unlock collections and chat.",
        true, submittedKey ? "That key was not accepted. Use RAG_APP_KEY, without quotes or extra spaces." : "");
    } else {
      showConnection("The service could not be reached or is still starting. Retry in a moment.",
        false, error.message);
    }
    setBusy(false);
  } finally {
    $("retry-connection").disabled = false;
    $("connect").disabled = !$("app-key").value.trim();
  }
}
start();
