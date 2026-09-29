const state = {
  experienceMode: "showcase",
  uiConfig: {
    defaultExperience: "showcase",
    switchEnabled: true,
    showcaseEnabled: true,
  },
  viewer: null,
  home: { hero: null, rows: [] },
  selectedTitle: null,
  memories: null,
  profile: null,
  metrics: null,
  status: null,
  agentCatalog: null,
  agentTraces: [],
  latestAgentTrace: null,
  traceThread: null,
  selectedTraceEvent: null,
  entitlement: null,
  chatBusy: false,
  ready: false,
  rowDescriptors: [],
  rowMap: new Map(),
  showcase: null,
  showcaseTab: "presenter",
  searchLab: null,
  governance: null,
  evaluation: null,
};

const $ = (id) => document.getElementById(id);
const EXPERIENCE_STORAGE_KEY = "streamai.experience-mode.v1";

function isShowcaseExperience() { return state.experienceMode === "showcase"; }

function storedExperienceMode() {
  try {
    const value = window.localStorage.getItem(EXPERIENCE_STORAGE_KEY);
    return value === "showcase" || value === "customer" ? value : null;
  } catch (_) { return null; }
}

function closeShowcaseSurfaces() {
  ["showcaseDialog", "memoryDialog", "traceThreadDialog"].forEach((id) => {
    const dialog = $(id);
    if (dialog?.open) dialog.close();
  });
  const evidence = $("detailEvidenceScorecard");
  if (evidence) evidence.hidden = true;
}

function applyExperienceMode(requestedMode, { persist = false, announce = false } = {}) {
  const showcaseAllowed = state.uiConfig.showcaseEnabled !== false;
  const mode = requestedMode === "showcase" && showcaseAllowed ? "showcase" : "customer";
  state.experienceMode = mode;
  document.documentElement.dataset.experienceMode = mode;
  document.title = mode === "showcase" ? "StreamAI · Couchbase Showcase" : "StreamAI";

  const toggle = $("experienceModeToggle");
  if (toggle) {
    toggle.checked = mode === "customer";
    toggle.disabled = state.uiConfig.switchEnabled === false;
  }
  if ($("experienceSwitcher")) $("experienceSwitcher").hidden = state.uiConfig.switchEnabled === false;
  if ($("experienceModeLabel")) $("experienceModeLabel").textContent = mode === "showcase" ? "Showcase" : "Customer";

  if ($("authBrandMark")) $("authBrandMark").textContent = mode === "showcase" ? "CB" : "S";
  if ($("searchEyebrow")) $("searchEyebrow").textContent = mode === "showcase" ? "Catalogue retrieval" : "Find your next watch";
  if ($("searchHeading")) $("searchHeading").textContent = mode === "showcase" ? "Search the catalogue" : "Search films and TV";
  if ($("authDescription")) $("authDescription").textContent = mode === "showcase"
    ? "The screen loads immediately. Login becomes available as soon as the local services are ready."
    : "Sign in to continue watching and get recommendations shaped around your preferences.";
  if ($("profileEyebrow")) $("profileEyebrow").textContent = mode === "showcase" ? "Operational viewer state" : "Your viewing preferences";
  if ($("profileDescription")) $("profileDescription").textContent = mode === "showcase"
    ? "Edit explicit preferences and inspect the exact likes, dislikes and history that drive recommendations."
    : "Manage the preferences, likes and viewing history that shape your recommendations.";

  if (mode === "customer") closeShowcaseSurfaces();
  if (persist && state.uiConfig.switchEnabled !== false) {
    try { window.localStorage.setItem(EXPERIENCE_STORAGE_KEY, mode); } catch (_) {}
  }
  if (announce) {
    showToast(mode === "showcase"
      ? "Couchbase Showcase view enabled — all technical panels are visible."
      : "Customer App view enabled — demo and operator panels are hidden.");
    if (mode === "showcase" && state.viewer) { loadMetrics(); refreshStatus(); }
  }
}

