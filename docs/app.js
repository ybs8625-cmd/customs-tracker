const STATE_URL =
  "https://raw.githubusercontent.com/ybs8625-cmd/customs-tracker/master/data/state.json";

const card = document.getElementById("statusCard");
const refreshBtn = document.getElementById("refreshBtn");

function formatProcessedAt(raw) {
  const text = String(raw || "").trim();
  if (text.length >= 14 && /^\d{14}/.test(text)) {
    return `${text.slice(2, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)} ${text.slice(8, 10)}:${text.slice(10, 12)}:${text.slice(12, 14)}`;
  }
  return text || "-";
}

function formatUpdatedAt(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const yy = String(d.getFullYear()).slice(2);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${yy}-${mm}-${dd} ${hh}:${mi}:${ss}`;
}

function pickLatest(state) {
  const items = Object.values(state || {});
  if (!items.length) return null;
  return items.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))[0];
}

function renderItem(item) {
  card.innerHTML = `
    <p class="label">현재 단계</p>
    <h2 class="status">${item.status || "-"}</h2>
    <dl class="rows">
      <div class="row"><dt>송장번호</dt><dd>${item.hbl || "-"}</dd></div>
      <div class="row"><dt>품명</dt><dd>${item.product_name || "-"}</dd></div>
      <div class="row"><dt>처리일시</dt><dd>${formatProcessedAt(item.processed_at)}</dd></div>
      <div class="row"><dt>연도</dt><dd>${item.year || "-"}</dd></div>
      <div class="row"><dt>스냅샷 갱신</dt><dd>${formatUpdatedAt(item.updated_at)}</dd></div>
    </dl>
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
