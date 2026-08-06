// Pages 자체 파일 사용 (raw.githubusercontent.com CDN 캐시 회피)
const STATE_URL = "./state.json";

const card = document.getElementById("statusCard");
const refreshBtn = document.getElementById("refreshBtn");

function formatProcessedAt(raw) {
  const text = String(raw || "").trim();
  if (text.length >= 14 && /^\d{14}/.test(text)) {
    return `${text.slice(2, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)}:${text.slice(12, 14)}`;
  }
  if (/^\d{4}-\d{2}-\d{2}/.test(text)) {
    return text.length >= 19 ? `${text.slice(2, 19)}` : text.slice(2);
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

function renderStages(stages) {
  if (!Array.isArray(stages) || !stages.length) {
    return `<p class="muted">진행 단계 정보가 아직 없습니다. 다음 자동 조회 후 표시됩니다.</p>`;
  }

  // 유니패스처럼 최신 단계가 위로 오도록 역순 표시
  const rows = [...stages]
    .reverse()
    .map((stage, idx) => {
      const state = stage.state || "pending";
      const when =
        formatProcessedAt(stage.processed_at) ||
        (state === "current" ? "진행 중" : "");
      return `
        <div class="stage ${escapeHtml(state)}">
          <div class="n">${idx + 1}</div>
          <div class="body">
            <div class="name">${escapeHtml(stage.name || "-")}</div>
            <div class="when">${escapeHtml(when)}</div>
          </div>
        </div>
      `;
    })
    .join("");

  return `<div class="timeline">${rows}</div>`;
}

function renderItem(item) {
  card.innerHTML = `
    <p class="label">현재 단계</p>
    <h2 class="status">${escapeHtml(item.status || "-")}</h2>
    <dl class="meta">
      <div class="row"><dt>송장번호</dt><dd>${escapeHtml(item.hbl || "-")}</dd></div>
      <div class="row"><dt>품명</dt><dd>${escapeHtml(item.product_name || "-")}</dd></div>
    </dl>
    <p class="label steps-label">처리 진행</p>
    ${renderStages(item.stages)}
  `;
}

async function loadState() {
  card.innerHTML = `<p class="muted">불러오는 중…</p>`;
  try {
    const resp = await fetch(`${STATE_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const state = await resp.json();
    const item = pickLatest(state);
    if (!item) {
      card.innerHTML = `<p class="error">저장된 통관 상태가 없습니다.</p>`;
      return;
    }
    renderItem(item);
  } catch (err) {
    card.innerHTML = `<p class="error">상태 불러오기 실패: ${err.message || err}</p>`;
  }
}

refreshBtn.addEventListener("click", loadState);
loadState();