async function loadExperienceConfig() {
  try {
    const payload = await api("/api/ui-config");
    state.uiConfig = { ...state.uiConfig, ...(payload.data || {}) };
  } catch (error) {
    console.warn("UI experience config unavailable; using showcase defaults", error);
  }
  const configured = state.uiConfig.defaultExperience === "customer" ? "customer" : "showcase";
  const selected = state.uiConfig.switchEnabled === false ? configured : (storedExperienceMode() || configured);
  applyExperienceMode(selected);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.detail || `${response.status} ${response.statusText}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function consumerSafeText(value) {
  return String(value || "")
    .replace(/Grounded\s+Couchbase\s+catalogue\s+results/gi, "Verified catalogue results")
    .replace(/Couchbase\s+catalogue/gi, "catalogue")
    .replace(/Couchbase\s+catalog/gi, "catalogue")
    .replace(/recorded\s+in\s+Couchbase/gi, "recorded in your viewing history")
    .replace(/Couchbase\s+entitlement\s+document/gi, "subscription profile")
    .replace(/Couchbase\s+Agent\s+Catalog/gi, "governed tool catalogue")
    .replace(/Couchbase\s+MCP\s+Server/gi, "data access service")
    .replace(/\bCouchbase\b/gi, "the platform");
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 2600);
}

function formatNumber(value) { return new Intl.NumberFormat("en-GB").format(Number(value || 0)); }
function formatCost(value, currency = "GBP") {
  const amount = Number(value || 0);
  const digits = amount >= 1 ? 2 : amount >= 0.01 ? 3 : 5;
  try {
    return new Intl.NumberFormat("en-GB", {
      style: "currency",
      currency: String(currency || "GBP").toUpperCase(),
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(amount);
  } catch (_) {
    return `£${amount.toFixed(digits)}`;
  }
}

function renderSavingsComparison(data) {
  const m = data.measured || {}, t = m.memorySavings || {}, phases = t.phases || {};
  const previous = phases.previous || {}, optimized = phases.optimized || {}, diff = t.difference || {};
  const valid = m.available === true && t.validComparison === true;
  const display = n => n == null ? "Unavailable" : formatNumber(n);
  const money = n => n == null ? "Unavailable" : new Intl.NumberFormat("en-US", {style:"currency",currency:"USD",minimumFractionDigits:2,maximumFractionDigits:6}).format(n);
  const keys = ["llmRequests", "llmTokens", "referenceCostUsd"];
  const delta = (key, cash) => !valid || diff[key] == null ? (t.id ? "Unavailable" : "Run comparison") :
    (cash ? money(Math.abs(diff[key])) : display(Math.abs(diff[key]))) + (diff[key] < 0 ? " extra" : "");
  $("comparisonScope").textContent = t.id ? "Measured memory-policy comparison · " + display(t.turns) + " chat turns + " + display(t.facts) + " explicit facts" : "Replay 2–4 completed chat turns under both memory policies.";
  const total = m.totals || {};
  $("meteredUsageSummary").textContent = m.available ? "Live deployment usage: " + display(total.chatRequests) + " LLM requests · " + display(total.embeddingRequests) + " embedding requests. The comparison below is a separate recorded trial." : "Usage gateway unavailable.";
  const cards = [
    [valid && diff.llmRequests < 0 ? "Extra LLM calls" : "LLM calls avoided", delta("llmRequests"), display(previous.llmRequests) + " previous · " + display(optimized.llmRequests) + " selective"],
    [valid && diff.llmTokens < 0 ? "Extra LLM tokens" : "LLM tokens saved", delta("llmTokens"), display(previous.llmTokens) + " previous · " + display(optimized.llmTokens) + " selective"],
    [valid && diff.referenceCostUsd < 0 ? "Extra token-cost equivalent" : "Token-cost equivalent saved", delta("referenceCostUsd", true), "Illustrative USD rates · LLM + embeddings"]
  ];
  $("savingsCards").innerHTML = cards.map(function(row, i) {
    return '<article class="metric-card ' + (!valid ? 'comparison-unknown' : diff[keys[i]] < 0 ? 'comparison-extra' : '') + '"><span>' + escapeHtml(row[0]) + '</span><strong>' + escapeHtml(row[1]) + '</strong><small>' + escapeHtml(row[2]) + '</small></article>';
  }).join("");
  $("savingsCoverage").textContent = "Previous policy: summarize every block. Selective policy: keep short originals and explicit facts, with embeddings, without redundant LLM summaries. This measures memory-processing savings, not whole-application or billed savings.";
  let status = "Complete at least two chat turns, wait for memory writes, then select Compare memory policies. This runs real model requests.";
  if (!m.available) status = "Usage monitoring is unavailable. No savings claim can be made.";
  else if (t.status === "running") status = "Comparison running. Please pause other demo activity until it finishes.";
  else if (valid) status = "Both policies retained the same original content in ready memory blocks." + (keys.some(k=>diff[k]<0) ? " Some measured usage increased; the extra usage is shown." : " The recorded difference is shown above.");
  else if (t.id) status = [t.error, ...(t.problems || []), "Comparison incomplete. No savings claim is shown."].filter(Boolean).join(" ");
  $("savingsStatus").textContent = status;
  $("runMemoryComparison").disabled = t.status === "running";
  const rows = [["LLM requests","llmRequests"],["LLM input tokens","llmInputTokens"],["LLM output tokens","llmOutputTokens"],["Embedding requests","embeddingRequests"],["Embedding input tokens","embeddingInputTokens"],["Token-cost equivalent (USD)","referenceCostUsd"],["Routed background requests included","backgroundRequests"]];
  $("savingsTable").innerHTML = '<table class="comparison-table"><thead><tr><th>Recorded metric</th><th>Previous policy</th><th>Selective policy</th></tr></thead><tbody>' + rows.map(function(row) {
    const format = row[1] === "referenceCostUsd" ? money : display;
    return '<tr><td>' + escapeHtml(row[0]) + '</td><td>' + escapeHtml(format(previous[row[1]])) + '</td><td>' + escapeHtml(format(optimized[row[1]])) + '</td></tr>';
  }).join("") + '</tbody></table>';
  const max = Math.max(previous.llmTokens || 0, optimized.llmTokens || 0, 1);
  $("comparisonBaselineBar").style.width = (previous.llmTokens || 0)/max*100 + "%";
  $("comparisonObservedBar").style.width = (optimized.llmTokens || 0)/max*100 + "%";
  $("comparisonBaselineTokens").textContent = display(previous.llmTokens);
  $("comparisonObservedTokens").textContent = display(optimized.llmTokens);
  $("comparisonReduction").textContent = valid && t.llmReductionPct != null ? Math.abs(t.llmReductionPct).toFixed(1) + "% " + (t.llmReductionPct < 0 ? "more" : "fewer") + " measured LLM tokens" : "Waiting for a complete measured comparison";
  $("comparisonLimitations").textContent = t.limitations || "Both policies run on the same conversation. Provider-reported tokens only. Missing usage prevents a savings claim. AI Functions and infrastructure are outside this memory-only experiment.";
  const receipt = t.id ? {trialId:t.id, startedAt:t.startedAt, finishedAt:t.finishedAt, contentFingerprint:t.fingerprint,
    prices:t.prices, previousProof:previous.proof, selectiveProof:optimized.proof,
    previousRequestIds:previous.requestIds, selectiveRequestIds:optimized.requestIds,
    entireTrialUsage:t.totalTrialUsage, outsidePhaseOverhead:t.otherTrialOverhead, isolatedUserRemoved:t.cleanupSucceeded,
    costNote:"The entire trial cost includes BOTH policies. The difference compares the individual policies, not the cost of performing the experiment."} : {};
  $("memoryTrialReceipt").textContent = JSON.stringify(receipt, null, 2);
  const aif = m.aiFunctions || {};
  $("externalAIUsage").textContent = display(aif.completed) + " guides generated · " + display(aif.guideReuses) + " saved-guide reuses. AI Functions tokens and cost are not reported by this meter.";
  const form = $("comparisonAssumptionsForm");
  if (form?.dataset?.dirty !== "true") {
    Object.entries(m.comparison?.assumptions || {}).forEach(function(entry) {
      const input = $("comparison_"+entry[0]); if (input) input.value=entry[1];
    });
  }
}

async function runMemoryComparison() {
  $("runMemoryComparison").disabled = true;
  try {
    await api("/api/metrics/memory-trial", {method:"POST"});
    $("savingsStatus").textContent = "Starting comparison. This can take a few minutes.";
    const poll = async () => {
      try {
        const status = await api("/api/metrics/memory-trial/status");
        await loadMetrics();
        if (status.running) {
          $("runMemoryComparison").disabled = true;
          $("savingsStatus").textContent = status.progress + ". Please pause other demo activity.";
          setTimeout(poll, 2500);
        } else {
          $("runMemoryComparison").disabled = false;
          if (status.progress) showToast(status.progress);
        }
      } catch (error) {
        $("runMemoryComparison").disabled = false;
        showToast(error.message);
      }
    };
    setTimeout(poll, 1000);
  } catch (error) {
    $("runMemoryComparison").disabled = false;
    showToast(error.message);
  }
}

async function saveComparisonAssumptions(event) {
  event.preventDefault();
  const form = $("comparisonAssumptionsForm");
  const values = Object.fromEntries(new FormData(form).entries());
  Object.keys(values).forEach(key => { values[key]=Number(values[key]); });
  try {
    const payload = await api("/api/metrics/comparison", {method:"PUT",body:JSON.stringify(values)});
    form.dataset.dirty="false"; renderMetrics(payload.data); showToast("Comparison assumptions updated for this deployment");
  } catch (error) { showToast(error.message); }
}

function renderMetrics(data) {
  if (!data || !isShowcaseExperience()) return;
  state.metrics = data;
  renderSavingsComparison(data);
  const m = data.measured || {};
  const runtime = data.runtime || {};
  const available = m.available === true;
  const t = m.totals || {}, a = m.activity || {}, memory = m.memoryApi || {};
  const value = (n) => available && n != null ? n : "Unavailable";
  const knownInput = t.requests > 0 && !t.inputUsageReports ? null : t.reportedInputTokens;
  const knownOutput = t.chatRequests > 0 && !t.chatOutputUsageReports ? null : t.reportedOutputTokens;
  const missing = Number(t.requestsWithMissingUsage || 0);
  const memoryLlm = (m.bySource || []).filter(r => r.source === "agent_memory" && r.operation === "chat/completions").reduce((n,r) => n + r.requests, 0);
  const start = m.windowStartedAt ? new Date(m.windowStartedAt * 1000).toLocaleString("en-GB") : "unavailable";
  $("metricsRuntimeNote").textContent = `${runtime.description || "Configured model service"}. Model totals cover this deployment since ${start}, including Agent Memory, ingestion, setup and retries. App activity below is for ${m.activityViewer || "the selected viewer"}.`;
  const notes = [];
  if (!available) notes.push(m.error || "Usage monitoring is unavailable. Missing values are not zero.");
  if (missing) notes.push(`${missing} inference requests have missing or incomplete token usage; reported-token sums cover only the known portion.`);
  if (m.activityReportingFailuresThisProcess) notes.push(`${m.activityReportingFailuresThisProcess} app observations could not be recorded in this UI process; activity counts are incomplete.`);
  if (m.persistence?.storage === 'couchbase') notes.push('Usage history and reporting settings are stored directly in Couchbase.');
  notes.push("Model usage before monitoring started is unavailable. External callers, local Agent Catalog embedding compute, managed services that bypass this gateway and infrastructure usage are outside this ledger.");
  $("metricsCoverageNote").textContent = notes.join(" ");
  const cards = [
    ["Observed LLM requests", value(t.chatRequests), `${formatNumber(memoryLlm)} from Agent Memory, including its checks`],
    ["Observed embedding requests", value(t.embeddingRequests), "All routed sources; includes batches and retries"],
    ["Model request failures", value(t.failed), `${formatNumber(t.unknownOutcome)} unknown outcomes · ${formatNumber(t.inFlight)} in flight`],
    ["Requests with token usage", available ? `${formatNumber(Number(t.requests || 0) - missing)} / ${formatNumber(t.requests)}` : "Unavailable", "Complete provider usage; missing reports stay unknown"],
    ["Reported input tokens", value(knownInput), `${formatNumber(t.inputUsageReports)} of ${formatNumber(t.requests)} requests reported input usage`],
    ["Reported output tokens", value(knownOutput), `${formatNumber(t.chatOutputUsageReports)} of ${formatNumber(t.chatRequests)} LLM requests reported output usage`],
    ["Memory API requests", value(memory.requests), `${formatNumber(memory.writeRequestsAccepted)} writes accepted · ${formatNumber(memory.healthRequests)} health checks · ${formatNumber(memory.failed)} failures`],
    ["Recorded app operations", value(a.recordedOperations), "Selected viewer: chat, search, actions and preference edits"],
    ["Chats without planner request", value(a.chatTurnsWithoutPlannerRequest), `Of ${formatNumber(a.completedChatTurns)} completed turns; background models may still run`],
    ["Validated plans reused", value(a.plansReused), `${formatNumber(a.plannerDecisions)} planner decisions · ${a.planReuseRatePct == null ? "rate unavailable" : a.planReuseRatePct + "% reuse"}`],
    ["Mean server answer time", available && a.meanServerAnswerMs != null ? `${a.meanServerAnswerMs} ms` : "Unavailable", `${formatNumber(a.answerTimeSamples)} completed chats; excludes background work and browser delivery`],
    ["Context facts supplied", value(a.contextFactOccurrencesSupplied), "Occurrences, not unique facts or proof of useful recall"],
  ];
  $("metricsGrid").innerHTML = cards.map(([label, n, detail]) => `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(typeof n === "number" ? formatNumber(n) : n)}</strong><small>${escapeHtml(detail)}</small></article>`).join("");
  const count = Number(t.requests || 0), complete = count - missing;
  $("tokensUsedBar").style.width = `${count ? complete / count * 100 : 0}%`;
  $("tokensSavedBar").style.width = `${count ? missing / count * 100 : 0}%`;
  $("tokenReductionLabel").textContent = available ? `${formatNumber(complete)} complete reports · ${formatNumber(missing)} missing/incomplete` : "Unavailable";
  $("costSavingsLabel").textContent = "No measured saving comparison";
  $("costComparison").innerHTML = `<div><span>Capella billed cost</span><strong>Unavailable</strong></div><div><span>Verified cost saving</span><strong>Not measured</strong></div>`;
  $("metricsPricingNote").textContent = m.billing?.basis || "Capella Model Service is billed by provisioned capacity and clock hours. The demo does not have billing data. Token reductions do not establish cash savings.";
  const sourceLabel = s => ({app:"Application, including cache preparation", agent_memory:"Agent Memory, including health probes", ingestion:"Catalogue ingestion", setup:"Setup validation", diagnostic:"Labelled diagnostics"}[s] || s);
  const rows = m.bySource || [];
  $("modelUsageBreakdown").innerHTML = rows.length ? `<table style="width:100%;text-align:left"><thead><tr><th>Source / model</th><th>Requests</th><th>Input tokens</th><th>Output tokens</th><th>Missing usage</th></tr></thead><tbody>${rows.map(r => `<tr><td>${escapeHtml(sourceLabel(r.source))}<br><small>${escapeHtml(r.model)} · ${escapeHtml(r.operation)}</small></td><td>${formatNumber(r.requests)}<br><small>${formatNumber(r.failed)} failed · ${formatNumber(r.unknownOutcome)} unknown</small></td><td>${r.inputUsageReports ? formatNumber(r.reportedInputTokens) : "Unavailable"}</td><td>${r.operation === "embeddings" ? "Not applicable" : r.chatOutputUsageReports ? formatNumber(r.reportedOutputTokens) : "Unavailable"}</td><td>${formatNumber(r.requestsWithMissingUsage)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty-row">${available ? "No inference requests observed in this reporting window." : "Model usage is unavailable."}</div>`;
  $("metricsEventList").innerHTML = (m.recentRequests || []).slice(0,20).map(e => {
    const usage = e.usage || {};
    const tokens = n => n == null ? "unknown" : formatNumber(n);
    const time = new Date(e.timestamp*1000).toLocaleTimeString("en-GB");
    return `<article class="metric-event"><div><strong>${escapeHtml(sourceLabel(e.source))}</strong><span>${escapeHtml(time)} · ${escapeHtml(e.operation)} · ${escapeHtml(e.status)}</span></div><div class="metric-event-values"><span>Input ${tokens(usage.inputTokens)}</span><span>Output ${e.operation === "embeddings" ? "n/a" : tokens(usage.outputTokens)}</span><span>${escapeHtml(e.durationMs == null ? "in flight" : e.durationMs + " ms")}</span></div></article>`;
  }).join("") || `<div class="empty-row">No recorded model requests in this window. Historical usage has not been reconstructed.</div>`;
}

async function loadMetrics() {
  if (!state.viewer || !isShowcaseExperience() || state.metricsLoading) return;
  state.metricsLoading = true;
  try { const payload = await api("/api/metrics"); renderMetrics(payload.data); }
  catch (error) { renderMetrics({measured: {available: false, error: "Unable to refresh usage. Previous figures must not be treated as current."}}); }
  finally { state.metricsLoading = false; }
}

function refreshVisibleMetrics() {
  if (!state.viewer || !isShowcaseExperience() || document.visibilityState !== "visible") return;
  const section = $("metricsSection");
  if (!section || section.hidden) return;
  const bounds = section.getBoundingClientRect();
  if (bounds.top < window.innerHeight && bounds.bottom > 0) void loadMetrics();
}

async function resetMetrics() {
  if (!state.viewer) return;
  if (!window.confirm("Start a new reporting window for this deployment and all viewers? The raw usage audit history is retained. Viewer preferences and history are not affected.")) return;
  try { const payload = await api("/api/metrics/reset", { method: "POST" }); renderMetrics(payload.data); showToast("New reporting window started"); }
  catch (error) { showToast(error.message); }
}

function formatRuntime(item) {
  const minutes = item.runtimeMinutes || item.episodeRuntimeMinutes;
  if (!minutes) return "";
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}h ${m ? `${m}m` : ""}`.trim();
}

function contentTypeLabel(item) { return item.contentType === "tv" ? "Series" : "Film"; }
function posterUrl(item) { return item.posterUrl || (item.posterPath ? `https://image.tmdb.org/t/p/w342${item.posterPath}` : ""); }
function backdropUrl(item) { return item.backdropUrl || (item.backdropPath ? `https://image.tmdb.org/t/p/w1280${item.backdropPath}` : ""); }

function searchModeLabel(value) {
  const labels = { fts: "FTS", vector: "Vector", hybrid: "Hybrid", fts_structured: "FTS structured", sqlpp_exact_fallback: "SQL++ exact fallback", sqlpp_fallback: "SQL++ fallback" };
  return labels[String(value || "").toLowerCase()] || String(value || "grounded").replaceAll("_", " ");
}

function itemModeSummary(item) {
  const requested = item.requestedSearchMode || item.matchExplanation?.requestedMode;
  const effective = item.effectiveSearchMode || item.matchExplanation?.effectiveMode || item.recommendationSource;
  if (requested && effective && requested !== effective) return `${searchModeLabel(effective)} (requested ${searchModeLabel(requested)})`;
  return effective ? searchModeLabel(effective) : "";
}

function titleReason(item) {
  if (item.heroReason) return item.heroReason;
  if (!isShowcaseExperience()) {
    return item.matchExplanation?.consumerSummary || "Recommended from catalogue relevance and your viewing preferences.";
  }
  const explanation = item.matchExplanation?.summary;
  const reasons = [];
  if (explanation) reasons.push(explanation);
  if (item.matchPct) reasons.push(`${item.matchPct}% relative rank`);
  const mode = itemModeSummary(item);
  if (mode) reasons.push(mode);
  return reasons.join(" · ") || "Catalogue ranking and viewer signals";
}


function cardEvidenceText(item) {
  const explanation = item?.matchExplanation || {};
  return explanation.cardEvidence || explanation.summary || "";
}

function renderHero(item) {
  state.home.hero = item || null;
  if (!item) {
    $("heroBackdrop").style.backgroundImage = "radial-gradient(circle at 70% 35%, #28384e, #080b11 65%)";
    $("heroTitle").textContent = state.viewer ? "Loading personalised picks…" : "Your next story starts here.";
    $("heroOverview").textContent = state.viewer ? "Rows are loading progressively from the catalogue." : "Sign in to load a persistent viewer profile.";
    $("heroMeta").innerHTML = "";
    $("heroExplain").hidden = true;
    return;
  }
  const backdrop = backdropUrl(item);
  $("heroBackdrop").style.backgroundImage = backdrop ? `url("${backdrop}")` : "radial-gradient(circle at 70% 35%, #28384e, #080b11 65%)";
  $("heroTitle").textContent = item.title || "Your next story starts here.";
  $("heroOverview").textContent = item.overview || "Explore films and television using intelligent retrieval and persistent viewer memory.";
  const meta = [item.releaseYear, contentTypeLabel(item), formatRuntime(item), item.voteAverage ? `★ ${Number(item.voteAverage).toFixed(1)}` : "", item.genres?.slice(0, 3).join(" · ")].filter(Boolean);
  $("heroMeta").innerHTML = meta.map((m) => `<span>${escapeHtml(m)}</span>`).join("");
  $("heroExplain").hidden = false;
  $("heroReason").textContent = titleReason(item);
  $("heroPlay").onclick = () => recordInteraction(item, "play", 5);
  $("heroInfo").onclick = () => openDetail(item);
}

function stateBadge(item) {
  const labels = { liked: "Liked", disliked: "Disliked", watched: "Watched", watching: "Watching", watchlist: "My List" };
  return item.viewerState ? `<span class="viewer-state ${escapeHtml(item.viewerState)}">${escapeHtml(labels[item.viewerState] || item.viewerState)}</span>` : "";
}

function imageMarkup(item, mini = false) {
  const src = posterUrl(item);
  if (!src) return `<div class="poster-placeholder">${escapeHtml(item.title)}</div>`;
  return `<img src="${escapeHtml(src)}" alt="${escapeHtml(item.title)}" loading="lazy" decoding="async" fetchpriority="low" width="${mini ? 92 : 180}" height="${mini ? 138 : 270}" />`;
}

function entitlementBadge(item) {
  const decision = item?.entitlement;
  if (!decision) return "";
  if (!decision.allowed) return `<span class="entitlement-badge denied">Unavailable</span>`;
  if (decision.includedInPlan) return `<span class="entitlement-badge included">Included</span>`;
  return `<span class="entitlement-badge purchase">${escapeHtml(decision.offerType || "Purchase")}</span>`;
}

function renderCard(item, mini = false) {
  if (mini) {
    const button = document.createElement("button");
    button.className = "mini-card";
    button.innerHTML = `${imageMarkup(item, true)}<strong>${escapeHtml(item.title)}</strong>`;
    button.onclick = () => openDetail(item);
    return button;
  }
  const card = document.createElement("button");
  card.className = "title-card";
  card.type = "button";
  card.dataset.titleId = item.id || "";
  const progress = Number(item.progressPct || 0);
  card.innerHTML = `
    ${item.matchPct ? `<span class="match-badge" title="Relative rank within these displayed results; not a probability">${Math.round(item.matchPct)}% rank</span>` : ""}
    <span class="type-badge">${escapeHtml(contentTypeLabel(item))}</span>${stateBadge(item)}${entitlementBadge(item)}
    <div class="poster-wrap">${imageMarkup(item)}</div>
    <div class="card-overlay"><h3>${escapeHtml(item.title)}</h3><div class="card-meta"><span>${escapeHtml(item.releaseYear || "")}</span><span>${escapeHtml(item.genres?.[0] || "")}</span>${item.voteAverage ? `<span>★ ${Number(item.voteAverage).toFixed(1)}</span>` : ""}</div>${cardEvidenceText(item) ? `<p class="card-evidence">${escapeHtml(cardEvidenceText(item))}</p>` : ""}</div>
    ${progress > 0 && progress < 100 ? `<div class="progress-track"><span style="width:${Math.min(progress, 100)}%"></span></div>` : ""}`;
  card.onclick = () => openDetail(item);
  return card;
}

function rowSection(descriptor, skeleton = false) {
  const section = document.createElement("section");
  section.className = `carousel-section${skeleton ? " row-loading" : ""}`;
  section.dataset.rowId = descriptor.id;
  section.innerHTML = `<div class="section-heading"><div><h2>${escapeHtml(descriptor.title)}</h2><p>${escapeHtml(descriptor.subtitle || "")}</p></div><span class="row-cache"></span></div><div class="carousel"></div>`;
  if (skeleton) {
    const carousel = section.querySelector(".carousel");
    for (let i = 0; i < 7; i++) carousel.insertAdjacentHTML("beforeend", `<div class="card-skeleton"></div>`);
  }
  return section;
}

function renderRowDescriptors(descriptors = []) {
  state.rowDescriptors = descriptors;
  state.rowMap.clear();
  const container = $("rowsContainer");
  container.innerHTML = "";
  descriptors.forEach((descriptor) => {
    const section = rowSection(descriptor, true);
    state.rowMap.set(descriptor.id, { ...descriptor, items: [] });
    container.appendChild(section);
  });
}

function renderRow(row) {
  state.rowMap.set(row.id, row);
  let section = document.querySelector(`[data-row-id="${CSS.escape(row.id)}"]`);
  if (!section) {
    section = rowSection(row);
    $("rowsContainer").appendChild(section);
  }
  section.classList.remove("row-loading");
  section.querySelector("h2").textContent = row.title;
  section.querySelector("p").textContent = row.subtitle || "";
  section.querySelector(".row-cache").textContent = row.cache ? `cache ${row.cache}` : "";
  const carousel = section.querySelector(".carousel");
  carousel.innerHTML = "";
  if (!row.items?.length) {
    carousel.innerHTML = `<div class="empty-row">Nothing here yet.</div>`;
  } else {
    row.items.forEach((item) => carousel.appendChild(renderCard(item)));
  }
}

async function loadRow(rowId) {
  try {
    const payload = await api(`/api/home/row/${encodeURIComponent(rowId)}`);
    renderRow(payload.data);
  } catch (error) {
    const descriptor = state.rowDescriptors.find((row) => row.id === rowId) || { id: rowId, title: rowId };
    renderRow({ ...descriptor, items: [], subtitle: `Could not load: ${error.message}` });
  }
}

async function loadHomeProgressively() {
  if (!state.viewer) return;
  renderHero(null);
  try {
    const payload = await api("/api/home/shell");
    const shell = payload.data;
    renderHero(shell.hero);
    renderRowDescriptors(shell.rowDescriptors || []);
    await Promise.allSettled((shell.rowDescriptors || []).map((row) => loadRow(row.id)));
  } catch (error) {
    showToast(`Home could not load: ${error.message}`);
  }
}

async function refreshRows(ids) {
  await Promise.allSettled(ids.map(loadRow));
  try {
    const shell = await api("/api/home/shell");
    renderHero(shell.data.hero);
  } catch (_) {}
}

function setViewer(data) {
  state.viewer = data;
  const name = data?.user_name || data?.login_id || "Viewer";
  $("viewerName").textContent = name;
  $("viewerAvatar").textContent = name.slice(0, 1).toUpperCase();
  $("sessionLabel").textContent = data?.session_active ? `Session ${data.session_number}` : "Session ended";
  $("drawerViewer").textContent = name;
  $("drawerSession").textContent = data?.session_active ? `Session ${data.session_number} · ${data.session_id}` : "No active session";
  renderChatLog(data?.chat_log || []);
}

function renderChatLog(log) {
  const el = $("chatLog");
  el.innerHTML = "";
  if (!log.length) {
    el.innerHTML = `<div class="chat-message assistant">Ask naturally: what should I watch next, show me sci-fi movies, is a title available, or remember a durable preference. Every title answer is grounded in the verified catalogue.</div>`;
    return;
  }
  log.forEach((entry) => addMessage(entry.role, entry.content, null, false));
  el.scrollTop = el.scrollHeight;
}

function addMessage(role, content, meta = null, scroll = true) {
  const msg = document.createElement("div");
  msg.className = `chat-message ${role}`;
  msg.textContent = content;
  if (meta) {
    const small = document.createElement("span"); small.className = "message-meta"; small.textContent = meta; msg.appendChild(small);
  }
  $("chatLog").appendChild(msg);
  if (scroll) $("chatLog").scrollTop = $("chatLog").scrollHeight;
  return msg;
}

function openAssistant() { $("assistantDrawer").classList.add("open"); $("assistantDrawer").setAttribute("aria-hidden", "false"); $("drawerScrim").hidden = false; setTimeout(() => $("chatInput").focus(), 100); }
function closeAssistant() { $("assistantDrawer").classList.remove("open"); $("assistantDrawer").setAttribute("aria-hidden", "true"); $("drawerScrim").hidden = true; }
function openSearch(preset = "") { $("searchDock").hidden = false; if (preset) $("catalogueSearchInput").value = preset; setTimeout(() => $("catalogueSearchInput").focus(), 50); window.scrollTo({ top: 0, behavior: "smooth" }); }
function closeSearch() { $("searchDock").hidden = true; }

async function refreshStatus() {
  try {
    const payload = await api("/api/status");
    state.status = payload;
    if (payload.starting) {
      $("serviceStatusText").textContent = "Services starting";
      renderServices(payload);
      return;
    }
    const allHealthy = Object.values(payload.services || {}).every((s) => s.enabled === false || s.healthy !== false);
    $("serviceStatusButton").classList.toggle("healthy", allHealthy);
    $("serviceStatusText").textContent = allHealthy ? "Data Plane healthy" : "Check services";
    renderServices(payload);
  } catch (error) {
    $("serviceStatusText").textContent = "Service error";
  }
}

function renderServices(payload) {
  const grid = $("serviceGrid"); grid.innerHTML = "";
  const services = payload.services || {};
  const statusFor = (name) => services[name] || {};
  const cards = [
    ["Data / KV", "Catalogue, viewer state, exact likes/dislikes and watch history", statusFor("data_kv")],
    ["Query + Index", "Progressive rows, exclusions and structured profile reads", statusFor("query_index")],
    ["Search / FTS + Vector", "Field-aware lexical, semantic and hybrid catalogue retrieval", statusFor("search_fts_vector")],
    ["Agent Memory", "Durable preference facts synchronised with viewer actions", statusFor("agent_memory")],
    ["Agent Catalog", "Published, Git-versioned prompts and governed tools loaded with agentc", statusFor("agent_catalog")],
    ["Couchbase MCP Server", "Read-only Streamable HTTP database boundary using couchbase-mcp-server", statusFor("mcp_server")],
    ["Agent Tracer", "Native Agent Catalog spans plus an operational trace projection", statusFor("agent_tracer")],
    ["Model runtime", [statusFor("model_runtime").provider === "capella_model_service" ? "Capella Model Service" : statusFor("model_runtime").provider, statusFor("model_runtime").model].filter(Boolean).join(" · ") || "Configured chat and embedding services", statusFor("model_runtime")],
    ["AI Functions", "SQL++ classification, summarisation and sentiment enrichment", statusFor("ai_functions")],
    ["Data Processing", "Python loader locally or a Capella vectorisation workflow", statusFor("data_processing")],
    ["Entitlement policy", "Region, subscription tier, parental rating and viewer safety checks", statusFor("governed_agent")],
    ["AI usage telemetry", "Observed model requests, provider token reports and actual Memory HTTP requests", statusFor("usage_meter")],
  ];
  const aiFunctions = statusFor("ai_functions");
  const aiFunctionsButton = $("aiFunctionsProofButton");
  if (aiFunctionsButton) {
    const available = aiFunctions.enabled === true;
    aiFunctionsButton.disabled = !available;
    aiFunctionsButton.textContent = available ? "AI viewing guide" : "AI Functions · Capella only";
    aiFunctionsButton.title = available
      ? "Open a title’s saved AI viewing guide, or generate one with Capella AI Functions."
      : (aiFunctions.detail || "Native AI Functions require a configured Capella operational cluster and model association.");
    aiFunctionsButton.dataset.available = available ? "true" : "false";
  }

  cards.forEach(([title, detail, service]) => {
    const enabled = service.enabled !== false;
    const healthy = service.healthy !== false;
    const configured = enabled && healthy;
    const label = configured ? "Active" : enabled ? "Needs attention" : "Optional";
    const stateText = payload.starting ? "Starting" : configured ? "Ready" : enabled ? "Check config" : "Not configured";
    const card = document.createElement("div"); card.className = `service-card ${configured ? "" : "future"}`;
    card.innerHTML = `<div class="service-top"><span class="eyebrow">${escapeHtml(label)}</span><span class="service-state">${escapeHtml(stateText)}</span></div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(service.detail || detail)}</p>`;
    grid.appendChild(card);
  });
}

async function runCatalogueSearch(event) {
  event?.preventDefault();
  const query = $("catalogueSearchInput").value.trim(); if (!query) return;
  const button = $("catalogueSearchForm").querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Searching…";
  try {
    const payload = await api("/api/catalogue/search", { method: "POST", body: JSON.stringify({ query, mode: $("searchMode").value, content_type: $("contentType").value || null, limit: 24 }) });
    const result = payload.data;
    renderRowDescriptors([{ id: "search-results", title: `Results for “${query}”`, subtitle: "Only verified catalogue records" }]);
    const requestedMode = result.trace.requestedMode || $("searchMode").value;
    const effectiveMode = result.trace.effectiveMode || result.trace.mode;
    const modeText = requestedMode !== effectiveMode
      ? `Requested ${searchModeLabel(requestedMode)} → effective ${searchModeLabel(effectiveMode)}`
      : searchModeLabel(effectiveMode);
    const resultCount = result.trace.returnedCount ?? result.results.length;
    const subtitle = isShowcaseExperience()
      ? `${modeText} · ${resultCount} results`
      : `${resultCount} matching title${Number(resultCount) === 1 ? "" : "s"}`;
    renderRow({ id: "search-results", title: `Results for “${query}”`, subtitle, items: result.results });
    const parts = [`<strong>${escapeHtml(modeText)}</strong>`, `${escapeHtml(result.trace.elapsedMs || "?")} ms`, `${escapeHtml(result.trace.candidateCount ?? "?")} candidates`];
    if (result.trace.structuredIntent) parts.push(`intent: ${escapeHtml(result.trace.structuredIntent)}`);
    if (result.trace.expandedTerms?.length) parts.push(`expanded: ${escapeHtml(result.trace.expandedTerms.join(", "))}`);
    if (result.trace.modeReason) parts.push(`route: ${escapeHtml(result.trace.modeReason.replaceAll("_", " "))}`);
    if (result.trace.rankingStrategy) parts.push(`ranking: ${escapeHtml(result.trace.rankingStrategy.replaceAll("_", " "))}`);
    parts.push("% is a relative final rank, not a probability");
    $("retrievalTrace").innerHTML = parts.join(" · "); $("retrievalTrace").hidden = false;
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; button.textContent = "Search"; }
}

function traceTime(value) {
  const timestamp = Number(value || 0) * 1000;
  if (!timestamp) return "—";
  return new Date(timestamp).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3 });
}

