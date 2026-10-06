(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const callbackUrl = `${window.location.origin}/v1/oauth/gmail/demo-callback`;
  let currentStatus = { configured: false, status: "disconnected" };

  const elements = {
    notice: $("#notice"),
    statusPill: $("#statusPill"),
    callbackUrl: $("#callbackUrl"),
    copyCallback: $("#copyCallback"),
    oauthForm: $("#oauthForm"),
    clientId: $("#clientId"),
    clientSecret: $("#clientSecret"),
    authorizeButton: $("#authorizeButton"),
    testForm: $("#testForm"),
    testButton: $("#testButton"),
    gmailQuery: $("#gmailQuery"),
    maxResults: $("#maxResults"),
    emptyResults: $("#emptyResults"),
    results: $("#results"),
    unansweredCount: $("#unansweredCount"),
    matchedCount: $("#matchedCount"),
    messageList: $("#messageList"),
    disconnectButton: $("#disconnectButton"),
  };

  elements.callbackUrl.textContent = callbackUrl;

  function setNotice(message, type = "success") {
    elements.notice.textContent = message;
    elements.notice.className = type === "error" ? "notice error" : "notice";
    elements.notice.hidden = false;
  }

  function clearNotice() {
    elements.notice.hidden = true;
    elements.notice.textContent = "";
  }

  function setBusy(element, busy, busyText) {
    element.disabled = busy;
    element.classList.toggle("busy", busy);
    const label = element.querySelector("span");
    if (!label) return;
    if (busy) {
      label.dataset.original = label.textContent;
      label.textContent = busyText;
    } else if (label.dataset.original) {
      label.textContent = label.dataset.original;
      delete label.dataset.original;
    }
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const detail = payload?.detail;
      const message = typeof detail === "string"
        ? detail
        : detail?.message || detail?.code || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return payload;
  }

  function renderStatus(status) {
    currentStatus = status;
    if (!elements.clientId.value && status.client_id) {
      elements.clientId.value = status.client_id;
    }
    const connected = status.status === "connected";
    elements.statusPill.className = `status-pill ${connected ? "connected" : "disconnected"}`;
    elements.statusPill.querySelector("span").textContent = connected
      ? "Connected with OAuth"
      : status.configured
        ? "Configured · authorization needed"
        : "Not configured";
    elements.testButton.disabled = !connected;
    elements.disconnectButton.disabled = !connected;
    const authorizeLabel = elements.authorizeButton.querySelector("span");
    authorizeLabel.textContent = status.configured
      ? connected ? "Authorize Gmail again" : "Authorize configured Gmail"
      : "Save and authorize Gmail";
    if (connected) {
      elements.emptyResults.querySelector("strong").textContent = "Connected and ready to test";
      elements.emptyResults.querySelector("p").textContent = "Press “Test Gmail” to read matching thread metadata.";
    }
  }

  async function refreshStatus() {
    try {
      const status = await request("/v1/oauth/gmail/status");
      renderStatus(status);
    } catch (error) {
      elements.statusPill.className = "status-pill error";
      elements.statusPill.querySelector("span").textContent = "Status unavailable";
      setNotice(error.message, "error");
    }
  }

  function renderMessages(payload) {
    elements.unansweredCount.textContent = String(payload.unanswered ?? 0);
    elements.matchedCount.textContent = String(payload.matched ?? 0);
    elements.messageList.replaceChildren();

    const items = Array.isArray(payload.items) ? payload.items : [];
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "message-item";
      const text = document.createElement("p");
      text.className = "snippet";
      text.textContent = "No unanswered threads matched this query.";
      empty.append(text);
      elements.messageList.append(empty);
    }

    for (const item of items) {
      const article = document.createElement("article");
      article.className = "message-item";
      const top = document.createElement("div");
      top.className = "message-top";
      const subject = document.createElement("h3");
      subject.textContent = item.subject || "(no subject)";
      const priority = document.createElement("span");
      priority.className = "priority";
      priority.textContent = item.priority || "normal";
      top.append(subject, priority);
      const sender = document.createElement("p");
      sender.className = "sender";
      sender.textContent = item.sender || "Unknown sender";
      const snippet = document.createElement("p");
      snippet.className = "snippet";
      snippet.textContent = item.snippet || "No preview available.";
      article.append(top, sender, snippet);
      elements.messageList.append(article);
    }

    elements.emptyResults.hidden = true;
    elements.results.hidden = false;
  }

  elements.copyCallback.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(callbackUrl);
      elements.copyCallback.textContent = "Copied";
      window.setTimeout(() => { elements.copyCallback.textContent = "Copy"; }, 1500);
    } catch {
      setNotice("Copy failed. Select the redirect URL manually.", "error");
    }
  });

  elements.oauthForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearNotice();
    setBusy(elements.authorizeButton, true, "Preparing Google…");
    try {
      const clientId = elements.clientId.value.trim();
      const clientSecret = elements.clientSecret.value;
      if (clientId) {
        const configuration = {
          authorization_endpoint: "https://accounts.google.com/o/oauth2/v2/auth",
          token_endpoint: "https://oauth2.googleapis.com/token",
          client_id: clientId,
          client_auth_method: "client_secret_post",
          redirect_uri: callbackUrl,
          scopes: ["https://www.googleapis.com/auth/gmail.readonly"],
        };
        if (clientSecret) configuration.client_secret = clientSecret;
        await request("/v1/oauth/gmail/configure", {
          method: "POST",
          body: JSON.stringify(configuration),
        });
        elements.clientSecret.value = "";
      } else if (!currentStatus.configured) {
        throw new Error("Enter the Google client ID and client secret first.");
      }
      const started = await request("/v1/oauth/gmail/start", { method: "POST" });
      window.location.assign(started.authorization_url);
    } catch (error) {
      setNotice(error.message, "error");
      setBusy(elements.authorizeButton, false, "");
      await refreshStatus();
    }
  });

  elements.testForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearNotice();
    setBusy(elements.testButton, true, "Reading Gmail…");
    try {
      const payload = await request("/v1/connectors/gmail/test", {
        method: "POST",
        body: JSON.stringify({
          query: elements.gmailQuery.value.trim(),
          max_results: Number(elements.maxResults.value),
        }),
      });
      renderMessages(payload);
      setNotice(`Live Gmail test completed. Diagnostic ${payload.diagnostic_id.slice(0, 8)} was audited.`);
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      setBusy(elements.testButton, false, "");
      elements.testButton.disabled = currentStatus.status !== "connected";
    }
  });

  elements.disconnectButton.addEventListener("click", async () => {
    clearNotice();
    elements.disconnectButton.disabled = true;
    try {
      await request("/v1/connectors/gmail/disconnect", { method: "POST" });
      elements.results.hidden = true;
      elements.emptyResults.hidden = false;
      setNotice("Gmail disconnected and its locally stored OAuth token was deleted.");
      await refreshStatus();
    } catch (error) {
      setNotice(error.message, "error");
      await refreshStatus();
    }
  });

  const parameters = new URLSearchParams(window.location.search);
  if (parameters.get("connected") === "1") {
    setNotice("Google authorization completed. You can run the live Gmail test now.");
    window.history.replaceState({}, "", "/gmail-demo");
  } else if (parameters.has("error")) {
    setNotice(`Google authorization failed: ${parameters.get("error")}.`, "error");
    window.history.replaceState({}, "", "/gmail-demo");
  }

  refreshStatus();
})();
