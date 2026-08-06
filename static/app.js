const RECENT_KEY = "customs-tracker-recent";

const form = document.getElementById("trackForm");
const hblInput = document.getElementById("hblInput");
const yearInput = document.getElementById("yearInput");
const submitBtn = document.getElementById("submitBtn");
const statusArea = document.getElementById("statusArea");
const errorArea = document.getElementById("errorArea");
const recentChips = document.getElementById("recentChips");

yearInput.value = String(new Date().getFullYear());

// 기본값: 직전 대화에서 보던 송장
hblInput.value = "509799520393";

function getRecent() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveRecent(hbl, year) {
  const next = [{ hbl, year }, ...getRecent().filter((x) => x.hbl !== hbl)].slice(0, 6);
  localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  renderRecent();
}

function renderRecent() {
  const items = getRecent();
  recentChips.innerHTML = "";
  items.forEach((item) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.textContent = `${item.hbl} · ${item.year}`;
    btn.addEventListener("click", () => {
      hblInput.value = item.hbl;
      yearInput.value = String(item.year);
      form.requestSubmit();
    });
    recentChips.appendChild(btn);
  });
}

function showError(msg) {
  statusArea.classList.add("hidden");
  errorArea.classList.remove("hidden");
  errorArea.textContent = msg;
}

function row(dl, label, value) {
  if (!value) return;
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  dl.append(dt, dd);
}

function renderResult(data) {
  errorArea.classList.add("hidden");
  statusArea.classList.remove("hidden");

  document.getElementById("currentStatus").textContent = data.status || "-";
  document.getElementById("productLine").textContent = [
    data.product_name,
    data.forwarder,
    data.arrival_port,
  ]
    .filter(Boolean)
    .join(" · ");

  const eta = data.eta;
  const etaEl = document.getElementById("etaReceive");
  const noteEl = document.getElementById("etaNote");
  if (eta) {
    etaEl.textContent =
      eta.receive_eta_from === eta.receive_eta_to
        ? eta.receive_eta_from
        : `${eta.receive_eta_from} ~ ${eta.receive_eta_to}`;
    noteEl.textContent = `${eta.cj_pickup}. ${eta.note}`;
  } else {
    etaEl.textContent = "-";
    noteEl.textContent = "";
  }

  const timeline = document.getElementById("timeline");
  timeline.innerHTML = "";
  (data.stages || []).forEach((stage, idx) => {
    const el = document.createElement("div");
    el.className = `stage ${stage.state}`;
    el.innerHTML = `
      <div class="n">${String(idx + 1).padStart(2, "0")}</div>
      <div class="name">${stage.name}</div>
      <div class="when">${stage.processed_at || (stage.state === "current" ? "진행 중" : "")}</div>
    `;
    timeline.appendChild(el);
  });

  const infoList = document.getElementById("infoList");
  infoList.innerHTML = "";
  row(infoList, "H B/L", data.hbl);
  row(infoList, "M B/L", data.mbl);
  row(infoList, "화물관리번호", data.cargo_no);
  row(infoList, "입항일", data.arrival_date);
  row(infoList, "양륙항", data.arrival_port);
  row(infoList, "입항세관", data.customs);
  row(infoList, "선사/항공", data.carrier);
  row(infoList, "특송업체", data.forwarder);
  row(infoList, "선박/편명", data.vessel);
  row(infoList, "컨테이너", data.container);
  row(infoList, "포장/중량", [data.packs, data.weight].filter(Boolean).join(" / "));
  row(infoList, "화물구분", data.cargo_type);
  row(infoList, "처리일시", data.processed_at);

  const eventList = document.getElementById("eventList");
  eventList.innerHTML = "";
  const events = data.events || [];
  if (!events.length) {
    const li = document.createElement("li");
    li.textContent = "상세 이력이 없습니다.";
    eventList.appendChild(li);
  } else {
    events.forEach((ev) => {
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="t">${ev.stage || "-"}</div>
        <div class="d">${[ev.processed_at, ev.warehouse, ev.note].filter(Boolean).join(" · ")}</div>
      `;
      eventList.appendChild(li);
    });
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const hbl = hblInput.value.trim();
  const year = Number(yearInput.value) || new Date().getFullYear();
  if (!hbl) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "조회 중...";
  errorArea.classList.add("hidden");

  try {
    const url = `/api/track?hbl=${encodeURIComponent(hbl)}&year=${year}`;
    const res = await fetch(url);
    const data = await res.json();
    if (!data.found) {
      showError(data.error || "조회 결과가 없습니다.");
      return;
    }
    saveRecent(hbl, year);
    renderResult(data);
    const demoNote = document.getElementById("demoNote");
    if (data.source === "demo" && data.error) {
      demoNote.hidden = false;
      demoNote.style.color = "#a16207";
      demoNote.textContent = data.error;
    } else {
      demoNote.hidden = true;
      demoNote.textContent = "";
    }
  } catch (err) {
    showError(`조회 중 오류: ${err.message || err}`);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "조회";
  }
});

renderRecent();