function prettyJson(value) {
  try { return JSON.stringify(value ?? null, null, 2); }
  catch (_) { return String(value ?? ""); }
}

function downloadJson(filename, value) {
  const blob = new Blob([prettyJson(value)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
  URL.revokeObjectURL(url);
}

function eventTypeLabel(type) {
  return ({ user_message: "User", agent_plan: "Plan", tool_call: "Tool", model_generation: "Model", assistant_message: "Assistant", error: "Error" })[type] || String(type || "Event");
}

function renderTraceEventDetail(event) {
  state.selectedTraceEvent = event;
  if (!event) { $("traceEventDetail").innerHTML = `<div class="empty-row">Select an event to inspect it.</div>`; return; }
  const payloadRows = [];
  if (event.content) payloadRows.push(["Content", event.content]);
  if (event.inputs && Object.keys(event.inputs).length) payloadRows.push(["Inputs", event.inputs]);
  if (event.output !== undefined && event.output !== null) payloadRows.push(["Output", event.output]);
  if (event.details && Object.keys(event.details).length) payloadRows.push(["Details", event.details]);
  if (event.error) payloadRows.push(["Error", event.error]);
  $("traceEventDetail").innerHTML = `<div class="trace-detail-heading"><span class="trace-event-kind ${escapeHtml(event.type)}">${escapeHtml(eventTypeLabel(event.type))}</span><h3>${escapeHtml(event.label || event.type)}</h3><p>${escapeHtml(traceTime(event.timestamp))} · ${escapeHtml(event.component || "Agent activity")} · ${escapeHtml(event.durationMs != null ? `${event.durationMs} ms` : event.status || "")}</p></div>${payloadRows.map(([label, value]) => `<section><strong>${escapeHtml(label)}</strong>${typeof value === "string" ? `<p>${escapeHtml(value)}</p>` : `<pre>${escapeHtml(prettyJson(value))}</pre>`}</section>`).join("") || `<div class="empty-row">No payload was recorded for this event.</div>`}`;
}

function renderTraceThread(thread, focusTraceId = null, focusSpanIndex = null) {
  state.traceThread = thread;
  const turns = thread?.turns || [];
  $("traceThreadTitle").textContent = `Session ${thread?.sessionId || "single turn"}`;
  $("traceThreadMeta").textContent = `${turns.length} turn${turns.length === 1 ? "" : "s"} · ${thread?.eventCount || 0} events · ${thread?.catalogIds?.length || 0} Agent Catalog version${thread?.catalogIds?.length === 1 ? "" : "s"}`;
  $("traceComponentPath").innerHTML = (thread?.components || []).map((component, index) => `${index ? `<span class="trace-path-arrow">→</span>` : ""}<span class="trace-path-chip">${escapeHtml(component)}</span>`).join("");
  $("traceThreadTimeline").innerHTML = turns.length ? turns.map((turn) => `<article class="trace-turn" data-trace-id="${escapeHtml(turn.traceId)}"><div class="trace-turn-heading"><div><span>Turn ${turn.turnNumber}</span><strong>${escapeHtml(String(turn.intent || "agent turn").replaceAll("_", " "))}</strong></div><div><em class="${turn.status === "completed" ? "ok" : "bad"}">${escapeHtml(turn.status || "unknown")}</em><small>${escapeHtml(turn.responseMs ?? "—")} ms · ${turn.llmInvoked ? "App planner requested" : "No app planner request"}</small></div></div><div class="trace-turn-events">${(turn.events || []).map((event) => `<button class="trace-event ${escapeHtml(event.type)}" data-event-id="${escapeHtml(event.eventId)}"><span>${escapeHtml(traceTime(event.timestamp))}</span><strong>${escapeHtml(event.label || event.type)}</strong><small>${escapeHtml(event.component || "Agent activity")}${event.durationMs != null ? ` · ${escapeHtml(event.durationMs)} ms` : ""}</small></button>`).join("")}</div></article>`).join("") : `<div class="empty-row">No persisted Agent Tracer turns were found.</div>`;
  document.querySelectorAll("#traceThreadTimeline [data-event-id]").forEach((button) => button.onclick = () => {
    const event = (thread.events || []).find((item) => item.eventId === button.dataset.eventId);
    document.querySelectorAll("#traceThreadTimeline .trace-event").forEach((item) => item.classList.remove("selected"));
    button.classList.add("selected");
    renderTraceEventDetail(event);
  });
  let focusEvent = null;
  if (focusTraceId != null && focusSpanIndex != null) focusEvent = (thread.events || []).find((event) => event.traceId === focusTraceId && Number(event.spanIndex) === Number(focusSpanIndex));
  if (!focusEvent && focusTraceId) focusEvent = (thread.events || []).find((event) => event.traceId === focusTraceId && event.type === "user_message");
  if (!focusEvent) focusEvent = thread.events?.[0] || null;
  if (focusEvent) {
    const button = document.querySelector(`#traceThreadTimeline [data-event-id="${CSS.escape(focusEvent.eventId)}"]`);
    button?.classList.add("selected");
    button?.scrollIntoView({ block: "center" });
  }
  renderTraceEventDetail(focusEvent);
}

function singleTraceThread(trace) {
  const started = Number(trace.startedAt || trace.timestamp || Date.now() / 1000);
  const events = [
    { eventId: `${trace.traceId}::user`, traceId: trace.traceId, type: "user_message", label: "User", timestamp: started, component: "Conversation", content: trace.userMessage || "" },
    { eventId: `${trace.traceId}::plan`, traceId: trace.traceId, type: "agent_plan", label: "Router and agent plan", timestamp: started + 0.0001, component: "Agent Catalog", content: trace.plan?.intent || trace.responseMode || "agent_turn", details: trace.plan || {} },
    ...(trace.spans || trace.toolCalls || []).map((span, index) => ({ eventId: `${trace.traceId}::tool::${index}`, traceId: trace.traceId, spanIndex: index, type: "tool_call", label: span.name, timestamp: span.startedAt || started, component: span.executionSource === "agent_catalog_mcp" ? "Agent Catalog → MCP" : "Application SDK", durationMs: span.durationMs, status: span.status, inputs: span.inputs || {}, output: span.output, error: span.error, details: { components: span.components || [], transport: span.transport, catalogId: span.catalogId } })),
    { eventId: `${trace.traceId}::assistant`, traceId: trace.traceId, type: "assistant_message", label: "Assistant", timestamp: trace.completedAt || started, component: "Conversation", content: trace.assistantResponse || "", details: { responseMode: trace.responseMode, policy: trace.policy || {}, groundedTitleIds: trace.groundedTitleIds || [] } },
  ];
  const components = [...new Set(events.flatMap((event) => [event.component, ...(event.details?.components || [])]).filter(Boolean))];
  return { sessionId: trace.sessionId || null, viewerId: trace.viewerId, turnCount: 1, eventCount: events.length, components, catalogIds: trace.agentCatalogId ? [trace.agentCatalogId] : [], events, turns: [{ turnNumber: 1, traceId: trace.traceId, status: trace.status, intent: trace.plan?.intent || trace.responseMode, responseMs: trace.responseMs, llmInvoked: trace.llmInvoked, events, rawTrace: trace }] };
}

async function openTraceThread(trace = state.latestAgentTrace, focusSpanIndex = null) {
  if (!trace) return showToast("No Agent Tracer activity is available yet.");
  try {
    let thread = null;
    if (trace.sessionId) {
      const payload = await api(`/api/agent/sessions/${encodeURIComponent(trace.sessionId)}?limit=100`);
      thread = payload.data;
    } else thread = singleTraceThread(trace);
    renderTraceThread(thread, trace.traceId, focusSpanIndex);
    $("traceThreadDialog").showModal();
  } catch (error) {
    renderTraceThread(singleTraceThread(trace), trace.traceId, focusSpanIndex);
    $("traceThreadDialog").showModal();
    showToast(`Session replay used the current turn only: ${error.message}`);
  }
}

function renderAgentInspector(trace = state.latestAgentTrace) {
  if (!trace) return;
  state.latestAgentTrace = trace;
  const plan = trace.plan || {};
  const spans = trace.spans || trace.toolCalls || [];
  const policy = trace.policy || {};
  const grounded = trace.groundedTitleIds || [];
  const status = trace.status || "running";
  const traceId = trace.traceId || "pending";
  const nativeTrace = trace.nativeTraceSession || "native trace pending";
  const catalogId = trace.agentCatalogId || "catalog version pending";
  const activeComponents = [...new Set(["Agent Catalog", ...spans.flatMap((span) => span.components || []), ...spans.filter((span) => span.executionSource === "agent_catalog_mcp").map(() => "MCP Server"), trace.llmInvoked ? "Model Runtime" : null].filter(Boolean))];
  $("agentTraceSummary").innerHTML = `<div class="trace-hero"><div><span class="trace-status ${escapeHtml(status)}">${escapeHtml(status)}</span><strong>${escapeHtml(String(plan.intent || trace.responseMode || "agent turn").replaceAll("_", " "))}</strong><small>${escapeHtml(traceId)} · ${escapeHtml(nativeTrace)} · ${escapeHtml(catalogId)}</small><div class="trace-active-path">${activeComponents.map((item, index) => `${index ? `<span>→</span>` : ""}<em>${escapeHtml(item)}</em>`).join("")}</div></div><div><strong>${escapeHtml(trace.responseMs ?? "—")} ms</strong><span>${formatNumber(spans.length)} tools · ${formatNumber(grounded.length)} grounded titles · ${trace.llmInvoked ? "App planner requested" : "No app planner request"}</span><div class="trace-hero-actions"><button class="secondary-btn compact" id="viewTraceSessionButton">View full session</button><button class="ghost-btn compact" id="copyTraceIdButton">Copy trace ID</button></div></div></div>`;
  $("viewTraceSessionButton").onclick = () => openTraceThread(trace);
  $("copyTraceIdButton").onclick = async () => { await navigator.clipboard.writeText(traceId); showToast("Trace ID copied"); };
  $("agentPlanMode").textContent = plan.deterministic === false ? "Model-assisted" : "Deterministic";
  const entities = Object.entries(plan.entities || {});
  $("agentPlan").innerHTML = [
    `<div><strong>Intent</strong><span>${escapeHtml(String(plan.intent || "not classified").replaceAll("_", " "))}</span></div>`,
    `<div><strong>Planned tools</strong><span>${escapeHtml((plan.tools || []).join(" → ") || "No tool required")}</span></div>`,
    ...entities.map(([key, value]) => `<div><strong>${escapeHtml(key)}</strong><span>${escapeHtml(Array.isArray(value) ? value.join(", ") : value)}</span></div>`),
  ].join("");
  $("agentToolCount").textContent = `${spans.length} call${spans.length === 1 ? "" : "s"}`;
  $("agentTools").innerHTML = spans.length ? spans.map((span, index) => {
    const source = span.executionSource === "agent_catalog_mcp" ? "Agent Catalog → MCP" : "Application SDK";
    return `<button class="tool-span tool-span-button" data-span-index="${index}"><span>${index + 1}</span><div><strong>${escapeHtml(span.name)}</strong><small>${escapeHtml(source)} · ${escapeHtml((span.components || []).join(" + ") || "Application tool")}</small></div><div><em class="${span.status === "completed" ? "ok" : "bad"}">${escapeHtml(span.status)}</em><small>${escapeHtml(span.durationMs ?? "?")} ms · inspect</small></div></button>`;
  }).join("") : `<div class="empty-row">No tool calls were required for this response.</div>`;
  document.querySelectorAll("#agentTools [data-span-index]").forEach((button) => button.onclick = () => openTraceThread(trace, Number(button.dataset.spanIndex)));
  $("agentPolicy").innerHTML = Object.entries(policy).map(([key, value]) => `<div><strong>${escapeHtml(key)}</strong><span>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)}</span></div>`).join("");
  const entitlementSpan = [...spans].reverse().find((span) => span.name === "check_entitlement");
  const entitlement = entitlementSpan?.output || state.entitlement || null;
  $("entitlementSummary").innerHTML = entitlement ? `<strong>${entitlement.allowed === false ? "Denied" : entitlement.includedInPlan ? "Included" : "Catalogue access"}</strong><span>${escapeHtml(entitlement.reason || `${entitlement.subscriptionTier || "standard"} · ${entitlement.region || "GB"}`)}</span>` : `<span>No title entitlement check was required.</span>`;
}

function renderAgentCatalog(catalog, entitlement) {
  if (catalog) state.agentCatalog = catalog;
  if (entitlement) state.entitlement = entitlement;
  const data = state.agentCatalog || {};
  const native = data.agentCatalog || {};
  const mcp = data.mcpServer || {};
  const tracer = data.agentTracer || {};
  $("agentCatalogMode").textContent = native.healthy && mcp.healthy ? "Native · connected" : "Degraded fallback";
  $("agentRuntimeStatus").innerHTML = [
    `<div class="runtime-chip ${native.healthy ? "ready" : "failed"}"><strong>Agent Catalog</strong><span>${escapeHtml(native.catalogId || native.error || "not loaded")}</span></div>`,
    `<div class="runtime-chip ${mcp.healthy ? "ready" : "failed"}"><strong>MCP Server</strong><span>${escapeHtml(mcp.healthy ? `${mcp.toolCount || 0} tools · ${mcp.transport || "HTTP"}` : (mcp.error || "not connected"))}</span></div>`,
    `<div class="runtime-chip ${tracer.healthy ? "ready" : "failed"}"><strong>Agent Tracer</strong><span>${escapeHtml(tracer.healthy ? "native spans enabled" : "not active")}</span></div>`,
  ].join("");
  const nativeTools = native.tools || [];
  const tools = nativeTools.length ? nativeTools : (data.tools || []);
  $("agentCatalogList").innerHTML = tools.length ? tools.map((tool) => `<div><strong>${escapeHtml(tool.name)}</strong><span>${escapeHtml(tool.source || tool.category || "tool")}${tool.transport ? ` · ${escapeHtml(tool.transport)}` : tool.mutating ? " · validated write" : ""}</span></div>`).join("") : `<div class="empty-row">Published tools become available after Agent Catalog startup.</div>`;
  if (state.latestAgentTrace) renderAgentInspector(state.latestAgentTrace);
}

async function loadAgentInspector(scroll = false) {
  if (!isShowcaseExperience()) return;
  if (!state.viewer) return showAuth();
  try {
    const [catalogPayload, tracesPayload, entitlementPayload] = await Promise.all([
      api("/api/agent/catalog"), api("/api/agent/traces?limit=20"), api("/api/entitlement")
    ]);
    renderAgentCatalog(catalogPayload.data, entitlementPayload.data);
    state.agentTraces = tracesPayload.data?.traces || [];
    if (state.agentTraces.length) renderAgentInspector(state.agentTraces[0]);
    if (scroll) $("agentInspectorSection").scrollIntoView({ behavior: "smooth" });
  } catch (error) { showToast(`Agent inspector unavailable: ${error.message}`); }
}

async function runMcpProof() {
  if (!state.viewer) return showAuth();
  const button = $("mcpProofButton");
  button.disabled = true; button.textContent = "Calling MCP…";
  try {
    const payload = await api("/api/agent/mcp/schema", { method: "POST" });
    const trace = payload.data?.trace;
    if (trace) renderAgentInspector(trace);
    const count = payload.data?.schema?.count ?? "?";
    showToast(`Agent Catalog → MCP proof complete: ${count} keyspaces returned.`);
  } catch (error) { showToast(`MCP proof failed: ${error.message}`); }
  finally { button.disabled = false; button.textContent = "Run MCP proof"; }
}

async function runAIFunctionsProof() {
  if (!state.viewer) return showAuth();
  const titleId = state.selectedTitle?.id || state.latestAgentTrace?.groundedTitleIds?.[0] || state.home?.hero?.id;
  if (!titleId) return showToast("Open a title or ask for a recommendation first.");
  try {
    const payload = await api(`/api/catalogue/title/${encodeURIComponent(titleId)}`);
    openDetail(payload.data);
    $("detailAIGuide").scrollIntoView({behavior:"smooth", block:"center"});
  } catch (error) { showToast(`Viewing guide unavailable: ${error.message}`); }
}

const guideRequests = new Set();

function renderViewingGuide(data, {busy=false, error="", action=""} = {}) {
  const panel = $("detailAIGuide"), guide = data?.guide;
  const titleId = data?.titleId || state.selectedTitle?.id;
  const enabled = data?.enabled ?? state.status?.services?.ai_functions?.enabled === true;
  const when = guide?.generatedAt ? new Date(guide.generatedAt * 1000).toLocaleString("en-GB") : "";
  const generationId = guide?.executionId || "";
  panel.innerHTML = `<div class="ai-guide-heading"><div><span class="eyebrow">Capella AI Functions</span><h3>AI viewing guide</h3></div><span class="ai-guide-state">${busy ? "Generating…" : guide ? "Saved in Capella" : data?.stale ? "Synopsis changed" : "Not generated yet"}</span></div>
    ${guide ? `<p class="ai-guide-summary">${escapeHtml(guide.summary)}</p><div class="ai-guide-insights"><div><strong>Mood classification</strong><p>${escapeHtml(guide.classification)}</p></div><div><strong>Synopsis tone</strong><p>${escapeHtml(guide.sentiment)}</p></div></div><p class="ai-guide-note">AI-generated interpretation of the synopsis. Age ratings, parental controls and availability remain authoritative.</p><p class="ai-guide-provenance">Generated ${escapeHtml(when)} · ${formatNumber(guide.durationMs)} ms · 3 AI Functions</p><details class="ai-guide-evidence" data-showcase-only><summary>Saved execution details</summary><p>Execution: <code>${escapeHtml(generationId)}</code></p><p>Query request: <code>${escapeHtml(guide.queryRequestId || "Not returned")}</code></p><p>Stored on the catalogue title as <code>aiEnrichment</code>. Generated by ${escapeHtml(guide.source || "Capella SQL++ AI Functions")}.</p><p>Model: ${escapeHtml(guide.model || "Configured in Capella")}. Token usage is not returned by this integration.</p></details>`
    : `<p>${data?.stale ? "The saved guide was based on an older synopsis. Generate an updated guide before reusing it." : "Turn this title’s synopsis into a short summary, mood classification and tone assessment. Save it once, then reuse it in the assistant."}</p>`}
    <p class="ai-guide-action-status" role="status">${escapeHtml(error || (busy ? "Reading the catalogue and running classification, summary and sentiment in Capella…" : action || (guide ? "Loaded from the saved catalogue document. No guide regeneration." : enabled ? "Ready to generate and save a viewing guide." : "AI Functions generation is disabled for this deployment.")))}</p>
    <div class="ai-guide-actions"><button class="primary-btn compact" id="generateViewingGuide" ${busy || !enabled ? "disabled" : ""}>${guide ? "Regenerate guide" : "Generate viewing guide"}</button>${guide ? `<button class="secondary-btn compact" id="reuseViewingGuide" ${busy ? "disabled" : ""}>Reuse saved guide</button><button class="secondary-btn compact" id="askViewingGuide" ${busy ? "disabled" : ""}>Ask the assistant about this title</button>` : ""}</div>`;
  $("generateViewingGuide").onclick = () => generateViewingGuide(titleId, Boolean(guide), data);
  if ($("reuseViewingGuide")) $("reuseViewingGuide").onclick = () => generateViewingGuide(titleId, false, data);
  if ($("askViewingGuide")) $("askViewingGuide").onclick = () => {
    $("detailDialog").close(); openAssistant(); sendChat(`Tell me about ${data.title || state.selectedTitle.title}`);
  };
}

async function loadViewingGuide(item) {
  const initial = {titleId:item.id, title:item.title, enabled:state.status?.services?.ai_functions?.enabled};
  $("detailAIGuide").innerHTML = '<p class="ai-guide-note">Loading saved viewing guide…</p>';
  try {
    const payload = await api(`/api/ai-functions/title/${encodeURIComponent(item.id)}`);
    if (state.selectedTitle?.id !== item.id) return;
    renderViewingGuide(payload.data, {busy:guideRequests.has(item.id)});
  } catch (error) {
    if (state.selectedTitle?.id === item.id) renderViewingGuide(initial, {error:`Could not load the saved guide: ${error.message}`});
  }
}

async function generateViewingGuide(titleId, refresh, previous) {
  if (guideRequests.has(titleId)) return;
  guideRequests.add(titleId);
  renderViewingGuide(previous, {busy:true});
  try {
    const payload = await api(`/api/ai-functions/enrich/${encodeURIComponent(titleId)}${refresh ? "?refresh=true" : ""}`, {method:"POST"});
    const data = {titleId, title:payload.data.result.title, guide:payload.data.guide, enabled:previous.enabled, originalOverview:payload.data.originalOverview};
    if (state.selectedTitle?.id === titleId) {
      state.selectedTitle.aiEnrichment = data.guide;
      renderViewingGuide(data, {action:payload.data.cacheHit ? "Reused the saved guide. No new AI Functions query or guide-generation tokens." : "Generated by 3 Capella AI Functions and saved to the catalogue. The assistant can now reuse this guide."});
    }
    if (isShowcaseExperience()) { loadMetrics(); refreshStatus(); }
  } catch (error) {
    if (state.selectedTitle?.id === titleId) renderViewingGuide(previous, {error:`Guide generation failed: ${error.message}. You can retry.`});
  } finally { guideRequests.delete(titleId); }
}

async function runDataProcessingProof() {
  const button = $("dataProcessingProofButton"); button.disabled = true; button.textContent = "Checking vectors…";
  try {
    const payload = await api("/api/data-processing/status");
    const data = payload.data || {};
    const catalogue = data.catalogue || {};
    showToast(`${formatNumber(catalogue.embedded || 0)} of ${formatNumber(catalogue.documents || 0)} catalogue documents are vectorised (${catalogue.dimension || 0} dimensions).`);
  } catch (error) { showToast(`Vector workflow status failed: ${error.message}`); }
  finally { button.disabled = false; button.textContent = "Vector workflow status"; }
}

async function sendChat(message) {
  if (state.chatBusy || !message.trim()) return;
  if (!state.viewer) return showAuth();
  state.chatBusy = true;
  addMessage("user", message.trim());
  const assistant = addMessage("assistant", ""); assistant.classList.add("typing-cursor");
  $("chatInput").value = ""; $("assistantRecommendations").hidden = true;
  let firstTokenMs = null, responseMs = null, retrievalTrace = null, catalogue = [], agentTrace = null;
  let historyChanged = false, profileChanged = false, responseMode = "";
  try {
    const response = await fetch("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: message.trim(), capture_long_term: $("captureFacts").checked, search_mode: $("chatSearchMode").value }) });
    if (!response.ok || !response.body) throw new Error(await response.text());
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true }); const lines = buffer.split("\n"); buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "context") {
          catalogue = event.catalogue || []; retrievalTrace = event.retrieval_trace || null; responseMode = event.response_mode || "";
          historyChanged ||= Boolean(event.history_changed); profileChanged ||= Boolean(event.profile_changed);
          agentTrace = event.agent_trace || agentTrace;
        } else if (event.type === "token") {
          assistant.textContent = consumerSafeText(assistant.textContent + (event.content || "")); $("chatLog").scrollTop = $("chatLog").scrollHeight;
        } else if (event.type === "response_complete") {
          firstTokenMs = event.first_token_ms; responseMs = event.response_ms; state.viewer = event.viewer || state.viewer;
          catalogue = event.catalogue || catalogue; retrievalTrace = event.retrieval_trace || retrievalTrace; responseMode = event.response_mode || responseMode;
          historyChanged ||= Boolean(event.history_changed); profileChanged ||= Boolean(event.profile_changed);
          agentTrace = event.agent_trace || agentTrace;
        } else if (event.type === "done") {
          historyChanged ||= Boolean(event.history_changed); profileChanged ||= Boolean(event.profile_changed);
          assistant.classList.remove("typing-cursor");
          const meta = document.createElement("span"); meta.className = "message-meta";
          const route = event.llm_invoked ? "App planner requested" : "No app planner request";
          const planner = event.planner || {};
          const plannerLabel = planner.source
            ? ` · plan ${String(planner.source).replaceAll("_", " ")}${planner.similarity != null ? ` similarity ${Number(planner.similarity).toFixed(4)}` : ""}`
            : "";
          const memoryStatus = event.persistence_status === "queued"
            ? "memory queued in background"
            : `${event.persistence_ms ?? "?"} ms memory acceptance`;
          const traceLabel = event.agent_trace_id ? ` · trace ${String(event.agent_trace_id).split("::").at(-1)}` : "";
          meta.textContent = `${responseMode || "grounded"} · ${route}${plannerLabel} · ${firstTokenMs ?? "?"} ms first response · ${responseMs ?? "?"} ms server answer · ${memoryStatus}${traceLabel}`;
          if (isShowcaseExperience()) assistant.appendChild(meta);
          if (event.metrics && isShowcaseExperience()) renderMetrics(event.metrics);
          else if (isShowcaseExperience()) {
            setTimeout(loadMetrics, 900);
            setTimeout(loadMetrics, 7000);
          }
        } else if (event.type === "error") throw new Error(event.detail || "Chat failed");
      }
    }
    if (catalogue.length) renderAssistantRecommendations(catalogue, retrievalTrace);
    if (agentTrace && isShowcaseExperience()) renderAgentInspector(agentTrace);
    if (historyChanged || profileChanged) refreshRows(["continue", "recently-watched", "my-list", "top-picks", "because", "trending"]);
    if (profileChanged && $("memoryDialog").open) openMemory();
  } catch (error) {
    assistant.classList.remove("typing-cursor"); assistant.textContent = `I could not complete that turn: ${error.message}`;
  } finally { state.chatBusy = false; }
}

