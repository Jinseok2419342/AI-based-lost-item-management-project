/* 분실물 관리 — 관리자 웹 앱 */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const CATEGORY_KO = { valuable: "귀중품", general: "일반 물품", food: "음식" };
const STATUS_KO = { stored: "보관 중", retrieved: "회수됨", disposed: "폐기됨" };
const STATE_KO = {
  idle: "감시 중",
  motion: "움직임 감지",
  settling: "분석 대기",
  no_camera: "카메라 없음",
  paused: "일시정지됨",
};

const state = {
  page: "dashboard",
  filterStatus: "",
  filterCat: "",
  search: "",
  items: [],
  itemsById: new Map(),
  status: null,
  modalItem: null,
  modalCategory: null,
};

/* ── 테마 ─────────────────────────────────────────── */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("lf-theme", theme);
}
(() => {
  const saved = localStorage.getItem("lf-theme");
  const prefersDark = matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
})();
$("#themeToggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  applyTheme(cur === "dark" ? "light" : "dark");
});

/* ── 공통 fetch/토스트 ────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = `요청 실패 (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function toast(message, type = "info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="t-dot"></span><span></span>`;
  el.lastElementChild.textContent = message;
  $("#toastRegion").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 350);
  }, 2800);
}

/* ── 유틸 ─────────────────────────────────────────── */
function ddayOf(item) {
  const end = new Date(item.deadline);
  const today = new Date();
  end.setHours(0, 0, 0, 0);
  today.setHours(0, 0, 0, 0);
  return Math.round((end - today) / 86400000);
}
function ddayBadge(item) {
  const warnDays = state.status?.warn_before_days ?? 3;
  const d = ddayOf(item);
  if (d < 0) return `<span class="badge badge-dday over">기한 지남</span>`;
  if (d === 0) return `<span class="badge badge-dday over">D-DAY</span>`;
  if (d <= warnDays) return `<span class="badge badge-dday soon">D-${d}</span>`;
  return `<span class="badge badge-dday">D-${d}</span>`;
}
function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso);
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return "방금 전";
  if (diff < 3600) return `${Math.floor(diff / 60)}분 전`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  return `${t.getMonth() + 1}월 ${t.getDate()}일 ${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const PLACEHOLDER_SVG = `<div class="item-photo-fallback"><svg viewBox="0 0 24 24"><path d="M12 3 4 7v10l8 4 8-4V7l-8-4z"/><path d="M4 7l8 4 8-4"/><path d="M12 11v10"/></svg></div>`;

/* ── 내비게이션 ───────────────────────────────────── */
function goto(page) {
  state.page = page;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${page}`));
  if (page === "items") {
    renderItems(); // 캐시로 즉시 표시하고, 최신 데이터는 백그라운드로 갱신
    pollItemsCache();
  }
  if (page === "events") refreshEvents();
  if (page === "settings") loadSettings();
}
$$(".nav-item").forEach((b) => b.addEventListener("click", () => goto(b.dataset.page)));
$$("[data-goto]").forEach((b) => b.addEventListener("click", () => goto(b.dataset.goto)));

/* ── 상태 폴링 ────────────────────────────────────── */
let serverDown = false;
let settleAnchor = null; // { remaining, at } — 안정화 카운트다운 보간용

function renderStatePill() {
  const s = state.status;
  if (!s) return;
  const pill = $("#statePill");
  const key = s.paused ? "paused" : s.state;
  let label = STATE_KO[key] || key;
  if (key === "settling" && settleAnchor) {
    const left = settleAnchor.remaining - (performance.now() - settleAnchor.at) / 1000;
    label = `${label} · ${Math.max(1, Math.ceil(left))}초`;
  }
  if (pill.textContent !== label) pill.textContent = label;
  pill.className = `state-pill ${key}`;
}
setInterval(renderStatePill, 250);
function reloadStream() {
  $("#liveStream").src = `/api/stream?t=${Date.now()}`;
}
$("#liveStream").addEventListener("error", () => setTimeout(reloadStream, 3000));

