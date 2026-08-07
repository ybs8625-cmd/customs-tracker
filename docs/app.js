// Pages 자체 파일 사용 (raw.githubusercontent.com CDN 캐시 회피)
const STATE_URL = "./state.json";

const card = document.getElementById("statusCard");
const refreshBtn = document.getElementById("refreshBtn");
const pageTitle = document.getElementById("pageTitle");
const pageSub = document.getElementById("pageSub");

function formatProcessedAt(raw) {
  const text = String(raw || "").trim();
  if (text.length >= 14 && /^\d{14}/.test(text)) {
    return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)}:${text.slice(12, 14)}`;
  }
  if (/^\d{4}-\d{2}-\d{2}/.test(text)) {
    return text.length >= 19 ? text.slice(0, 19) : text;
  }
  return text;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function pickLatest(state) {
  const items = Object.values(state || {});
  if (!items.length) return null;
  return items.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))[0];
}

function normalizeItem(item) {
  const customs = item.customs || {
    status: item.status,
    product_name: item.product_name,
    processed_at: item.processed_at,
    current_stage_index: item.current_stage_index,
    stages: item.stages || [],
  };
  const domestic = item.domestic || null;
  const statusText = String(customs.status || "");
  const clearanceDone =
    item.clearance_done === true ||
    Number(customs.current_stage_index) >= 7 ||
    statusText.includes("물품반출") ||
    statusText.includes("반출신고") ||
    statusText.includes("반출완료");
  return { item, customs, domestic, clearanceDone };
}

function renderStages(stages) {
  if (!Array.isArray(stages) || !stages.length) {
    return `<p class="muted">진행 단계 정보가 아직 없습니다. 다음 자동 조회 후 표시됩니다.</p>`;
  }

  // 표출: 최신/상위 단계가 위. 번호: 아래가 1
  const rows = [...stages].reverse();
  const total = rows.length;
  const html = rows
    .map((stage, idx) => {
      const state = stage.state || "pending";
      const when =
        formatProcessedAt(stage.processed_at) ||
        (state === "current" ? "진행 중" : "");
      const loc = stage.location ? ` · ${stage.location}` : "";
      const num = total - idx;
      return `
        <div class="stage ${escapeHtml(state)}">
          <div class="n">${num}</div>
          <div class="body">
            <div class="name">${escapeHtml(stage.name || "-")}</div>
            <div class="when">${escapeHtml(when)}${escapeHtml(loc)}</div>
          </div>
        </div>
      `;
    })
    .join("");

  return `<div class="timeline">${html}</div>`;
}

function renderDomesticEvents(events) {
  if (!Array.isArray(events) || !events.length) {
    return "";
  }

  // 표출: 최신이 위(기존 유지). 번호: 아래(과거)가 1
  const total = events.length;
  const html = events
    .map((ev, idx) => {
      const name = ev.raw_status || ev.stage || "-";
      const when = formatProcessedAt(ev.processed_at) || "";
      const loc = ev.location ? ` · ${ev.location}` : "";
      const note = ev.note ? `<div class="ev-note">${escapeHtml(ev.note)}</div>` : "";
      const state = idx === 0 ? "current" : "done";
      const num = total - idx;
      return `
        <div class="stage ${state}">
          <div class="n">${num}</div>
          <div class="body">
            <div class="name">${escapeHtml(name)}</div>
            <div class="when">${escapeHtml(when)}${escapeHtml(loc)}</div>
            ${note}
          </div>
        </div>
      `;
    })
    .join("");

  return `<div class="timeline">${html}</div>`;
}

function renderCustoms(item, customs) {
  if (pageTitle) pageTitle.textContent = "통관 진행상태";
  if (pageSub) pageSub.textContent = "GitHub Actions가 저장한 최신 통관 스냅샷입니다.";
  card.innerHTML = `
    <p class="label">현재 단계</p>
    <h2 class="status">${escapeHtml(customs.status || item.status || "-")}</h2>
    <dl class="meta">
      <div class="row"><dt>송장번호</dt><dd>${escapeHtml(item.hbl || "-")}</dd></div>
      <div class="row"><dt>품명</dt><dd>${escapeHtml(customs.product_name || item.product_name || "-")}</dd></div>
    </dl>
    <p class="label steps-label">통관 진행</p>
    ${renderStages(customs.stages || item.stages || [])}
  `;
}

function renderDomestic(item, domestic) {
  if (pageTitle) pageTitle.textContent = "국내배송 진행상태";
  if (pageSub) pageSub.textContent = "CJ대한통운 스캔 이력입니다.";
  const events = domestic?.events || [];
  const latest = events[0] || {};
  // 현재단계 = CJ 요약상태(nsDlvNm). 이력 최신 스캔명과 다를 수 있음(예: 간선하차 vs 간선상차).
  const status =
    domestic?.status ||
    latest.raw_status ||
    latest.stage ||
    "배송준비";
  const invoice = domestic?.invoice || item.hbl || "-";
  const when = formatProcessedAt(domestic?.processed_at || latest.processed_at) || "-";
  const note = domestic?.error
    ? `<p class="muted note">${escapeHtml(domestic.error)}</p>`
    : "";
  const detailBlock = events.length
    ? renderDomesticEvents(events)
    : `<p class="muted">아직 배송 이력이 없습니다.</p>`;
  const loc = domestic?.location || latest.location || "-";
  card.innerHTML = `
    <p class="label">현재 단계</p>
    <h2 class="status">${escapeHtml(status)}</h2>
    <dl class="meta">
      <div class="row"><dt>송장번호</dt><dd>${escapeHtml(invoice)}</dd></div>
      <div class="row"><dt>품명</dt><dd>${escapeHtml(item.product_name || item.customs?.product_name || "-")}</dd></div>
      <div class="row"><dt>위치</dt><dd>${escapeHtml(loc)}</dd></div>
      <div class="row"><dt>최종갱신</dt><dd>${escapeHtml(when)}</dd></div>
    </dl>
    ${note}
    <p class="label steps-label">배송 이력</p>
    ${detailBlock}
  `;
}

function renderItem(raw) {
  const { item, customs, domestic, clearanceDone } = normalizeItem(raw);
  if (clearanceDone) {
    renderDomestic(item, domestic);
  } else {
    renderCustoms(item, customs);
  }
}

async function loadState() {
  card.innerHTML = `<p class="muted">불러오는 중…</p>`;
  try {
    const resp = await fetch(`${STATE_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const state = await resp.json();
    const item = pickLatest(state);
    if (!item) {
      card.innerHTML = `<p class="error">저장된 상태가 없습니다.</p>`;
      return;
    }
    renderItem(item);
  } catch (err) {
    card.innerHTML = `<p class="error">상태 불러오기 실패: ${err.message || err}</p>`;
  }
}

refreshBtn.addEventListener("click", loadState);
loadState();