function renderAssistantRecommendations(items, trace) {
  const carousel = $("assistantCarousel"); carousel.innerHTML = "";
  items.slice(0, 8).forEach((item) => carousel.appendChild(renderCard(item, true)));
  $("assistantSearchMode").textContent = `${String(trace?.mode || "grounded").toUpperCase()} · ${trace?.elapsedMs || "?"} ms`;
  $("assistantRecommendations").hidden = false;
}

function openDetail(item) {
  state.selectedTitle = item;
  $("detailTitle").textContent = item.title || ""; $("detailOverview").textContent = item.overview || "No synopsis available."; $("detailType").textContent = contentTypeLabel(item);
  $("detailMeta").innerHTML = [item.releaseYear, formatRuntime(item), item.voteAverage ? `★ ${Number(item.voteAverage).toFixed(1)}` : "", item.genres?.join(" · ")].filter(Boolean).map((x) => `<span>${escapeHtml(x)}</span>`).join("");
  $("detailPoster").src = posterUrl(item) || ""; $("detailPoster").alt = item.title || "";
  const bg = backdropUrl(item); $("detailBackdrop").style.backgroundImage = bg ? `linear-gradient(0deg, #111722, transparent), url("${bg}")` : "linear-gradient(145deg, #27384e, #111722)";
  const facts = [];
  if (item.keywords?.length) facts.push(`<strong>Keywords</strong><span>${escapeHtml(item.keywords.slice(0, 12).join(", "))}</span>`);
  if (item.castNames?.length) facts.push(`<strong>Cast</strong><span>${escapeHtml(item.castNames.slice(0, 6).join(", "))}</span>`);
  if (item.directorNames?.length) facts.push(`<strong>Creators</strong><span>${escapeHtml(item.directorNames.join(", "))}</span>`);
  if (item.originCountries?.length) facts.push(`<strong>Origin</strong><span>${escapeHtml(item.originCountries.join(", "))}</span>`);
  if (item.spokenLanguages?.length || item.originalLanguage) facts.push(`<strong>Language</strong><span>${escapeHtml((item.spokenLanguages || [item.originalLanguage]).join(", "))}</span>`);
  if (item.entitlement) facts.push(`<strong>Viewer access</strong><span>${escapeHtml(item.entitlement.reason || item.entitlement.decision)}</span>`);
  $("detailFacts").innerHTML = facts.map((x) => `<div>${x}</div>`).join("");
  $("detailReason").textContent = titleReason(item);
  const matched = item.matchExplanation?.matchedFields || [];
  const fieldLabels = { semanticQuery: "Semantic query", contentType: "Content type", originCountries: "Origin", originalLanguage: "Language" };
  const matchLabels = { exact: "exact", lexical_highlight: "FTS lexical", lexical_term: "FTS term", expanded_lexical_term: "FTS query expansion", vector_similarity: "Vector semantic", ranking: "ranking" };
  $("detailMatchFields").innerHTML = matched.map((m) => `<span><strong>${escapeHtml(fieldLabels[m.field] || m.field)}</strong>: ${escapeHtml(String(m.value || "").replaceAll(/<[^>]+>/g, ""))} <em>${escapeHtml(matchLabels[m.matchType] || m.matchType || "")}</em></span>`).join("");
  $("detailEvidenceScorecard").hidden = true; $("detailEvidenceScorecard").innerHTML = "";
  $("detailEvidenceButton").onclick = () => loadEvidenceScorecard(item);
  $("detailPlay").onclick = () => recordInteraction(item, "play", 5); $("detailLike").onclick = () => recordInteraction(item, "like"); $("detailDislike").onclick = () => recordInteraction(item, "dislike"); $("detailWatchlist").onclick = () => recordInteraction(item, "watchlist");
  if (!$("detailDialog").open) $("detailDialog").showModal();
  loadViewingGuide(item);
}