async function pollStatus() {
  try {
    const s = await api("/api/status");
    if (serverDown) {
      serverDown = false;
      reloadStream(); // 서버가 살아나면 멈춘 MJPEG 스트림 재연결
    }
    state.status = s;
    // 카운트다운은 서버 값(1.5초 폴링)을 앵커로 삼아 로컬에서 보간 → 3·2·1이 건너뛰지 않음
    settleAnchor =
      !s.paused && s.state === "settling" && s.settle_remaining != null
        ? { remaining: s.settle_remaining, at: performance.now() }
        : null;
    renderStatePill();
    $("#btnPause").textContent = s.paused ? "감지 재개" : "일시정지";
    $("#btnPause").classList.toggle("on", s.paused);

    const dot = $("#systemDot");
    const label = $("#systemLabel");
    if (!s.camera_connected) {
      dot.className = "dot err";
      label.textContent = "카메라 미연결";
    } else if (s.ai_provider === "mock") {
      dot.className = "dot warn";
      label.textContent = "동작 중 · AI 미설정";
    } else {
      dot.className = "dot ok";
      label.textContent = `동작 중 · ${s.ai_provider}`;
    }
    renderOverlay(s);
  } catch {
    serverDown = true;
    $("#systemDot").className = "dot err";
    $("#systemLabel").textContent = "서버 연결 끊김";
  }
}

function renderOverlay(s) {
  const overlay = $("#liveOverlay");
  const img = $("#liveStream");
  if (!s.camera_connected || !s.frame_width || !s.frame_height || !img.clientWidth) {
    overlay.innerHTML = ""; // 카메라 없음 화면 위에 어긋난 박스를 남기지 않는다
    return;
  }
  // object-fit: contain 보정 — 실제 표시되는 영상 사각형 계산
  const boxW = img.clientWidth, boxH = img.clientHeight;
  const scale = Math.min(boxW / s.frame_width, boxH / s.frame_height);
  const drawW = s.frame_width * scale, drawH = s.frame_height * scale;
  const offX = (boxW - drawW) / 2, offY = (boxH - drawH) / 2;

  // 기존 박스를 재사용해 위치만 갱신 — CSS transition이 부드럽게 이동시킨다
  const seen = new Set();
  for (const t of s.tracks || []) {
    const tid = String(t.item_id);
    seen.add(tid);
    let el = overlay.querySelector(`[data-tid="${tid}"]`);
    if (!el) {
      el = document.createElement("div");
      el.className = "track-box";
      el.dataset.tid = tid;
      el.innerHTML = `<span class="track-tag"></span>`;
      overlay.appendChild(el);
    }
    const [x, y, w, h] = t.bbox;
    el.style.left = `${offX + x * scale}px`;
    el.style.top = `${offY + y * scale}px`;
    el.style.width = `${w * scale}px`;
    el.style.height = `${h * scale}px`;
    const item = state.itemsById.get(t.item_id);
    const name = item && item.ai_status !== "pending" ? item.name : `#${t.item_id} 분석 중`;
    const tag = el.firstElementChild;
    if (tag.textContent !== name) tag.textContent = name;
  }
  for (const el of [...overlay.children]) {
    if (!seen.has(el.dataset.tid)) el.remove();
  }
}

/* ── 통계/최근 ────────────────────────────────────── */
async function pollStats() {
  try {
    const s = await api("/api/stats");
    $("#statStored").textContent = s.stored;
    $("#statToday").textContent = s.registered_today;
    $("#statExpired").textContent = s.expired;
    $("#statRetrieved").textContent = s.retrieved;
    $("#navItemCount").textContent = s.stored > 0 ? s.stored : "";
    $("#dashCaption").textContent =
      s.expired > 0 ? `폐기 대상 물품이 ${s.expired}건 있습니다.` : "실시간 감시 현황";
  } catch {}
}

/* 목록은 내용이 실제로 바뀔 때만 다시 그린다 — 매 폴링마다 innerHTML을 갈아끼우면
   사진이 깜빡여 화면이 지저분해진다. 상대 시각("n분 전") 갱신용으로 60초마다는 강제 렌더. */
let itemsSig = "";
let lastListRender = 0;

async function pollItemsCache(force = false) {
  try {
    const data = await api("/api/items");
    state.items = data.items;
    state.itemsById = new Map(data.items.map((i) => [i.id, i]));
    const sig = JSON.stringify(
      data.items.map((i) => [i.id, i.status, i.name, i.category, i.deadline, i.ai_status, !!i.photo_path])
    );
    const changed = sig !== itemsSig;
    itemsSig = sig;
    if (force || changed || Date.now() - lastListRender > 60000) {
      lastListRender = Date.now();
      renderRecent();
      if (state.page === "items") renderItems();
    }
  } catch {}
}

