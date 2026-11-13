const $ = (id) => document.getElementById(id);
let key = "",
  snapshot = null,
  received = 0,
  busy = false,
  readId = 0,
  selectedReceipt = null;
const clients = ["A", "B"].map((holder) => ({
  holder,
  token: null,
  last: null,
  captured: null,
}));
async function api(path, body) {
  const response = await fetch(
      path,
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    ),
    result = await response.json();
  if (!response.ok && !result.receipt)
    throw Error(result.error || "Request failed.");
  return { ...result, httpStatus: response.status };
}
function clientUI(c) {
  const section = document.createElement("section");
  section.className = "client";
  section.innerHTML = `<div class="client-head"><h2>Client ${c.holder}</h2><span class="token" id="token-${c.holder}">NO TOKEN</span></div><div class="lease-actions"><label>Lease<select id="ttl-${c.holder}"><option value="1000">1 second</option><option value="5000" selected>5 seconds</option><option value="15000">15 seconds</option><option value="60000">60 seconds</option></select></label><button data-action="acquire">Acquire</button><button data-action="renew">Renew</button><button data-action="release">Release</button></div><textarea id="value-${c.holder}" aria-label="Client ${c.holder} document value" maxlength="2048">A note from Client ${c.holder}.</textarea><div class="version-controls"><label>Expected version<input id="expected-${c.holder}" type="number" min="0" max="1000000000" value="0" aria-label="Client ${c.holder} expected version"></label><button data-action="sync">Use current version</button></div><div class="write-actions"><button data-action="write">Write now</button><button data-action="capture">Capture delayed write</button><button data-action="deliver">Deliver captured</button></div><div class="capture" id="captured-${c.holder}">No delayed write captured.</div><p class="last-result" id="result-${c.holder}"></p><footer><button data-action="retry">Retry last request</button></footer>`;
  for (const b of section.querySelectorAll("[data-action]"))
    b.onclick = () => act(c, b.dataset.action);
  c.section = section;
  return section;
}
$("clients").replaceChildren(...clients.map(clientUI));
function controls() {
  for (const c of clients) {
    $("token-" + c.holder).textContent =
      c.token === null ? "NO TOKEN" : "TOKEN " + c.token;
    for (const b of c.section.querySelectorAll("button")) {
      const a = b.dataset.action;
      b.disabled =
        busy ||
        !snapshot ||
        (["renew", "release", "write", "capture"].includes(a) &&
          c.token === null) ||
        (a === "deliver" && !c.captured) ||
        (a === "retry" && !c.last);
    }
  }
  $("resources").disabled = busy;
  $("refresh").disabled = busy || !key;
  $("export").disabled = busy || !snapshot;
  for (const b of $("create-form").querySelectorAll("button"))
    b.disabled = busy;
}
function countdown() {
  if (!snapshot) return;
  const remaining = Math.max(
    0,
    snapshot.remaining_ms - (performance.now() - received),
  );
  $("remaining").textContent = (remaining / 1000).toFixed(1) + "s";
  $("lease-state").textContent =
    snapshot.state.holder && remaining > 0 ? "LEASE ACTIVE" : "AVAILABLE";
}
setInterval(countdown, 200);
function render() {
  const s = snapshot.state;
  $("fence").textContent = s.fence;
  $("holder").textContent = s.holder || "—";
  $("version").textContent = s.version;
  $("document").textContent = s.value || "(empty document)";
  $("audit").textContent = "REPLAY + HASH CHAIN VERIFIED";
  countdown();
  $("history").replaceChildren(
    ...snapshot.entries
      .slice(-12)
      .reverse()
      .map((entry) => {
        const r = entry.receipt,
          tr = document.createElement("tr");
        tr.className = r.allowed ? "accepted-row" : "denied-row";
        for (const value of [
          r.sequence,
          r.request.holder,
          r.request.action,
          r.reason,
          r.after.fence,
          r.after.version,
        ]) {
          const td = document.createElement("td");
          td.textContent = value;
          tr.append(td);
        }
        const td = document.createElement("td"),
          b = document.createElement("button");
        b.textContent = "View";
        b.setAttribute("aria-label", "View receipt " + r.sequence);
        b.onclick = () => {
          selectedReceipt = entry;
          $("receipt").textContent = JSON.stringify(entry, null, 2);
        };
        td.append(b);
        tr.append(td);
        return tr;
      }),
  );
  $("history-note").textContent =
    `Showing the latest ${Math.min(12, snapshot.entries.length)} of ${snapshot.entries.length} receipts. Exports include all receipts.`;
  selectedReceipt = selectedReceipt || snapshot.entries.at(-1);
  $("receipt").textContent = selectedReceipt
    ? JSON.stringify(selectedReceipt, null, 2)
    : "No commands recorded.";
  controls();
}
async function refresh() {
  if (!key) return;
  const ticket = ++readId,
    resource = key;
  const data = await api("/api/resources/" + resource);
  if (ticket !== readId || resource !== key) return;
  snapshot = data;
  received = performance.now();
  render();
}
async function listing(select) {
  const data = await api("/api/resources");
  $("resources").replaceChildren(
    ...data.resources.map((id) => new Option(id, id)),
  );
  key = select || data.resources[0] || "";
  $("resources").value = key;
  await change();
}
async function change() {
  readId++;
  snapshot = null;
  selectedReceipt = null;
  for (const c of clients) {
    c.token = null;
    c.last = null;
    c.captured = null;
    $("captured-" + c.holder).textContent = "No delayed write captured.";
    $("result-" + c.holder).textContent = "";
    $("expected-" + c.holder).value = 0;
  }
  controls();
  await refresh();
}
async function guarded(task) {
  if (busy) return;
  busy = true;
  controls();
  $("error").textContent = "";
  try {
    await task();
  } catch (e) {
    $("error").textContent = e.message;
  } finally {
    busy = false;
    controls();
  }
}
function request(c, action) {
  const r = { action, holder: c.holder, request_key: crypto.randomUUID() };
  if (["acquire", "renew"].includes(action))
    r.ttl_ms = Number($("ttl-" + c.holder).value);
  if (["renew", "release", "write"].includes(action)) r.token = c.token;
  if (action === "write") {
    r.expected_version = Number($("expected-" + c.holder).value);
    r.value = $("value-" + c.holder).value;
  }
  return r;
}
async function send(c, r) {
  c.last = r;
  const result = await api("/api/resources/" + key + "/commands", r),
    receipt = result.receipt;
  selectedReceipt = {
    receipt,
    hash: result.hash,
    canonical_payload: result.canonical_payload,
  };
  if (receipt.allowed && !result.replayed) {
    if (r.action === "acquire") c.token = receipt.after.fence;
    if (["acquire", "write"].includes(r.action))
      $("expected-" + c.holder).value = receipt.after.version;
  }
  $("result-" + c.holder).textContent =
    `${result.replayed ? "Original receipt replayed" : "New receipt"} #${receipt.sequence}: ${receipt.reason} · HTTP ${result.httpStatus}.`;
  $("result-" + c.holder).className =
    "last-result " + (receipt.allowed ? "" : "denied");
  $("status").textContent = result.replayed
    ? "Retry returned the original decision. It did not execute the command again."
    : "Command and receipt committed together.";
  await refresh();
}
function act(c, action) {
  if (action === "sync") {
    $("expected-" + c.holder).value = snapshot.state.version;
    return;
  }
  if (action === "capture") {
    c.captured = request(c, "write");
    $("captured-" + c.holder).textContent =
      `Token ${c.captured.token} · expected v${c.captured.expected_version}\n${c.captured.value}`;
    controls();
    return;
  }
  guarded(() =>
    send(
      c,
      action === "retry"
        ? c.last
        : action === "deliver"
          ? c.captured
          : request(c, action),
    ),
  );
}
$("create-form").onsubmit = (e) => {
  e.preventDefault();
  guarded(async () => {
    const id = $("resource-id").value;
    await api("/api/resources", { id });
    await listing(id);
    $("status").textContent = "Resource opened. Existing state is preserved.";
  });
};
$("resources").onchange = () =>
  guarded(async () => {
    key = $("resources").value;
    await change();
  });
$("refresh").onclick = () => guarded(refresh);
$("export").onclick = () =>
  guarded(async () => {
    await refresh();
    const url = URL.createObjectURL(
        new Blob([JSON.stringify(snapshot, null, 2)], {
          type: "application/json",
        }),
      ),
      a = document.createElement("a");
    a.href = url;
    a.download = key + "-lease-evidence.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
guarded(() => listing());