function removeTitleOptimistically(titleId, action) {
  const removeFrom = action === "dislike" ? [...state.rowMap.keys()] : ["top-picks"];
  removeFrom.forEach((rowId) => {
    const row = state.rowMap.get(rowId); if (!row) return;
    row.items = (row.items || []).filter((item) => item.id !== titleId); renderRow(row);
  });
  if (state.home.hero?.id === titleId && ["like", "dislike", "play", "complete"].includes(action)) renderHero(null);
}

async function recordInteraction(item, action, progressPct = null) {
  if (!state.viewer) return showAuth();
  removeTitleOptimistically(item.id, action);
  if ($("detailDialog").open) $("detailDialog").close();
  try {
    const payload = await api("/api/interactions", { method: "POST", body: JSON.stringify({ title_id: item.id, action, progress_pct: progressPct }) });
    const labels = { like: "Added to liked titles", dislike: "Disliked and suppressed from recommendations", play: "Added to Continue Watching", complete: "Added to watch history", watchlist: "Added to My List" };
    showToast(labels[action] || "Viewer signal saved");
    const rows = action === "dislike" ? ["continue", "recently-watched", "my-list", "top-picks", "because", "trending"] : action === "like" ? ["top-picks", "because"] : action === "watchlist" ? ["my-list", "top-picks", "because"] : ["continue", "recently-watched", "my-list", "top-picks", "because"];
    refreshRows(rows);
    if ($("profileDialog").open) openProfile();
    if (payload.metrics) renderMetrics(payload.metrics);
    if (payload.memoryError) console.warn("Interaction stored but Agent Memory sync failed", payload.memoryError);
  } catch (error) { showToast(error.message); await loadHomeProgressively(); }
}