function renderRecent() {
  const recent = state.items.slice(0, 6);
  const wrap = $("#recentList");
  if (!recent.length) {
    wrap.innerHTML = `<div class="empty-note">아직 등록된 분실물이 없습니다.</div>`;
    return;
  }
  wrap.innerHTML = recent
    .map(
      (i) => `
    <div class="recent-item" data-id="${i.id}">
      ${i.photo_path ? `<img src="/api/items/${i.id}/photo" alt="" loading="lazy" onerror="this.outerHTML='<div class=noimg>📦</div>'">` : `<div class="noimg">📦</div>`}
      <div class="ri-body">
        <div class="ri-name">${esc(i.name)}</div>
        <div class="ri-sub">${relTime(i.registered_at)} · ${CATEGORY_KO[i.category] || i.category}</div>
      </div>
      ${i.status === "stored" ? ddayBadge(i) : `<span class="badge badge-${i.status}">${STATUS_KO[i.status]}</span>`}
    </div>`
    )
    .join("");
  wrap.querySelectorAll(".recent-item").forEach((el) =>
    el.addEventListener("click", () => openModal(+el.dataset.id))
  );
}

/* ── 물품 목록 ────────────────────────────────────── */
const seenCardIds = new Set(); // 등장 애니메이션을 이미 재생한 카드

async function refreshItems() {
  await pollItemsCache(true);
}

function renderItems() {
  let list = state.items;
  if (state.filterStatus) list = list.filter((i) => i.status === state.filterStatus);
  if (state.filterCat) list = list.filter((i) => i.category === state.filterCat);
  if (state.search) {
    const q = state.search.toLowerCase();
    list = list.filter(
      (i) => i.name.toLowerCase().includes(q) || (i.description || "").toLowerCase().includes(q)
    );
  }
  $("#itemsCaption").textContent = `${list.length}개 물품`;
  $("#itemsEmpty").hidden = list.length > 0;
  if (!list.length) {
    const filtered = !!(state.filterStatus || state.filterCat || state.search);
    $("#itemsEmptyTitle").textContent = filtered
      ? "조건에 맞는 물품이 없습니다"
      : "표시할 분실물이 없습니다";
    $("#itemsEmptyHint").textContent = filtered
      ? "필터나 검색어를 변경해 보세요."
      : "카메라 앞에 물건이 놓이면 자동으로 등록됩니다.";
  }
  $("#itemGrid").innerHTML = list
    .map((i) => {
      const pending = i.ai_status === "pending";
      // 한 번 화면에 나온 카드는 등장 애니메이션을 재생하지 않는다 (재렌더 시 전체 점멸 방지)
      return `
    <div class="item-card${seenCardIds.has(i.id) ? " no-anim" : ""}" data-id="${i.id}">
      ${i.photo_path ? `<img class="item-photo" src="/api/items/${i.id}/photo" alt="${esc(i.name)}" loading="lazy">` : PLACEHOLDER_SVG}
      <div class="item-body">
        <div class="item-name">${pending ? `<span class="pending">분석 중…</span>` : esc(i.name)}</div>
        <div class="item-sub">#${i.id} · ${relTime(i.registered_at)}</div>
        <div class="item-badges">
          <span class="badge badge-${i.category}">${CATEGORY_KO[i.category] || i.category}</span>
          ${i.status === "stored" ? ddayBadge(i) : ""}
          <span class="badge badge-${i.status}">${STATUS_KO[i.status]}</span>
        </div>
      </div>
    </div>`;
    })
    .join("");
  list.forEach((i) => seenCardIds.add(i.id));
  $("#itemGrid").querySelectorAll(".item-card").forEach((el) =>
    el.addEventListener("click", () => openModal(+el.dataset.id))
  );
}

$$("#statusSeg .seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#statusSeg .seg-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.filterStatus = b.dataset.status;
    renderItems();
  })
);
$$("#catChips .chip").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#catChips .chip").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.filterCat = b.dataset.cat;
    renderItems();
  })
);
$("#searchInput").addEventListener("input", (e) => {
  state.search = e.target.value.trim();
  renderItems();
});

/* 사진으로 수동 등록 */
$("#manualPhoto").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("photo", file);
  try {
    await api("/api/items/manual", { method: "POST", body: fd });
    toast("사진이 등록되었습니다. AI가 분석 중입니다.", "ok");
    await refreshItems();
  } catch (err) {
    toast(err.message, "err");
  }
  e.target.value = "";
});

