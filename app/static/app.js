/* Minimal dashboard script.
 *
 * Three things matter here:
 *  1. Every id (video, comment, order) is treated as an opaque STRING. Nothing
 *     is passed through Number(), so ids above 2^53 survive exactly.
 *  2. All provider-supplied text is written with textContent, never innerHTML,
 *     including SSE-driven updates.
 *  3. Start is idempotent: one key is minted per press and reused for retries,
 *     so a double click or a browser retry cannot create a second batch.
 */

(function () {
  "use strict";

  const body = document.body;
  const CSRF = body.dataset.csrf;
  const MAX_LINKS = parseInt(body.dataset.maxLinks, 10) || 10;
  const LIVE_ALLOWED = body.dataset.liveAllowed === "yes";

  const el = {
    links: document.getElementById("links"),
    start: document.getElementById("start"),
    stop: document.getElementById("stop"),
    checkService: document.getElementById("check-service"),
    formMessage: document.getElementById("form-message"),
    serviceMessage: document.getElementById("service-message"),
    connection: document.getElementById("connection"),
    batches: document.getElementById("batches")
  };

  const batchTemplate = document.getElementById("batch-template");
  const itemTemplate = document.getElementById("item-template");

  let pendingKey = null;
  let latestBatchId = null;
  let lastEventId = 0;
  let source = null;
  let currentItemId = null;

  const OUTCOME_TONE = {
    submitted: "good",
    dry_run_complete: "dry",
    delivered: "good",
    keyword_not_found: "warn",
    scan_incomplete: "warn",
    skipped_threshold: "warn",
    already_ordered: "warn",
    active_order_exists: "warn",
    target_ambiguous: "warn",
    target_unverified: "warn",
    quantity_out_of_range: "warn",
    service_configuration_required: "bad",
    reader_unconfigured: "bad",
    url_invalid: "bad",
    provider_error: "bad",
    submission_unknown: "bad",
    failed: "bad",
    cancelled: "warn"
  };

  const OUTCOME_LABEL = {
    pending: "Queued",
    processing: "Working",
    keyword_not_found: "No keyword match",
    scan_incomplete: "Scan incomplete",
    skipped_threshold: "Skipped, 10k+ leader",
    already_ordered: "Already ordered",
    active_order_exists: "Order in flight",
    target_ambiguous: "Ambiguous target",
    target_unverified: "Target unverified",
    quantity_out_of_range: "Quantity out of range",
    service_configuration_required: "Service needs setup",
    reader_unconfigured: "Reader not configured",
    url_invalid: "Bad link",
    provider_error: "Provider error",
    dry_run_complete: "Dry run done",
    submitted: "Submitted",
    submission_unknown: "Submission unknown",
    failed: "Failed",
    cancelled: "Cancelled"
  };

  const DELIVERY_LABEL = {
    not_applicable: "",
    unknown: "Delivery unknown",
    pending: "Delivery pending",
    in_progress: "Delivering",
    processing: "Delivering",
    partial: "Partial",
    completed: "Delivery completed",
    canceled: "Delivery cancelled",
    error: "Delivery error"
  };

  function text(node, selector, value) {
    const target = node.querySelector(selector);
    if (target) target.textContent = value === null || value === undefined || value === "" ? "\u2014" : String(value);
    return target;
  }

  function setMessage(node, message, kind) {
    node.textContent = message || "";
    node.classList.remove("error", "ok");
    if (kind) node.classList.add(kind);
  }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "k" + Date.now() + Math.random().toString(36).slice(2);
  }

  function selectedMode() {
    const checked = document.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "dry_run";
  }

  document.querySelectorAll('input[name="mode"]').forEach(function (input) {
    input.addEventListener("change", function () {
      el.start.classList.toggle("live", selectedMode() === "live");
      el.start.textContent = selectedMode() === "live" ? "Start live run" : "Start";
    });
  });

  // ------------------------------------------------------------------ start
  el.start.addEventListener("click", async function () {
    const raw = el.links.value || "";
    const lines = raw.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
    if (lines.length === 0) {
      setMessage(el.formMessage, "Paste at least one link.", "error");
      return;
    }
    if (lines.length > MAX_LINKS) {
      setMessage(el.formMessage, "Maximum " + MAX_LINKS + " links; you pasted " + lines.length + ".", "error");
      return;
    }
    const mode = selectedMode();
    if (mode === "live" && !LIVE_ALLOWED) {
      setMessage(el.formMessage, "Live mode is disabled until the panel key and the comment reader are configured.", "error");
      return;
    }

    if (!pendingKey) pendingKey = newKey();
    el.start.disabled = true;
    setMessage(el.formMessage, "Queueing\u2026");

    try {
      const response = await fetch("/api/batches", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          links: lines.join("\n"),
          mode: mode,
          idempotency_key: pendingKey,
          csrf_token: CSRF
        })
      });
      const data = await response.json();
      if (!response.ok) {
        setMessage(el.formMessage, data.detail || "Could not queue the batch.", "error");
        return;
      }
      latestBatchId = data.batch_id;
      el.stop.disabled = false;
      setMessage(
        el.formMessage,
        data.created
          ? "Queued " + data.links + " link(s) in " + (mode === "live" ? "Live" : "Dry run") + " mode."
          : "That batch was already queued; showing the existing run.",
        "ok"
      );
      pendingKey = null;
    } catch (error) {
      // Keep pendingKey so pressing Start again retries the SAME batch.
      setMessage(el.formMessage, "Network error while queueing. Press Start again to retry the same batch.", "error");
    } finally {
      el.start.disabled = false;
    }
  });

  // ------------------------------------------------------------------- stop
  async function stopBatch(batchId) {
    if (!batchId) return;
    const response = await fetch("/api/batches/" + encodeURIComponent(batchId) + "/stop", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ csrf_token: CSRF })
    });
    const data = await response.json();
    setMessage(
      el.formMessage,
      response.ok ? data.note : data.detail || "Could not stop the batch.",
      response.ok ? "ok" : "error"
    );
  }

  el.stop.addEventListener("click", function () { stopBatch(latestBatchId); });

  el.checkService.addEventListener("click", async function () {
    setMessage(el.serviceMessage, "Reading services and balance\u2026");
    try {
      const response = await fetch("/api/service");
      const data = await response.json();
      if (!data.configured) {
        setMessage(el.serviceMessage, data.error, "error");
        return;
      }
      const parts = [];
      if (data.service) {
        parts.push(
          "Service " + data.service.service_id + ": " + data.service.name +
          " (type " + data.service.type + ", min " + data.service.min +
          ", max " + data.service.max + ", rate " + data.service.rate + ")"
        );
        parts.push(data.compatible ? "Compatible with this order shape." : "NOT compatible with this order shape.");
      } else if (data.error) {
        parts.push(data.error);
      }
      if (data.balance !== null) parts.push("Balance " + data.balance + " " + (data.currency || ""));
      if (data.balance_error) parts.push("Balance read failed: " + data.balance_error);
      setMessage(el.serviceMessage, parts.join(" \u00b7 "), data.compatible ? "ok" : "error");
    } catch (error) {
      setMessage(el.serviceMessage, "Could not reach the panel from the server.", "error");
    }
  });

  // ---------------------------------------------------------------- render
  function renderItem(node, item) {
    node.dataset.itemId = item.id;
    node.classList.toggle("active", item.id === currentItemId);

    text(node, ".pos", item.position);
    const outcome = node.querySelector(".outcome");
    outcome.textContent = OUTCOME_LABEL[item.outcome] || item.outcome;
    outcome.dataset.tone = OUTCOME_TONE[item.outcome] || "";

    const delivery = node.querySelector(".delivery");
    const deliveryLabel = DELIVERY_LABEL[item.delivery_state] || "";
    delivery.textContent = deliveryLabel;
    delivery.hidden = !deliveryLabel;
    delivery.dataset.tone = item.delivery_state === "completed" ? "good" : "";

    const timings = item.timings || {};
    text(node, ".elapsed", timings.total_ms ? timings.total_ms + " ms" : "");

    text(node, ".item-link", item.input_url);
    text(node, ".owner", item.owner_username ? "@" + item.owner_username : null);
    text(node, ".top", item.top_likes);
    text(node, ".tlikes", item.target_likes);
    text(node, ".qty", item.quantity);

    text(node, ".vid", item.video_id);
    text(node, ".submitted", item.submitted_link || item.canonical_url);
    text(node, ".cid", item.target_comment_id);
    text(node, ".ctext", item.target_text);
    const scan = item.scan || {};
    text(
      node,
      ".scan",
      item.scan_status
        ? item.scan_status +
          (scan.actual_scope ? " (" + scan.actual_scope + ")" : "") +
          (scan.incomplete_reasons && scan.incomplete_reasons.length
            ? " \u2014 " + scan.incomplete_reasons.join(", ")
            : "")
        : null
    );
    // Scope limitations are shown next to the result, not hidden in the logs.
    const limitations = (scan.scope_limitations || []);
    const limitNode = node.querySelector(".scope-limits");
    if (limitNode) {
      limitNode.textContent = limitations.length
        ? "provider-visible data only \u2014 " + limitations.join("; ")
        : "";
      limitNode.hidden = limitations.length === 0;
    }
    text(node, ".counts", item.pages_read + " / " + item.comments_read);
    text(node, ".cost", item.estimated_cost);
    text(node, ".oid", item.order_id);
    text(
      node,
      ".timings",
      "resolve " + (timings.url_resolve_ms || 0) +
      " \u00b7 read " + (timings.comment_read_ms || 0) +
      " \u00b7 submit " + (timings.submission_ms || 0) + " ms"
    );
    text(node, ".note", item.error);
  }

  function renderBatch(batch) {
    const fragment = batchTemplate.content.cloneNode(true);
    const article = fragment.querySelector(".batch");
    article.dataset.batchId = batch.id;

    const created = batch.created_at ? new Date(batch.created_at) : null;
    article.querySelector("h3").textContent = created ? created.toLocaleString() : batch.id;

    const modeBadge = article.querySelector(".mode-badge");
    modeBadge.textContent = batch.mode === "live" ? "Live" : "Dry run";
    modeBadge.dataset.tone = batch.mode === "live" ? "live" : "dry";

    const stateBadge = article.querySelector(".state-badge");
    stateBadge.textContent = batch.stop_requested_at ? "stopped" : batch.state;

    const stopButton = article.querySelector(".stop-batch");
    stopButton.disabled = batch.state === "finished" || Boolean(batch.stop_requested_at);
    stopButton.addEventListener("click", function () { stopBatch(batch.id); });

    const items = article.querySelector(".items");
    batch.items.forEach(function (item) {
      const itemNode = itemTemplate.content.cloneNode(true);
      const itemEl = itemNode.querySelector(".item");
      renderItem(itemEl, item);
      const toggle = itemEl.querySelector(".toggle");
      const detail = itemEl.querySelector(".item-detail");
      toggle.addEventListener("click", function () {
        const open = detail.hidden;
        detail.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
      });
      items.appendChild(itemNode);
    });

    return fragment;
  }

  function renderAll(batches) {
    el.batches.textContent = "";
    if (!batches.length) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No runs yet. Paste a link above and start a dry run.";
      el.batches.appendChild(p);
      return;
    }
    batches.forEach(function (batch) { el.batches.appendChild(renderBatch(batch)); });
    if (batches[0]) {
      latestBatchId = latestBatchId || batches[0].id;
      el.stop.disabled = batches[0].state === "finished";
    }
  }

  function patchItem(item) {
    const node = el.batches.querySelector('.item[data-item-id="' + item.id + '"]');
    if (!node) {
      refresh();
      return;
    }
    renderItem(node, item);
  }

  async function refresh() {
    try {
      const response = await fetch("/api/state");
      if (response.status === 401) {
        window.location.href = "/login";
        return;
      }
      const data = await response.json();
      lastEventId = data.last_event_id;
      currentItemId = data.current_item_id;
      renderAll(data.batches);
    } catch (error) {
      setMessage(el.connection, "Could not load runs.", "error");
    }
  }

  // ------------------------------------------------------- progress stream
  function connect() {
    if (source) source.close();
    source = new EventSource("/api/events?last_event_id=" + encodeURIComponent(String(lastEventId)));

    source.addEventListener("open", function () {
      setMessage(el.connection, "Live progress connected.", "ok");
    });

    source.addEventListener("snapshot", function (event) {
      // Reconnect snapshot: the full current state, so a refresh or a dropped
      // connection never leaves a stale table behind.
      const data = JSON.parse(event.data);
      lastEventId = data.last_event_id;
      renderAll(data.batches);
    });

    source.addEventListener("item", function (event) {
      const data = JSON.parse(event.data);
      lastEventId = Number(event.lastEventId) || lastEventId;
      if (data.payload && data.payload.item) {
        currentItemId = null;
        patchItem(data.payload.item);
      }
    });

    ["batch", "stop", "recovery", "resolved", "delivery"].forEach(function (kind) {
      source.addEventListener(kind, function (event) {
        lastEventId = Number(event.lastEventId) || lastEventId;
        refresh();
      });
    });

    source.addEventListener("error", function () {
      setMessage(el.connection, "Progress stream dropped. Reconnecting\u2026");
      // EventSource retries on its own; the snapshot on reconnect repairs state.
    });
  }

  refresh().then(connect);
})();