async function openMemory() {
  if (!isShowcaseExperience()) return;
  try {
    const payload = await api("/api/memories"); state.memories = payload.data; renderMemory("long_term");
    if (!$("memoryDialog").open) $("memoryDialog").showModal();
  } catch (error) { showToast(error.message); }
}

function renderMemory(tab, blocksOverride = null) {
  document.querySelectorAll("[data-showcase-tab]").forEach((button) => button.onclick = () => setShowcaseTab(button.dataset.showcaseTab));
  $("showcaseRefresh").onclick = openShowcaseStudio; $("showcaseReset").onclick = resetShowcaseDemo; $("clearFaults").onclick = clearFaults; $("runEvaluation").onclick = runEvaluation; $("loadSupportingDocs").onclick = loadSupportingDocuments; $("loadProfileTimeline").onclick = loadProfileTimeline;
  document.querySelectorAll("[data-memory-tab]").forEach((b) => b.classList.toggle("active", b.dataset.memoryTab === tab));
  $("memorySearchForm").hidden = tab !== "search";
  let blocks = blocksOverride || (tab === "short_term" ? state.memories?.short_term : tab === "long_term" ? state.memories?.long_term : []);
  const content = $("memoryContent"); content.innerHTML = "";
  if (!blocks?.length) { content.innerHTML = `<div class="memory-block"><strong>No memory or profile facts to display</strong><p>Say “I like animal movies”, edit My Profile, or Like/Dislike a title. The inspector refreshes from Agent Memory and the structured viewer profile.</p></div>`; return; }
  blocks.forEach((block) => {
    const div = document.createElement("div"); div.className = "memory-block";
    const text = block.fact || block.summary || block.user_content || "Memory block";
    const storage = block.block_id ? "Agent Memory block" : block.annotations?.source === "structured_viewer_profile" ? "Operational profile only — not an Agent Memory block" : "Local working context — persistence not confirmed";
    div.innerHTML = `<strong>${escapeHtml(storage)}</strong><p>${escapeHtml(text)}</p><small>scope: ${escapeHtml(block.annotations?.memory_scope || "unknown")} · source: ${escapeHtml(block.annotations?.source || "unknown")} · status: ${escapeHtml(block.status || "n/a")}${block.block_id ? ` · block: ${escapeHtml(block.block_id)}` : ""}</small>`;
    content.appendChild(div);
  });
}

async function runMemorySearch(event) {
  event.preventDefault(); const query = $("memorySearchInput").value.trim(); if (!query) return;
  try { const payload = await api("/api/memories/search", { method: "POST", body: JSON.stringify({ query, scope: $("memorySearchScope").value }) }); renderMemory("search", payload.data.blocks || []); if (payload.metrics) renderMetrics(payload.metrics); }
  catch (error) { showToast(error.message); }
}

function splitCsv(value) { return String(value || "").split(",").map((x) => x.trim()).filter(Boolean); }
function joinCsv(values) { return (values || []).join(", "); }

function renderProfileTitleGrid(id, items, emptyText) {
  const grid = $(id); grid.innerHTML = "";
  if (!items?.length) { grid.innerHTML = `<div class="empty-profile-list">${escapeHtml(emptyText)}</div>`; return; }
  items.slice(0, 30).forEach((item) => grid.appendChild(renderCard(item, true)));
}

async function openProfile() {
  if (!state.viewer) return showAuth();
  try {
    const payload = await api("/api/profile"); state.profile = payload.data;
    const p = payload.data.profile || {};
    renderProfileMemorySync(payload.data.memorySync);
    $("profilePreferredGenres").value = joinCsv(p.preferredGenres); $("profileDislikedGenres").value = joinCsv(p.dislikedGenres);
    $("profilePreferredThemes").value = joinCsv(p.preferredThemes); $("profileDislikedThemes").value = joinCsv(p.dislikedThemes);
    $("profilePreferredPeople").value = joinCsv(p.preferredPeople); $("profileDislikedPeople").value = joinCsv(p.dislikedPeople);
    $("profileLanguages").value = joinCsv(p.preferredLanguages); $("profileRuntime").value = p.maxRuntimeMinutes || ""; $("profileAvoidViolence").checked = Boolean(p.avoidGraphicViolence); $("profileAvoidAdultContent").checked = Boolean(p.avoidAdultContent);
    const s = payload.data.summary || {}; $("profileStats").innerHTML = [["Liked", s.liked], ["Disliked", s.disliked], ["Watched", s.watched], ["My List", s.watchlist]].map(([label, value]) => `<div><strong>${escapeHtml(value || 0)}</strong><span>${escapeHtml(label)}</span></div>`).join("");
    renderProfileTitleGrid("profileLikedTitles", payload.data.likedTitles, "No liked titles yet");
    renderProfileTitleGrid("profileDislikedTitles", payload.data.dislikedTitles, "No disliked titles yet");
    renderProfileTitleGrid("profileWatchlistTitles", payload.data.watchlistTitles, "No titles in My List yet");
    renderProfileTitleGrid("profileHistory", payload.data.watchHistory, "No watch history yet");
    if (!$("profileDialog").open) $("profileDialog").showModal();
  } catch (error) { showToast(error.message); }
}

function renderProfileMemorySync(sync = {}) {
  const labels = {ready:"Preferences ready in Agent Memory",pending:"Preferences accepted; enrichment pending",empty:"No explicit preferences to synchronise",not_synced:"Preferences not yet in Agent Memory",out_of_sync:"Profile and Agent Memory need synchronisation",enrichment_failed:"Memory enrichment failed",error:"Agent Memory sync needs attention"};
  $("profileMemorySyncStatus").textContent = labels[sync.status] || "Agent Memory status unavailable";
  $("profileMemorySyncDetails").textContent = sync.status === "error" ? (sync.message || "Operational preferences are saved. Retry synchronisation.") : `${sync.storedFacts ?? 0} of ${sync.expectedFacts ?? 0} preference facts verified stored; ${sync.readyFacts ?? 0} ready for recall. ${sync.addedFacts ? sync.addedFacts + " new facts accepted in this request. " : ""}Background model work is included in AI Usage.`;
  $("profileMemorySyncSession").textContent = sync.sessionId ? `Agent Memory session: ${sync.sessionId}` : "";
}

async function syncProfileMemory() {
  try {
    const payload = await api("/api/profile/memory-sync", {method:"POST"});
    renderProfileMemorySync(payload.data);
    showToast(payload.data.status === "error" ? "Preferences remain saved; memory sync needs attention" : "Profile memory sync checked");
    loadMetrics();
  } catch (error) { showToast(error.message); }
}

async function saveProfile(event) {
  event.preventDefault();
  try {
    const payload = {
      preferred_genres: splitCsv($("profilePreferredGenres").value), disliked_genres: splitCsv($("profileDislikedGenres").value),
      preferred_themes: splitCsv($("profilePreferredThemes").value), disliked_themes: splitCsv($("profileDislikedThemes").value),
      preferred_people: splitCsv($("profilePreferredPeople").value), disliked_people: splitCsv($("profileDislikedPeople").value),
      preferred_languages: splitCsv($("profileLanguages").value), preferred_content_types: [],
      max_runtime_minutes: $("profileRuntime").value ? Number($("profileRuntime").value) : null, avoid_graphic_violence: $("profileAvoidViolence").checked,
      avoid_adult_content: $("profileAvoidAdultContent").checked,
    };
    const result = await api("/api/profile/preferences", { method: "PUT", body: JSON.stringify(payload) });
    showToast(isShowcaseExperience() ? "Viewer preferences saved and recommendation cache invalidated" : "Preferences saved");
    if (result.metrics) renderMetrics(result.metrics);
    await Promise.all([openProfile(), refreshRows(["top-picks", "because"])]);
    if (result.memoryError) showToast("Profile saved; Agent Memory sync needs attention. See its status above.");
  } catch (error) { showToast(error.message); }
}