/* ── 모달 ─────────────────────────────────────────── */
function openModal(id) {
  const item = state.itemsById.get(id);
  if (!item) return;
  state.modalItem = item;
  state.modalCategory = item.category;
  $("#modalTitle").textContent = `#${item.id} ${item.name}`;
  $("#modalPhoto").src = item.photo_path ? `/api/items/${item.id}/photo?t=${Date.now()}` : "";
  $("#m_name").value = item.name;
  $("#m_description").value = item.description || "";
  $("#m_deadline").value = String(item.deadline).slice(0, 10);
  $$("#m_category .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.val === item.category)
  );
  const prov = item.ai_provider ? `${item.ai_provider} · 확신도 ${Math.round(item.ai_confidence * 100)}%` : "—";
  $("#m_meta").innerHTML = `
    <div>등록: <b>${esc(String(item.registered_at).replace("T", " "))}</b> (${item.source === "manual" ? "수동 등록" : "카메라 감지"})</div>
    <div>상태: <b>${STATUS_KO[item.status]}</b>${item.retrieved_at ? ` · 회수 ${esc(String(item.retrieved_at).slice(0, 16).replace("T", " "))}` : ""}${item.disposed_at ? ` · 폐기 ${esc(String(item.disposed_at).slice(0, 16).replace("T", " "))}` : ""}</div>
    <div>AI 분석: <b>${esc(prov)}</b></div>`;
  $("#m_status_toggle").textContent = item.status === "stored" ? "회수 처리" : "재보관";
  $("#m_dispose").style.display = item.status === "disposed" ? "none" : "";
  const del = $("#m_delete");
  del.textContent = "삭제";
  del.dataset.armed = "";
  $("#modalBackdrop").hidden = false;
}
function closeModal() {
  $("#modalBackdrop").hidden = true;
  state.modalItem = null;
}
$("#modalClose").addEventListener("click", closeModal);
$("#modalBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#modalBackdrop")) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#modalBackdrop").hidden) closeModal();
});
$$("#m_category .seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$("#m_category .seg-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.modalCategory = b.dataset.val;
  })
);