function showAuth() { if (!$("authDialog").open) $("authDialog").showModal(); }
function setAuthTab(tab) { document.querySelectorAll("[data-auth-tab]").forEach((b) => b.classList.toggle("active", b.dataset.authTab === tab)); $("loginForm").hidden = tab !== "login"; $("registerForm").hidden = tab !== "register"; $("authError").hidden = true; }

async function handleLogin(event) {
  event.preventDefault();
  if (!state.ready) return showToast(isShowcaseExperience() ? "Local services are still starting" : "StreamAI is still starting");
  const button = $("loginSubmit"); button.disabled = true; button.textContent = "Signing in…";
  try {
    const payload = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ login_id: $("loginId").value, pin: $("loginPin").value }) });
    setViewer(payload.data); $("authDialog").close(); loadHomeProgressively();
    if (isShowcaseExperience()) { loadMetrics(); refreshStatus(); }
  } catch (error) { $("authError").textContent = error.message; $("authError").hidden = false; }
  finally { button.disabled = !state.ready; button.textContent = "Continue viewing"; }
}

async function handleRegister(event) {
  event.preventDefault();
  if (!state.ready) return showToast(isShowcaseExperience() ? "Local services are still starting" : "StreamAI is still starting");
  const button = $("registerSubmit"); button.disabled = true; button.textContent = "Creating…";
  try {
    const payload = await api("/api/auth/register", { method: "POST", body: JSON.stringify({ name: $("registerName").value, login_id: $("registerId").value, pin: $("registerPin").value }) });
    setViewer(payload.data); $("authDialog").close(); loadHomeProgressively();
    if (isShowcaseExperience()) { loadMetrics(); refreshStatus(); }
  } catch (error) { $("authError").textContent = error.message; $("authError").hidden = false; }
  finally { button.disabled = !state.ready; button.textContent = "Create viewer"; }
}

async function newSession() {
  try { const payload = await api("/api/session/new", { method: "POST", body: JSON.stringify({ label: "New discovery session" }) }); setViewer(payload.data); showToast(isShowcaseExperience() ? "New Agent Memory session started" : "New viewing session started"); }
  catch (error) { showToast(error.message); }
}

async function pollReadiness() {
  for (let attempt = 0; attempt < 120; attempt++) {
    try {
      const payload = await api("/api/readiness");
      if (payload.ready) {
        state.ready = true; $("readinessBanner").classList.add("ready"); $("readinessText").textContent = isShowcaseExperience() ? `Local services ready in ${(payload.elapsedMs / 1000).toFixed(1)}s` : "Ready to stream";
        $("loginSubmit").disabled = false; $("registerSubmit").disabled = false; $("serviceStatusText").textContent = "Services ready";
        if (isShowcaseExperience()) refreshStatus();
        return;
      }
      if (payload.state === "failed") throw new Error(payload.error || "Startup failed");
      $("readinessText").textContent = isShowcaseExperience() ? `Connecting to local services… ${(payload.elapsedMs / 1000).toFixed(1)}s` : "Preparing your viewing experience…";
    } catch (error) { $("readinessBanner").classList.add("failed"); $("readinessText").textContent = error.message; return; }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  $("readinessText").textContent = "Services are taking longer than expected. Run scripts/09-diagnose.sh.";
}


async function loadEvidenceScorecard(item = state.selectedTitle) {
  if (!isShowcaseExperience()) return;
  if (!item?.id) return;
  const target = $("detailEvidenceScorecard");
  target.hidden = false;
  target.innerHTML = `<div class="empty-row">Calculating deterministic evidence…</div>`;
  try {
    const payload = await api("/api/showcase/evidence", { method: "POST", body: JSON.stringify({ title_id: item.id, item }) });
    const data = payload.data || {};
    const components = data.components || [];
    target.innerHTML = `<div class="evidence-total"><strong>${escapeHtml(data.score || 0)}</strong><span>heuristic demo points</span><small>${escapeHtml(data.scoreMeaning || "")}</small></div>
      <div class="evidence-components">${components.map((c) => `<div><span>${escapeHtml(c.name)}</span><strong>+${escapeHtml(c.score)}</strong><i style="width:${Math.min(100, Number(c.score || 0) * 4)}%"></i></div>`).join("")}</div>
      <div class="evidence-policy ${data.eligible ? "pass" : "fail"}"><strong>${data.eligible ? "PASS" : "EXCLUDED"}</strong><span>${escapeHtml(data.entitlement?.reason || "No entitlement result")}</span></div>
      ${(data.violations || []).length ? `<div class="evidence-violations">${data.violations.map((x) => `<span>− ${escapeHtml(x)}</span>`).join("")}</div>` : ""}`;
    requestAnimationFrame(() => target.scrollIntoView({ behavior: "smooth", block: "nearest" }));
  } catch (error) { target.innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`; target.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
}

function setShowcaseTab(tab) {
  state.showcaseTab = tab;
  document.querySelectorAll("[data-showcase-tab]").forEach((button) => button.classList.toggle("active", button.dataset.showcaseTab === tab));
  document.querySelectorAll("[data-showcase-pane]").forEach((pane) => { pane.hidden = pane.dataset.showcasePane !== tab; });
  if (tab === "governance") loadGovernanceStudio();
  if (tab === "capella") loadCapellaShowcase();
  if (tab === "resilience") renderResilienceControls();
  if (tab === "policy") {
    if (state.selectedTitle?.id && !$("policyTitleId").value.trim()) $("policyTitleId").value = state.selectedTitle.id;
    $("policyInlineStatus").textContent = state.selectedTitle?.title ? `Selected: ${state.selectedTitle.title}` : "Select any title card to prefill this field.";
  }
}

async function openShowcaseStudio() {
  if (!isShowcaseExperience() || state.uiConfig.showcaseEnabled === false) return;
  if (!state.viewer) return showAuth();
  try {
    const payload = await api("/api/showcase");
    state.showcase = payload.data;
    renderPresenterStudio();
    renderResilienceControls();
    if (!$("showcaseDialog").open) $("showcaseDialog").showModal();
    setShowcaseTab(state.showcaseTab || "presenter");
  } catch (error) { showToast(error.message); }
}

function renderPresenterStudio() {
  const data = state.showcase || {};
  $("showcasePersonas").innerHTML = (data.personas || []).map((persona) => `<button class="persona-chip" data-persona="${escapeHtml(persona.id)}" title="${escapeHtml(persona.description)}">${escapeHtml(persona.name)}</button>`).join("");
  document.querySelectorAll("[data-persona]").forEach((button) => button.onclick = () => seedPersona(button.dataset.persona));
  const steps = data.steps || [];
  $("demoStepList").innerHTML = steps.map((step, index) => `<button class="demo-step" data-demo-step="${escapeHtml(step.id)}"><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${escapeHtml(step.title)}</strong><small>${escapeHtml((step.components || []).join(" · "))}</small></div></button>`).join("");
  document.querySelectorAll("[data-demo-step]").forEach((button) => button.onclick = () => renderDemoStep(steps.find((step) => step.id === button.dataset.demoStep)));
  if (steps.length) renderDemoStep(steps[0]);
}

function renderDemoStep(step) {
  if (!step) return;
  document.querySelectorAll("[data-demo-step]").forEach((button) => button.classList.toggle("active", button.dataset.demoStep === step.id));
  const actionLabel = step.action === "search_lab" ? "Run comparison" : "Run in assistant";
  const prompt = step.prompt ? `<div class="demo-prompt"><code>${escapeHtml(step.prompt)}</code><button class="primary-btn compact" id="runDemoPrompt">${actionLabel}</button><button class="ghost-btn compact" id="copyDemoPrompt">Copy</button></div>` : `<div class="demo-prompt muted">This step is demonstrated through the UI rather than a chat prompt.</div>`;
  $("demoStepDetail").innerHTML = `<span class="eyebrow">Presenter step</span><h3>${escapeHtml(step.title)}</h3><p><strong>Expected:</strong> ${escapeHtml(step.expected)}</p>${prompt}<div class="component-chips">${(step.components || []).map((x) => `<span>${escapeHtml(x)}</span>`).join("")}</div>`;
  if (step.prompt) {
    $("runDemoPrompt").onclick = () => {
      if (step.action === "search_lab") {
        setShowcaseTab("search");
        $("searchLabQuery").value = step.prompt;
        runSearchLab({ preventDefault() {} });
        return;
      }
      $("showcaseDialog").close(); openAssistant(); $("chatInput").value = step.prompt; sendChat(step.prompt);
    };
    $("copyDemoPrompt").onclick = async () => { await navigator.clipboard.writeText(step.prompt); showToast("Demo prompt copied"); };
  }
}

async function seedPersona(personaId) {
  try {
    await api("/api/showcase/persona", { method: "POST", body: JSON.stringify({ persona_id: personaId }) });
    showToast(`${personaId} persona seeded`);
    await Promise.all([loadHomeProgressively(), loadMetrics()]);
  } catch (error) { showToast(error.message); }
}

async function resetShowcaseDemo() {
  const persona = window.prompt("Reset to persona: executive, family or guest", "executive") || "executive";
  try {
    await api("/api/showcase/reset", { method: "POST", body: JSON.stringify({ persona_id: persona }) });
    showToast("Demo state restored");
    await Promise.all([loadHomeProgressively(), loadMetrics(), openShowcaseStudio()]);
  } catch (error) { showToast(error.message); }
}

async function runSearchLab(event) {
  event.preventDefault();
  const query = $("searchLabQuery").value.trim(); if (!query) return;
  $("searchLabResults").innerHTML = `<div class="empty-row">Running FTS, Vector and Hybrid against Couchbase…</div>`;
  try {
    const payload = await api("/api/showcase/search-lab", { method: "POST", body: JSON.stringify({ query, content_type: $("searchLabContentType").value || null, limit: 8 }) });
    state.searchLab = payload.data;
    renderSearchLab(payload.data);
  } catch (error) { $("searchLabResults").innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`; }
}

function renderSearchLab(data) {
  const overlap = data.overlap || {};
  $("searchLabSummary").innerHTML = `<article><span>Auto route</span><strong>${escapeHtml(String(data.autoMode || "").toUpperCase())}</strong></article><article><span>FTS ∩ Vector</span><strong>${escapeHtml(overlap.ftsVector || 0)}</strong></article><article><span>FTS ∩ Hybrid</span><strong>${escapeHtml(overlap.ftsHybrid || 0)}</strong></article><article><span>Experiment</span><strong>${escapeHtml(String(data.experimentId || "").slice(0, 8))}</strong></article>`;
  $("searchLabResults").innerHTML = ["fts", "vector", "hybrid"].map((mode) => {
    const result = data.modes?.[mode] || {}; const trace = result.trace || {};
    return `<section class="search-lab-column"><div class="mini-heading"><strong>${mode.toUpperCase()}</strong><span>${escapeHtml(result.wallClockMs || "?")} ms</span></div><div class="search-lab-meta">effective ${escapeHtml(trace.effectiveMode || trace.mode || mode)} · ${escapeHtml(trace.candidateCount ?? "?")} candidates · ${escapeHtml((result.results || []).length)} shown</div>${result.error ? `<div class="lab-error">${escapeHtml(result.error)}</div>` : ""}<div class="lab-cards">${(result.results || []).slice(0, 6).map((item) => `<button class="lab-result" data-lab-title="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.matchPct || "?")}% rank</span><small>${escapeHtml((item.genres || []).slice(0, 3).join(" · "))}</small><em>${escapeHtml(cardEvidenceText(item) || "No evidence summary")}</em></button>`).join("")}</div></section>`;
  }).join("");
  document.querySelectorAll("[data-lab-title]").forEach((button) => {
    const item = Object.values(data.modes || {}).flatMap((x) => x.results || []).find((x) => x.id === button.dataset.labTitle);
    button.onclick = () => item && openDetail(item);
  });
}

async function loadGovernanceStudio() {
  $("governanceStudio").innerHTML = `<div class="empty-row">Loading active prompt and governed tools…</div>`;
  try {
    const payload = await api("/api/showcase/governance"); state.governance = payload.data; renderGovernance(payload.data);
  } catch (error) { $("governanceStudio").innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`; }
}

function renderGovernance(data) {
  const catalog = data.catalog || {}; const tools = catalog.tools || [];
  $("governanceStudio").innerHTML = `<div class="governance-summary"><article><span>Active catalogue</span><strong>${escapeHtml(String(catalog.catalogId || "not loaded").slice(0, 14))}</strong></article><article><span>Prompt</span><strong>${escapeHtml(data.prompt?.name || "")}</strong></article><article><span>Tools</span><strong>${escapeHtml(tools.length)}</strong></article><article><span>Native tracer</span><strong>${catalog.nativeTracerEnabled ? "Enabled" : "Disabled"}</strong></article></div>
    <div class="governance-layout"><section><div class="mini-heading"><strong>Published tools</strong><span>Agent Catalog</span></div>${tools.map((tool) => `<article class="governed-tool"><strong>${escapeHtml(tool.name || "tool")}</strong><p>${escapeHtml(tool.description || "Governed Couchbase tool")}</p><small>${escapeHtml(JSON.stringify(tool.annotations || {}))}</small></article>`).join("")}</section>
    <section><div class="mini-heading"><strong>Active prompt</strong><span>SHA ${escapeHtml(String(data.prompt?.sha256 || "").slice(0, 10))}</span></div><pre>${escapeHtml(data.prompt?.content || "")}</pre></section>
    <section><div class="mini-heading"><strong>Policy diff</strong><span>baseline → active</span></div><pre class="diff-view">${escapeHtml((data.diff || []).join("\n"))}</pre></section></div>`;
}

function renderResilienceControls() {
  const faults = state.showcase?.faults || {};
  const labels = { mcp_unavailable: "MCP unavailable", embedding_timeout: "Embedding timeout", model_unavailable: "Model unavailable", search_unavailable: "Search unavailable", agent_memory_unavailable: "Agent Memory unavailable" };
  $("resilienceControls").innerHTML = Object.entries(labels).map(([key, label]) => `<label class="fault-card"><input type="checkbox" data-fault="${key}" ${faults[key] ? "checked" : ""}/><span><strong>${escapeHtml(label)}</strong><small>Inject this condition into the next applicable route.</small></span></label>`).join("");
  document.querySelectorAll("[data-fault]").forEach((input) => input.onchange = updateFaults);
}

async function updateFaults() {
  const body = {};
  document.querySelectorAll("[data-fault]").forEach((input) => { body[input.dataset.fault] = input.checked; });
  try { const payload = await api("/api/showcase/faults", { method: "PUT", body: JSON.stringify(body) }); state.showcase.faults = payload.data.faults || body; showToast("Resilience simulation updated"); }
  catch (error) { showToast(error.message); }
}

async function clearFaults() {
  document.querySelectorAll("[data-fault]").forEach((input) => { input.checked = false; });
  await updateFaults();
}

async function runEvaluation() {
  $("evaluationResults").innerHTML = `<div class="empty-row">Running evaluation suite…</div>`;
  try { const payload = await api("/api/showcase/evaluate", { method: "POST" }); state.evaluation = payload.data; renderEvaluation(payload.data); }
  catch (error) { $("evaluationResults").innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`; }
}

function renderEvaluation(data) {
  $("evaluationResults").innerHTML = `<div class="evaluation-score"><strong>${escapeHtml(data.passRatePct)}%</strong><span>${escapeHtml(data.passed)} of ${escapeHtml(data.total)} checks passed</span><small>${escapeHtml(data.scope || "Rule and availability checks; not an AI quality benchmark.")} Run ${escapeHtml(String(data.runId || "").slice(0, 10))}</small></div><div class="evaluation-cases">${(data.cases || []).map((c) => `<article class="${c.passed ? "pass" : "fail"}"><strong>${c.passed ? "✓" : "×"} ${escapeHtml(c.name)}</strong><p>${escapeHtml(c.detail)}</p></article>`).join("")}</div>`;
}

async function runPolicySimulation(event) {
  event.preventDefault();
  const target = $("policySimulationResult");
  const status = $("policyInlineStatus");
  const button = $("simulateDecisionButton");
  const titleRef = $("policyTitleId").value.trim() || state.selectedTitle?.id || state.selectedTitle?.title;
  if (!titleRef) {
    status.textContent = "A title is required. Select a card or enter a title/catalogue ID.";
    status.className = "policy-inline-status error";
    target.innerHTML = `<div class="policy-error">No title was selected, so no decision was run.</div>`;
    return;
  }
  button.disabled = true; button.textContent = "Simulating…";
  status.textContent = `Resolving ${state.selectedTitle?.title || titleRef} and evaluating the what-if policy…`;
  status.className = "policy-inline-status loading";
  target.innerHTML = `<div class="empty-row">Running baseline and simulated entitlement checks…</div>`;
  try {
    const payload = await api("/api/showcase/entitlement-simulate", { method: "POST", body: JSON.stringify({ title_id: titleRef, region: $("policyRegion").value, tier: $("policyTier").value, parental_rating: $("policyRating").value }) });
    const d = payload.data;
    $("policyTitleId").value = d.title?.id || titleRef;
    status.textContent = `Decision completed for ${d.title?.title || titleRef}. No viewer data was changed.`;
    status.className = "policy-inline-status success";
    target.innerHTML = `<article class="policy-result"><h3>${escapeHtml(d.title?.title || titleRef)}</h3><div><span>Baseline</span><strong>${escapeHtml(d.baseline?.reason || "")}</strong></div><div class="${d.decision?.allowed ? "pass" : "fail"}"><span>What-if</span><strong>${escapeHtml(d.decision?.reason || "")}</strong></div><small>Simulation only — authoritative profile not mutated.</small></article>`;
  } catch (error) {
    status.textContent = "The entitlement simulation failed.";
    status.className = "policy-inline-status error";
    target.innerHTML = `<div class="policy-error"><strong>Simulation failed</strong><span>${escapeHtml(error.message)}</span></div>`;
  } finally {
    button.disabled = false; button.textContent = "Simulate decision";
  }
}

async function loadSupportingDocuments() {
  const titleId = state.selectedTitle?.id || ""; const traceId = state.latestAgentTrace?.traceId || "";
  try {
    const payload = await api(`/api/showcase/documents?title_id=${encodeURIComponent(titleId)}&trace_id=${encodeURIComponent(traceId)}`);
    $("supportingDocuments").innerHTML = (payload.data.documents || []).map((doc) => `<details class="document-card"><summary><strong>${escapeHtml(doc.label)}</strong><span>${escapeHtml(doc.scope)}.${escapeHtml(doc.collection)} · ${escapeHtml(doc.id)}</span></summary><pre>${escapeHtml(prettyJson(doc.document))}</pre></details>`).join("");
  } catch (error) { showToast(error.message); }
}

async function loadProfileTimeline() {
  try {
    const payload = await api("/api/showcase/profile-timeline?limit=30");
    const items = payload.data || [];
    $("profileTimeline").innerHTML = items.length ? items.map((item) => `<article class="timeline-snapshot"><strong>${escapeHtml(item.cause)}</strong><span>version ${escapeHtml(item.recommendationVersion ?? "?")}</span><small>${new Date(Number(item.timestamp || 0) * 1000).toLocaleString()}</small><details><summary>Profile JSON</summary><pre>${escapeHtml(prettyJson(item.profile))}</pre></details></article>`).join("") : `<div class="empty-row">Interact with titles or seed a persona to create profile snapshots.</div>`;
  } catch (error) { showToast(error.message); }
}

async function loadCapellaShowcase() {
  $("capellaShowcase").innerHTML = `<div class="empty-row">Loading Capella v2 profile…</div>`;
  try {
    const payload = await api("/api/showcase/capella"); const d = payload.data || {}; const links = d.deepLinks || {};
    $("capellaShowcase").innerHTML = `<div class="capella-hero"><span class="eyebrow">Deployment profile</span><h3>${d.configured ? "Capella AI Data Plane configured" : "Local-first demo with Capella-ready adapters"}</h3><p>${escapeHtml(d.localTruth || "")}</p></div><div class="capella-service-grid">${Object.entries(d.services || {}).map(([name, service]) => `<article><span>${escapeHtml(name)}</span><strong>${service.enabled ? "Configured" : "Optional"}</strong><p>${escapeHtml(service.detail || service.mode || "")}</p></article>`).join("")}</div><div class="deep-link-grid">${Object.entries(links).filter(([, url]) => url).map(([name, url]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Open ${escapeHtml(name)}</a>`).join("")}</div><div class="capella-flow"><span>Model Service caching</span><b>→</b><span>AI Functions</span><b>→</b><span>Managed Data Processing</span><b>→</b><span>Agent Tracer</span></div>`;
  } catch (error) { $("capellaShowcase").innerHTML = `<div class="empty-row">${escapeHtml(error.message)}</div>`; }
}

function bindEvents() {
  $("experienceModeToggle").onchange = (event) => applyExperienceMode(
    event.target.checked ? "customer" : "showcase",
    { persist: true, announce: true },
  );
  $("openAssistantButton").onclick = openAssistant; $("showcaseNavButton").onclick = openShowcaseStudio; $("closeAssistantButton").onclick = closeAssistant; $("drawerScrim").onclick = closeAssistant;
  $("openSearchButton").onclick = () => openSearch(); $("closeSearchButton").onclick = closeSearch; $("catalogueSearchForm").onsubmit = runCatalogueSearch;
  $("chatForm").onsubmit = (event) => { event.preventDefault(); sendChat($("chatInput").value); };
  $("promptChips").addEventListener("click", (event) => { if (event.target.tagName === "BUTTON") sendChat(event.target.textContent); });
  $("newSessionButton").onclick = newSession; $("memoryButton").onclick = openMemory; $("assistantProfileButton").onclick = openProfile; $("assistantInspectorButton").onclick = () => { closeAssistant(); loadAgentInspector(true); };
  $("closeDetail").onclick = () => $("detailDialog").close(); $("closeShowcase").onclick = () => $("showcaseDialog").close(); $("closeMemory").onclick = () => $("memoryDialog").close(); $("closeProfile").onclick = () => $("profileDialog").close(); $("closeTraceThread").onclick = () => $("traceThreadDialog").close();
  $("copyTraceJson").onclick = async () => { if (!state.traceThread) return; await navigator.clipboard.writeText(prettyJson(state.traceThread)); showToast("Session trace JSON copied"); };
  $("downloadSessionJson").onclick = () => { if (!state.traceThread) return; downloadJson(`streamai-trace-${state.traceThread.sessionId || "turn"}.json`, state.traceThread); };
  $("comparisonAssumptionsForm").onsubmit = saveComparisonAssumptions;
  $("comparisonAssumptionsForm").oninput = () => { $("comparisonAssumptionsForm").dataset.dirty="true"; };
  $("syncProfileMemory").onclick = syncProfileMemory;
  $("memorySearchForm").onsubmit = runMemorySearch; $("searchLabForm").onsubmit = runSearchLab; $("policySimulatorForm").onsubmit = runPolicySimulation; $("profileForm").onsubmit = saveProfile;
  document.querySelectorAll("[data-showcase-tab]").forEach((button) => button.onclick = () => setShowcaseTab(button.dataset.showcaseTab));
  $("showcaseRefresh").onclick = openShowcaseStudio; $("showcaseReset").onclick = resetShowcaseDemo; $("clearFaults").onclick = clearFaults; $("runEvaluation").onclick = runEvaluation; $("loadSupportingDocs").onclick = loadSupportingDocuments; $("loadProfileTimeline").onclick = loadProfileTimeline;
  document.querySelectorAll("[data-memory-tab]").forEach((button) => button.onclick = () => renderMemory(button.dataset.memoryTab));
  document.querySelectorAll("[data-auth-tab]").forEach((button) => button.onclick = () => setAuthTab(button.dataset.authTab));
  $("loginForm").onsubmit = handleLogin; $("registerForm").onsubmit = handleRegister;
  $("viewerButton").onclick = () => state.viewer ? openProfile() : showAuth(); $("profileNavButton").onclick = openProfile;
  $("refreshStatus").onclick = refreshStatus; $("serviceStatusButton").onclick = () => document.querySelector(".data-plane-section").scrollIntoView({ behavior: "smooth" });
  $("runMemoryComparison").onclick = runMemoryComparison; $("refreshMetrics").onclick = loadMetrics; $("resetMetrics").onclick = resetMetrics; $("metricsNavButton").onclick = async () => { await loadMetrics(); $("metricsSection").scrollIntoView({ behavior: "smooth" }); };
  $("refreshInspector").onclick = () => loadAgentInspector(false); $("mcpProofButton").onclick = runMcpProof; $("aiFunctionsProofButton").onclick = runAIFunctionsProof; $("dataProcessingProofButton").onclick = runDataProcessingProof; $("inspectorNavButton").onclick = () => loadAgentInspector(true);
  document.querySelectorAll("[data-search-preset]").forEach((button) => button.onclick = () => openSearch(button.dataset.searchPreset));
  document.querySelectorAll("[data-row-jump]").forEach((button) => button.onclick = () => document.querySelector(`[data-row-id="${button.dataset.rowJump}"]`)?.scrollIntoView({ behavior: "smooth" }));
  document.querySelectorAll("[data-home]").forEach((button) => button.onclick = loadHomeProgressively); $("brandButton").onclick = loadHomeProgressively;
}

async function boot() {
  await loadExperienceConfig();
  bindEvents();
  if (!state.metricsRefreshTimer) state.metricsRefreshTimer = setInterval(refreshVisibleMetrics, 5000);
  renderHero(null);
  showAuth();
  pollReadiness();
  if (isShowcaseExperience()) renderServices({ starting: true });
}

document.addEventListener("DOMContentLoaded", () => { void boot(); });