$("#m_save").addEventListener("click", async () => {
  const item = state.modalItem;
  if (!item) return;
  // 실제로 바꾼 값만 전송 — AI 분석이 끝나기 전 저장해도 다른 필드를 덮어쓰지 않음
  const body = {};
  if ($("#m_name").value.trim() !== item.name) body.name = $("#m_name").value;
  if ($("#m_description").value.trim() !== (item.description || ""))
    body.description = $("#m_description").value;
  if (state.modalCategory !== item.category) body.category = state.modalCategory;
  const d = $("#m_deadline").value;
  if (d && d !== String(item.deadline).slice(0, 10)) body.deadline = d;
  if (!Object.keys(body).length) {
    closeModal();
    return;
  }
  try {
    await api(`/api/items/${item.id}`, { method: "PATCH", body: JSON.stringify(body) });
    toast("저장되었습니다.", "ok");
    closeModal();
    await refreshItems();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#m_status_toggle").addEventListener("click", async () => {
  const item = state.modalItem;
  if (!item) return;
  const next = item.status === "stored" ? "retrieved" : "stored";
  try {
    await api(`/api/items/${item.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) });
    toast(next === "retrieved" ? "회수 처리되었습니다." : "다시 보관 중으로 변경했습니다.", "ok");
    closeModal();
    await refreshItems();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#m_dispose").addEventListener("click", async () => {
  const item = state.modalItem;
  if (!item) return;
  try {
    await api(`/api/items/${item.id}`, { method: "PATCH", body: JSON.stringify({ status: "disposed" }) });
    toast("폐기 처리되었습니다.", "ok");
    closeModal();
    await refreshItems();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#m_reclassify").addEventListener("click", async () => {
  const item = state.modalItem;
  if (!item) return;
  try {
    await api(`/api/items/${item.id}/reclassify`, { method: "POST" });
    toast("AI 재분석을 요청했습니다.", "ok");
    closeModal();
    await refreshItems();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#m_delete").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const item = state.modalItem;
  if (!item) return;
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1";
    btn.textContent = "정말 삭제할까요?";
    setTimeout(() => {
      btn.dataset.armed = "";
      btn.textContent = "삭제";
    }, 3000);
    return;
  }
  try {
    await api(`/api/items/${item.id}`, { method: "DELETE" });
    toast("삭제되었습니다.", "ok");
    closeModal();
    await refreshItems();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ── 카메라 제어 ──────────────────────────────────── */
$("#btnRebaseline").addEventListener("click", async () => {
  try {
    await api("/api/camera/rebaseline", { method: "POST" });
    toast("기준 화면을 초기화했습니다.", "ok");
  } catch (err) {
    toast(err.message, "err");
  }
});
$("#btnPause").addEventListener("click", async () => {
  const paused = !(state.status && state.status.paused);
  try {
    await api("/api/camera/pause", { method: "POST", body: JSON.stringify({ paused }) });
    toast(paused ? "감지를 일시정지했습니다." : "감지를 재개했습니다.", "ok");
    await pollStatus();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ── 이벤트 로그 ──────────────────────────────────── */
const EVENT_ICON = {
  item_registered: ["reg", `<path d="M12 5v14M5 12h14"/>`],
  ai_classified: ["reg", `<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/>`],
  item_retrieved: ["ret", `<path d="M5 12.5 10 17.5 19 7"/>`],
  status_changed: ["ret", `<path d="M4 12h16M14 6l6 6-6 6"/>`],
  email_sent: ["mail", `<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="m4 7 8 6 8-6"/>`],
  email_failed: ["err", `<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="m4 7 8 6 8-6"/>`],
  rebaseline: ["warn", `<path d="M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5"/>`],
  item_ignored: ["warn", `<circle cx="12" cy="12" r="9"/><path d="M6 6l12 12"/>`],
  item_deleted: ["err", `<path d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13"/>`],
};
let eventsSig = "";
async function refreshEvents() {
  try {
    const data = await api("/api/events");
    const sig = data.events.length ? `${data.events[0].id}:${data.events.length}` : "0";
    if (sig === eventsSig) {
      // 내용 변화 없음 — 상대 시각만 갱신 (전체 재렌더는 등장 애니메이션이 전 행에 재생됨)
      $("#eventList").querySelectorAll(".event-time[data-ts]").forEach((el) => {
        const t = relTime(el.dataset.ts);
        if (el.textContent !== t) el.textContent = t;
      });
      return;
    }
    eventsSig = sig;
    const list = $("#eventList");
    if (!data.events.length) {
      list.innerHTML = `<div class="empty-note">이벤트가 없습니다.</div>`;
      return;
    }
    list.innerHTML = data.events
      .map((ev) => {
        const [cls, path] = EVENT_ICON[ev.type] || ["", `<circle cx="12" cy="12" r="8"/>`];
        return `
      <div class="event-row">
        <div class="event-ico ${cls}"><svg viewBox="0 0 24 24">${path}</svg></div>
        <div>
          <div class="event-msg">${esc(ev.message)}</div>
          <div class="event-time" data-ts="${esc(ev.created_at)}">${relTime(ev.created_at)}</div>
        </div>
      </div>`;
      })
      .join("");
  } catch {}
}

/* ── 설정 ─────────────────────────────────────────── */
const SETTING_FIELDS = [
  "ai_provider", "openai_api_key", "gemini_api_key",
  "days_valuable", "days_general", "days_food", "warn_before_days",
  "admin_email", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
  "camera_source", "settle_seconds", "motion_threshold",
  "min_area_ratio", "match_threshold", "max_change_ratio",
];
async function loadSettings() {
  try {
    const data = await api("/api/settings");
    const s = data.settings;
    for (const key of SETTING_FIELDS) {
      const el = $(`#s_${key}`);
      if (el) el.value = s[key] ?? "";
    }
    $("#s_email_enabled").checked = ["1", "true", "on"].includes(String(s.email_enabled));
    if (state.status) $("#aiProviderNow").textContent = state.status.ai_provider;
  } catch (err) {
    toast(err.message, "err");
  }
}
$("#btnSaveSettings").addEventListener("click", async () => {
  const values = {};
  for (const key of SETTING_FIELDS) {
    const el = $(`#s_${key}`);
    if (el) values[key] = el.value.trim();
  }
  values.email_enabled = $("#s_email_enabled").checked ? "1" : "0";
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
    toast("설정이 저장되었습니다.", "ok");
    await loadSettings();
    await pollStatus();
  } catch (err) {
    toast(err.message, "err");
  }
});
$("#btnTestEmail").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "발송 중…";
  try {
    const r = await api("/api/settings/test-email", { method: "POST" });
    toast(r.message, r.ok ? "ok" : "err");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "테스트 메일 발송";
  }
});
$("#btnRunScheduler").addEventListener("click", async () => {
  try {
    const r = await api("/api/scheduler/run-now", { method: "POST" });
    toast(`검사 완료 — 사전 알림 ${r.warn_sent}건, 기한 도래 ${r.expire_sent}건 발송`, "ok");
    $("#schedulerNote").textContent = `마지막 검사: ${String(r.checked_at).replace("T", " ")} · 1분마다 자동 실행됩니다.`;
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ── 루프 시작 ────────────────────────────────────── */
pollStatus();
pollStats();
pollItemsCache(true);
setInterval(pollStatus, 1500);
setInterval(pollStats, 4000);
setInterval(() => {
  pollItemsCache();
  if (state.page === "events") refreshEvents();
}, 3000);
