"use strict";
// API 키는 이 파일의 변수에도, 브라우저 저장소에도 남기지 않는다. 요청 헤더로만 보내고 입력칸을 비운다.
const $app = document.getElementById("app");
const $ = id => document.getElementById(id);
const RUNNING = ["queued", "preparing", "analyzing", "rechecking"];
const STAGE_KO = {created: "작업 만듦", queued: "순서 대기 중", preparing: "준비 분석 중", prepared: "준비 완료",
  analyzing: "영상 분석 중", rechecking: "재검증 중", done: "분석 완료", failed: "실패", interrupted: "중단됨"};
const REC = ["", "착용 확인", "업로드했으나 미착용", "업로드 안 함", "확인 불가"];
const KINDS = [["ok", "확인됨", s => s.startsWith("확인됨")], ["sus", "의심", s => s.startsWith("의심")], ["unk", "미확인", s => s === "미확인"],
  ["none", "업로드 없음", s => s === "업로드 없음"], ["err", "채널 오류", s => s === "채널 오류"]];
const FIELD_LABELS = {date: "발송일", no: "번호", name: "이름", product: "상품", option: "옵션", url: "채널 링크"};

let poll = null, board = null, lastState = "", serverVideo = false, flash = "", presets = [], job = null;
let tab = null, filter = "all", query = "", boardKey = "", hitRow = null, prevRunning = false, quoteTimer = null, tabChosen = false, firstBoard = true;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => (/^https:\/\/(www\.)?(youtube\.com|youtu\.be)\//.test(u) ? esc(u) : "");
const kindOf = st => (KINDS.find(k => k[2](st)) || ["err"])[0];
const fmtDur = sec => { sec = Math.max(0, Math.round(sec)); const m = Math.floor(sec / 60), s = sec % 60; return m >= 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : m ? `${m}분 ${String(s).padStart(2, "0")}초` : `${s}초`; };

function route() {
  clearInterval(poll);
  const m = location.hash.match(/^#\/j\/([A-Za-z0-9_-]{20,})$/);
  m ? jobView(m[1]) : homeView();
}

async function api(token, path, opt = {}) {
  const headers = Object.assign({"X-Job-Token": token}, opt.headers || {});
  const r = await fetch(path, Object.assign({}, opt, {headers}));
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* 본문이 JSON이 아님 */ }
    throw new Error(msg);
  }
  return r;
}

// ───────────────────────── 견적 표
function quoteHtml(q) {
  if (!q) return "";
  const a = q.analysis || {}, p = q.prepare || {}, r = q.recheck || {};
  const rows = [];
  if (q.kind === "rough") {
    rows.push(["수집", `YouTube 쿼터 약 ${(p.yt_quota || 0).toLocaleString()} / 일일 10,000`, "OpenAI 비용 없음"]);
    rows.push(["영상 분석", `약 $${a.usd} · ${a.time} · 영상 약 ${a.videos}개`, "유료 · 시간은 패션 93명 실측 기준"]);
  } else {
    rows.push(["영상 분석", a.videos === a.videos_all
      ? `전부 분석하면 약 $${a.usd} · ${a.time} · 영상 ${a.videos}개`
      : `상한 안에서 약 $${a.usd} · ${a.time} · 영상 ${a.videos}개 (전부: $${a.usd_all} · ${a.time_all} · ${a.videos_all}개)`, "유료"]);
    rows.push(["무료 재검증", `댓글 ${r.yt_quota.toLocaleString()} · 링크 ${r.links_followed.toLocaleString()}개`, "YouTube 쿼터만 사용"]);
  }
  return `<table class="quote"><tbody>${rows.map(x => `<tr><th>${esc(x[0])}</th><td>${esc(x[1])}<div class="muted small">${esc(x[2])}</div></td></tr>`).join("")}</tbody></table>
    <p class="muted small">${esc(q.basis || "")}</p>`;
}

// ═════════════════════════ 새 작업 ═════════════════════════
async function homeView() {
  if (!presets.length) { try { presets = await (await fetch("/api/presets")).json(); } catch (e) { presets = []; } }
  let industry = "fashion", mode = "xlsx", mapState = null, channelCount = 0, roughHtml = "", sheetName = "";
  $app.innerHTML = `
  <div class="intro"><h1>채널별 제품 노출을 한 번에 확인</h1>
    <p>유튜버 목록을 넣으면 기간 안의 영상에서 우리 브랜드·제품이 나왔는지 골라냅니다. 결과는 리포트로 보고 엑셀로 받아요.</p></div>
  <div class="home">
    <form id="f" autocomplete="off">
      <section class="card"><div class="sec-title"><span class="num">1</span><h2>업종</h2></div>
        <div class="chips" id="chips"></div><p class="muted" id="indnote" style="margin-top:10px"></p></section>
      <section class="card"><div class="sec-title"><span class="num">2</span><h2>채널 목록</h2></div>
        <div class="seg" role="tablist"><button type="button" role="tab" data-m="xlsx" aria-selected="true">엑셀 올리기</button>
          <button type="button" role="tab" data-m="text" aria-selected="false">주소 붙여넣기</button></div>
        <div id="m-xlsx">
          <label class="drop" id="drop"><input type="file" name="xlsx" accept=".xlsx">
            <b id="dropt">엑셀 파일을 끌어다 놓거나 눌러서 고르세요</b>
            <span class="muted">열 순서가 달라도 자동으로 찾아줍니다 · 한 줄에 유튜버 한 명 · 5MB 이하</span></label>
          <p class="muted small" style="margin:8px 0 0">처음이라면 <a href="/api/template.xlsx" download>엑셀 템플릿 내려받기</a></p>
          <div class="mapbox" id="mapbox" hidden></div></div>
        <div id="m-text" hidden>
          <textarea name="channels_text" rows="5" aria-label="채널 주소" placeholder="한 줄에 하나씩&#10;@핸들&#10;https://www.youtube.com/@핸들&#10;채널이름 https://www.youtube.com/@핸들"></textarea>
          <p class="muted small" id="textcount" style="margin:6px 0 0"></p></div></section>
      <section class="card"><div class="sec-title"><span class="num">3</span><h2>찾을 브랜드</h2></div>
        <label class="f" for="brand" style="margin-top:0">브랜드명 <small>쉼표로 구분 · 표기 변형도 함께 적으세요</small></label>
        <input type="text" id="brand" name="brand" placeholder="예: 브랜드명, brandname" required maxlength="400">
        <label class="f" for="product">상품명 키워드 <small>선택 · 브랜드명 없이 상품명만 쓴 영상도 찾습니다</small></label>
        <input type="text" id="product" name="product" maxlength="600">
        <details class="more"><summary>세부 설정 (수집 기간 · 제목 키워드 · 참고 사진)</summary>
          <div class="grid2"><div><label class="f" for="window">수집 기간 <small>발송일 이후 며칠</small></label>
            <input type="number" id="window" name="window_days" min="7" max="365"></div>
            <div><label class="f" for="since">시작일 <small>발송일이 없는 채널</small></label><input type="date" id="since" name="since_date"></div></div>
          <label class="f" for="pname">상품 이름 <small>엑셀에 상품 열이 없을 때 쓰는 이름</small></label>
          <input type="text" id="pname" name="product_name" value="상품" maxlength="60">
          <label class="f" for="titlekw">먼저 분석할 영상의 제목 키워드 <small>이 단어가 제목에 있으면 우선합니다</small></label>
          <textarea id="titlekw" name="title_keywords" rows="2" maxlength="600"></textarea>
          <label class="f">상품 참고 사진 <small>선택 · 최대 30장 · 영상 분석 정확도가 올라갑니다</small></label>
          <div class="reflist" id="refs"></div><button type="button" id="addref" class="btn" style="margin-top:8px">+ 사진 추가</button></details></section>
    </form>
    <aside class="aside"><section class="card"><h2>이대로 시작</h2>
      <dl class="sumrows"><dt>업종</dt><dd id="s-ind"></dd><dt>채널</dt><dd id="s-ch">아직 없음</dd><dt>브랜드</dt><dd id="s-br">—</dd><dt>기간</dt><dd id="s-win"></dd></dl>
      <hr class="sep"><h3>대략의 견적</h3><div id="roughquote" class="muted" style="margin-top:6px">채널을 넣으면 예상 비용·시간이 나옵니다.</div>
      <hr class="sep"><button class="btn primary big" type="submit" form="f" id="go">작업 만들기</button>
      <p class="muted small" id="msg" style="margin:8px 0 0"></p>
      <p class="muted small" style="margin:10px 0 0">올린 목록은 서버에 <b>7일</b> 보관되고 언제든 지울 수 있어요. API 키는 저장하지 않습니다.</p></section></aside>
  </div>`;
  const form = $("f");
  const refresh = () => {
    const p = presets.find(x => x.id === industry) || presets[0] || {label: "", window_days: 180};
    $("s-ind").textContent = p.label + (p.verified ? "" : " (영상 분석 미검증)");
    $("s-ch").textContent = channelCount ? `${channelCount}곳${sheetName ? " · " + sheetName : ""}` : "아직 없음";
    $("s-br").textContent = form.brand.value.trim() || "—";
    $("s-win").textContent = `발송일 이후 ${form.window_days.value || p.window_days}일`;
    $("roughquote").innerHTML = roughHtml || "채널을 넣으면 예상 비용·시간이 나옵니다.";
  };
  const applyPreset = () => {
    const p = presets.find(x => x.id === industry) || presets[0];
    if (!p) return;
    form.title_keywords.value = p.title_keywords.join(", ");
    form.window_days.value = p.window_days;
    form.product.placeholder = p.product_example;
    $("indnote").innerHTML = p.verified
      ? `영상에서 볼 것: ${esc(p.look_for)}. <b>실제 데이터(93명)로 검증된 업종</b>이에요.`
      : `영상에서 볼 것: ${esc(p.look_for)}. <b>⚠ 영상 분석은 이 업종에서 검증되지 않았어요</b> (분석 기준이 의류 착용). 제목·설명란·댓글·링크 점검은 그대로 쓸 수 있어요.`;
    document.querySelectorAll("#chips .chip").forEach(c => c.setAttribute("aria-pressed", String(c.dataset.id === industry)));
    refresh();
  };
  $("chips").innerHTML = presets.map(p => `<button type="button" class="chip" data-id="${esc(p.id)}" aria-pressed="false"><i class="dot ${p.verified ? "" : "off"}"></i>${esc(p.label)}</button>`).join("");
  $("chips").onclick = e => { const c = e.target.closest(".chip"); if (c) { industry = c.dataset.id; applyPreset(); } };
  form.since_date.value = new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
  applyPreset();
  form.brand.oninput = refresh; form.window_days.oninput = refresh;

  document.querySelectorAll(".seg button").forEach(b => b.onclick = () => {
    mode = b.dataset.m;
    document.querySelectorAll(".seg button").forEach(x => x.setAttribute("aria-selected", String(x === b)));
    $("m-xlsx").hidden = mode === "text"; $("m-text").hidden = mode !== "text";
    if (mode === "text") textQuote(); else if (!form.xlsx.files[0]) { channelCount = 0; roughHtml = ""; refresh(); }
  });
  let tq = null;
  const textQuote = () => {
    const n = form.channels_text.value.split("\n").filter(l => l.trim()).length;
    $("textcount").textContent = n ? `채널 ${n}곳` : "";
    channelCount = n; sheetName = "";
    clearTimeout(tq);
    tq = setTimeout(async () => {
      if (!n || n > 300) { roughHtml = ""; refresh(); return; }
      const fd = new FormData(); fd.append("channels", n);
      try { roughHtml = quoteHtml(await (await fetch("/api/quote-preview", {method: "POST", body: fd})).json()); } catch (e) { roughHtml = ""; }
      refresh();
    }, 400);
    refresh();
  };
  form.channels_text.oninput = textQuote;

  const drop = $("drop");
  ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", e => { if (e.dataTransfer.files[0]) { form.xlsx.files = e.dataTransfer.files; form.xlsx.dispatchEvent(new Event("change")); } });
  form.xlsx.onchange = async () => {
    const box = $("mapbox"); mapState = null; box.hidden = true; roughHtml = ""; channelCount = 0;
    const f = form.xlsx.files[0];
    $("dropt").textContent = f ? f.name : "엑셀 파일을 끌어다 놓거나 눌러서 고르세요";
    if (!f) { refresh(); return; }
    const fd = new FormData(); fd.append("xlsx", f);
    try {
      const r = await fetch("/api/xlsx-preview", {method: "POST", body: fd});
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const pv = await r.json();
      channelCount = pv.channels; sheetName = ""; roughHtml = quoteHtml(pv.rough);
      box.hidden = false;
      if (pv.standard) { box.innerHTML = `<div class="notice ok">✔ 템플릿 형식으로 읽었어요 (채널 ${pv.channels}곳)</div>`; refresh(); return; }
      mapState = {header_row: pv.header_row, map: pv.map};
      const head = pv.rows[Math.max(0, pv.header_row - 1)] || pv.rows[0] || [];
      const opts = sel => `<option value="">(사용 안 함)</option>` + Array.from({length: pv.ncols}, (_, i) => `<option value="${i}" ${sel === i ? "selected" : ""}>${i + 1}번째 열 · ${esc(head[i] || "")}</option>`).join("");
      box.innerHTML = `<div class="notice">열 순서가 템플릿과 달라 자동으로 찾았어요 (채널 ${pv.channels}곳). 틀렸다면 고쳐주세요. 한 줄에 유튜버 한 명으로 읽습니다.</div>
        <div class="maprow">${Object.keys(FIELD_LABELS).map(k => `<label>${FIELD_LABELS[k]}<select data-k="${k}">${opts(pv.map[k])}</select></label>`).join("")}</div>
        <div class="tbl-wrap"><table class="mini"><tbody>${pv.rows.map(rw => `<tr>${rw.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
      box.querySelectorAll("select").forEach(s => s.onchange = () => { mapState.map[s.dataset.k] = s.value === "" ? null : +s.value; });
    } catch (err) { box.hidden = false; box.innerHTML = `<div class="notice err">⚠ ${esc(err.message)}</div>`; }
    refresh();
  };

  const refs = $("refs");
  $("addref").onclick = () => {
    if (refs.children.length >= 30) return;
    const d = document.createElement("div");
    d.className = "r";
    d.innerHTML = `<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" aria-label="참고 사진"><input type="text" placeholder="상품명 [색상] (엑셀과 같게)" maxlength="80" aria-label="상품 이름"><button type="button" class="btn quiet">삭제</button>`;
    d.querySelector("button").onclick = () => d.remove();
    refs.appendChild(d);
  };
  form.onsubmit = async e => {
    e.preventDefault();
    const msg = $("msg");
    const fd = new FormData();
    const f0 = form.xlsx.files[0];
    if (mode === "text") fd.append("channels_text", form.channels_text.value);
    else if (f0) { fd.append("xlsx", f0); if (mapState) fd.append("mapping", JSON.stringify(mapState)); }
    else { msg.textContent = "⚠ 엑셀 파일을 고르거나 '주소 붙여넣기'를 선택하세요"; return; }
    fd.append("industry", industry);
    for (const k of ["brand", "product", "product_name", "title_keywords", "window_days", "since_date"]) fd.append(k, form[k].value);
    const labels = [];
    refs.querySelectorAll(".r").forEach(r => {
      const f = r.querySelector("input[type=file]").files[0];
      if (f) { fd.append("refs", f); labels.push(r.querySelector("input[type=text]").value || "상품"); }
    });
    fd.append("ref_labels", JSON.stringify(labels));
    $("go").disabled = true; msg.textContent = "만드는 중…";
    try {
      const r = await fetch("/api/job", {method: "POST", body: fd});
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const {token} = await r.json();
      sessionStorage.setItem("new_token_" + token.slice(0, 8), "1");
      location.hash = "#/j/" + token;
    } catch (err) { msg.textContent = "⚠ " + err.message; $("go").disabled = false; }
  };
}

// ═════════════════════════ 작업 화면 ═════════════════════════
async function jobView(token) {
  try { serverVideo = !!(await (await fetch("/api/config")).json()).server_video; } catch (e) { serverVideo = false; }
  const fresh = sessionStorage.getItem("new_token_" + token.slice(0, 8));
  $app.innerHTML = `
    ${fresh ? `<div class="notice" style="margin-top:0"><b>이 주소를 꼭 저장하세요.</b> 주소를 아는 사람만 이 작업을 열 수 있고, 잃어버리면 다시 찾을 수 없어요.
      <div class="row" style="margin-top:6px"><input type="text" readonly value="${esc(location.href)}" aria-label="작업 주소" style="flex:1 1 300px">
      <button class="btn" id="copy" type="button">주소 복사</button></div></div>` : ""}
    <div class="jobhead" id="jhead"></div>
    <ol class="stepper" id="jstep"></ol>
    <div id="jprog"></div>
    <div class="tabs" role="tablist"><button role="tab" id="t-run" aria-selected="true" data-t="run">실행</button>
      <button role="tab" id="t-rep" aria-selected="false" data-t="report">리포트<span class="cnt" id="repcnt" hidden></span></button></div>
    <section id="tab-run"></section><section id="tab-report" hidden></section>
    <div class="danger-zone noprint"><button class="btn danger" id="del" type="button">이 작업과 데이터 지금 삭제</button>
      <span class="muted small">보관 기간(7일)이 지나면 자동으로 삭제됩니다.</span></div>`;
  if (fresh && $("copy")) $("copy").onclick = async () => { try { await navigator.clipboard.writeText(location.href); $("copy").textContent = "복사했어요"; } catch (e) { $("copy").textContent = "직접 복사하세요"; } };
  $("del").onclick = async () => {
    if (!confirm("작업과 올린 파일, 결과를 모두 삭제합니다. 되돌릴 수 없어요.")) return;
    await api(token, "/api/job", {method: "DELETE"});
    location.hash = "#/";
  };
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => { tabChosen = true; setTab(b.dataset.t); });
  tab = null; job = null; prevRunning = false; tabChosen = false; firstBoard = true;

  async function refresh() {
    let s;
    try { s = await (await api(token, "/api/job")).json(); }
    catch (e) { $app.innerHTML = `<div class="card"><div class="notice err">작업을 찾을 수 없어요. 주소가 맞는지, 보관 기간(7일)이 지나지 않았는지 확인하세요.</div><a class="btn" href="#/">새 작업 만들기</a></div>`; clearInterval(poll); return; }
    job = s;
    const running = RUNNING.includes(s.stage);
    drawHead(s); drawStepper(s); await drawProgress(token, s);
    const key = JSON.stringify([s.stage, s.estimate, s.cap_usd, flash]);
    if (key !== lastState) { lastState = key; drawRun(token, s); loadQuote(token); }  // 바뀐 게 없으면 다시 그리지 않는다(입력 중인 키가 지워지지 않게)
    if (["prepared", "done"].includes(s.stage) || (s.stage === "failed" && board)) await loadBoard(token, s);
    if (prevRunning && !running && s.stage === "done") setTab("report");  // 실행이 끝나면 리포트로 안내
    prevRunning = running;
    if (!running) clearInterval(poll);
  }
  window._refresh = refresh;
  await refresh();
  poll = setInterval(refresh, 2000);
}

function setTab(t) {
  tab = t;
  $("t-run").setAttribute("aria-selected", String(t === "run")); $("t-rep").setAttribute("aria-selected", String(t === "report"));
  $("tab-run").hidden = t !== "run"; $("tab-report").hidden = t !== "report";
}

function drawHead(s) {
  const running = RUNNING.includes(s.stage);
  const cls = s.stage === "failed" || s.stage === "interrupted" ? "bad" : running ? "run" : ["done", "prepared"].includes(s.stage) ? "ok" : "";
  const title = s.brand ? `${esc(s.brand)} 노출 확인` : "노출 확인";
  $("jhead").innerHTML = `<a class="back" href="#/">← 새 작업</a>
    <h1>${title}<span class="sub">${esc(s.industry_label || "")} · 채널 ${s.channels ?? "?"}곳 · 발송일 이후 ${s.window_days ?? "?"}일 · 보관 만료 ${new Date(s.expires * 1000).toLocaleDateString("ko-KR")}</span></h1>
    <span class="status ${cls}"><i></i>${esc(STAGE_KO[s.stage] || s.stage)}</span>`;
}

function drawStepper(s) {
  const prepared = !!s.estimate || ["prepared", "done", "analyzing", "rechecking"].includes(s.stage);
  const hasBoard = !!board;
  const st = (done, now, fail) => (fail ? "fail" : now ? "now" : done ? "done" : "");
  const failed = s.stage === "failed" || s.stage === "interrupted";
  const items = [
    ["준비 분석", st(prepared, s.stage === "preparing", failed && !prepared), prepared ? "완료" : s.stage === "preparing" ? "진행 중" : "시작 전"],
    ["영상 분석", st(false, s.stage === "analyzing", false), s.stage === "analyzing" ? "진행 중" : "선택 · 유료"],
    ["미확인 재검증", st(false, s.stage === "rechecking", false), s.stage === "rechecking" ? "진행 중" : "선택 · 무료 점검"],
    ["리포트", st(hasBoard, false, false), hasBoard ? "볼 수 있어요" : "준비되면 열려요"]];
  $("jstep").innerHTML = items.map((x, i) => `<li class="${x[1]}"><span class="ic">${x[1] === "done" ? "✓" : x[1] === "fail" ? "!" : i + 1}</span><div><b>${esc(x[0])}</b><span>${esc(x[2])}</span></div></li>`).join("");
}

async function drawProgress(token, s) {
  const el = $("jprog");
  const running = RUNNING.includes(s.stage);
  const failed = s.stage === "failed" || s.stage === "interrupted";
  if (!running && !failed && !flash) { el.innerHTML = ""; return; }
  if (!running && !failed) { el.innerHTML = `<div class="notice ok">${esc(flash)}</div>`; return; }
  const p = s.progress || {};
  const elapsed = s.stage_since ? s.now - s.stage_since : 0;
  const hasFrac = p.total && p.done != null;
  const pct = hasFrac ? Math.min(100, Math.round(p.done / p.total * 100)) : null;
  const eta = hasFrac && p.done > 0 && p.done < p.total ? elapsed * (p.total - p.done) / p.done : null;
  let log = "";
  try { log = await (await api(token, "/api/job/log")).text(); } catch (e) { /* 로그는 참고용 */ }
  const open = el.querySelector("details")?.open;
  el.innerHTML = `<section class="card prog ${failed ? "fail" : ""}">
    <div class="head"><h2>${failed ? "멈췄어요" : esc(p.step || STAGE_KO[s.stage] || "진행 중")}</h2>
      <span class="muted">${running ? `경과 ${fmtDur(elapsed)}${eta ? ` · 남은 시간 약 ${fmtDur(eta)}` : ""}` : ""}</span></div>
    ${running ? `<div class="bar ${hasFrac ? "" : "indet"}" role="progressbar" aria-valuemin="0" aria-valuemax="100" ${hasFrac ? `aria-valuenow="${pct}"` : ""}><i ${hasFrac ? `style="width:${pct}%"` : ""}></i></div>
      <div class="muted">${hasFrac ? `<b>${p.done} / ${p.total}</b> (${pct}%)` : "진행률을 계산할 수 없는 단계예요. 끝날 때까지 기다려 주세요."}</div>` : ""}
    ${s.message ? `<p class="${failed ? "notice err" : "muted small"}" style="margin:8px 0 0">${esc(s.message)}</p>` : ""}
    ${failed ? `<p class="muted" style="margin:8px 0 0">같은 단계를 다시 시작하면 이어서 합니다. 키가 틀렸거나 쿼터가 소진됐을 수 있어요.</p>` : ""}
    ${log ? `<details ${open ? "open" : ""}><summary>자세한 로그</summary><pre class="log">${esc(log)}</pre></details>` : ""}</section>`;
}

// ───── 실행 탭
function drawRun(token, s) {
  const running = RUNNING.includes(s.stage);
  const est = s.estimate;
  const canPrep = !running && ["created", "failed", "interrupted", "prepared", "done"].includes(s.stage);
  const canAnalyze = !running && est && ["prepared", "done", "failed", "interrupted"].includes(s.stage);
  const canRecheck = !running && ["prepared", "done"].includes(s.stage);
  const prepared = !!est;
  const tag = (txt, ok) => `<span class="tag ${ok ? "ok" : ""}">${esc(txt)}</span>`;
  $("tab-run").innerHTML = `
    <section class="card"><h2>견적 <span class="muted small">(추정)</span></h2><div id="quotebody" class="muted" style="margin-top:6px">계산 중…</div>
      <label id="ackrow" hidden class="muted blk"><input type="checkbox" id="ack"> 이 업종의 영상 분석은 검증되지 않았음을 이해했고, 그래도 진행합니다</label></section>
    <details class="card stepcard" ${["created", "failed", "interrupted"].includes(s.stage) && !prepared ? "open" : ""}>
      <summary><span class="num">1</span>준비 분석 ${tag(prepared ? "완료" : "먼저 하세요", prepared)}</summary>
      <p class="muted">제목·설명란·댓글에서 브랜드 언급을 찾습니다. YouTube 쿼터만 쓰고 OpenAI 비용은 없어요.</p>
      <label class="f" for="ytkey">YouTube API 키</label><input type="password" id="ytkey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      <div style="margin-top:10px"><button class="btn primary" id="prep" ${canPrep ? "" : "disabled"}>${s.stage === "created" ? "준비 분석 시작" : "다시 실행"}</button></div></details>
    ${serverVideo ? `<details class="card stepcard" ${s.stage === "prepared" ? "open" : ""}>
      <summary><span class="num">2</span>영상 분석 ${tag("선택 · 유료")}</summary>
      ${est ? `<p class="muted">대상 채널 ${est.channels}곳 · 분석 가능한 영상 ${est.videos_all}개 · 전부 분석 시 약 $${est.est_usd_all} (정가 기준 추정)</p>` : `<p class="muted">준비 분석이 끝나면 예상 비용이 나옵니다.</p>`}
      <label class="f" for="cap">비용 상한 (USD)</label><input type="number" id="cap" min="0.5" step="0.5" value="${s.cap_usd || 5}">
      <label class="f" for="oakey">OpenAI API 키</label><input type="password" id="oakey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      <div style="margin-top:10px"><button class="btn primary" id="ana" ${canAnalyze ? "" : "disabled"}>분석 시작</button></div></details>`
    : `<details class="card stepcard" ${s.stage === "prepared" ? "open" : ""}>
      <summary><span class="num">2</span>영상 분석 — 내 PC에서 ${tag("선택 · 유료")}</summary>
      <p class="muted">YouTube가 서버의 영상 다운로드를 막아서 이 단계만 내 컴퓨터에서 실행해요. OpenAI 키는 내 PC에서만 입력하고 서버로 보내지 않습니다.</p>
      <label class="f" for="cap">비용 상한 (USD)</label><input type="number" id="cap" min="0.5" step="0.5" value="${s.cap_usd || 5}">
      <ol class="how muted"><li>아래에서 <b>분석 패키지</b>(zip)를 받아 압축을 풉니다.</li>
        <li>폴더의 <b>읽어보세요.txt</b>대로 준비(Python·ffmpeg)하고 <b>실행.bat</b>을 실행합니다.</li>
        <li>끝나면 생기는 <b>result.json</b>을 아래에서 올립니다.</li></ol>
      <div class="row"><button class="btn primary" id="pkg" ${canAnalyze ? "" : "disabled"}>분석 패키지 받기</button></div>
      <hr class="sep"><label class="f" for="resfile" style="margin-top:0">결과 올리기 <small>result.json</small></label>
      <input type="file" id="resfile" accept=".json,application/json">
      <div class="row" style="margin-top:8px"><button class="btn" id="resup" ${canRecheck ? "" : "disabled"}>결과 올리기</button><span class="muted small" id="resmsg"></span></div></details>`}
    <details class="card stepcard" ${s.stage === "done" ? "open" : ""}>
      <summary><span class="num">3</span>미확인 재검증 ${tag("선택 · 무료 점검")}</summary>
      <p class="muted">미확인 채널만 다시 점검합니다: 댓글 재검토 · 표기 변형 · 설명란 링크 · 분석 서술.${serverVideo ? " 비용 상한을 적으면 아직 안 본 영상도 분석합니다(유료)." : " 안 본 영상의 분석은 ②의 PC 도우미로 해요."}</p>
      ${serverVideo ? `<label class="f" for="rcap">영상 분석 비용 상한 <small>USD · 비우면 무료 점검만</small></label><input type="number" id="rcap" min="0.5" step="0.5" placeholder="예: 5">` : ""}
      <label class="f" for="rytkey">YouTube API 키</label><input type="password" id="rytkey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      ${serverVideo ? `<label class="f" for="roakey">OpenAI API 키 <small>영상 분석할 때만</small></label><input type="password" id="roakey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">` : ""}
      <div class="row" style="margin-top:10px"><button class="btn" id="rplan" ${canRecheck ? "" : "disabled"}>계획 보기</button>
        <button class="btn primary" id="rec" ${canRecheck ? "" : "disabled"}>재검증 시작</button></div>
      <pre class="log" id="rplanout" hidden></pre></details>`;

  const run = async (path, opt, keyInputs) => {
    try { await api(token, path, opt); } catch (e) { alert(e.message); return; }
    keyInputs.forEach(i => { i.value = ""; });  // 키를 화면에서도 지운다
    flash = ""; lastState = "";
    await window._refresh();
    clearInterval(poll); poll = setInterval(window._refresh, 2000);
  };
  const yt = $("ytkey");
  $("prep").onclick = () => run("/api/job/prepare", {method: "POST", headers: {"X-Youtube-Key": yt.value.trim()}}, [yt]);
  $("cap").oninput = () => loadQuote(token);
  const ackForm = fd => { if ($("ack").checked) fd.append("ack_unverified", "1"); return fd; };
  if (serverVideo) {
    const oa = $("oakey");
    $("ana").onclick = () => {
      const fd = ackForm(new FormData()); fd.append("cap_usd", $("cap").value);
      run("/api/job/analyze", {method: "POST", body: fd, headers: {"X-Openai-Key": oa.value.trim()}}, [oa]);
    };
  } else {
    $("pkg").onclick = async () => {
      const fd = ackForm(new FormData()); fd.append("cap_usd", $("cap").value);
      try {
        const blob = await (await api(token, "/api/job/helper-package", {method: "POST", body: fd})).blob();
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "seeding-helper.zip"; a.click();
      } catch (e) { alert(e.message); }
    };
    $("resup").onclick = async () => {
      const f = $("resfile").files[0], msg = $("resmsg");
      if (!f) { msg.textContent = "result.json 파일을 고르세요"; return; }
      const fd = new FormData(); fd.append("result", f);
      msg.textContent = "올리는 중…";
      try {
        const r = await (await api(token, "/api/job/helper-result", {method: "POST", body: fd})).json();
        const skipped = r.rejected_sig + r.rejected_unknown + r.rejected_bad;
        flash = `✔ PC 도우미 결과 영상 ${r.accepted}개를 반영했어요` + (skipped ? ` (제외 ${skipped}개: 설정 불일치 ${r.rejected_sig}, 이 작업에 없는 영상 ${r.rejected_unknown}, 형식 오류 ${r.rejected_bad})` : "");
        lastState = "";
        await window._refresh(); clearInterval(poll); poll = setInterval(window._refresh, 2000);
      } catch (e) { msg.textContent = "⚠ " + e.message; }
    };
  }
  const rcap = $("rcap"), ryt = $("rytkey"), roa = $("roakey");
  const capForm = () => { const fd = ackForm(new FormData()); if (rcap && rcap.value) fd.append("cap_usd", rcap.value); return fd; };
  if (rcap) rcap.oninput = () => loadQuote(token);
  $("rplan").onclick = async () => {
    const out = $("rplanout");
    out.hidden = false; out.textContent = "계산 중… (아무것도 호출하거나 저장하지 않습니다)";
    try { out.textContent = await (await api(token, "/api/job/recheck-plan", {method: "POST", body: capForm()})).text(); }
    catch (e) { out.textContent = "⚠ " + e.message; }
  };
  $("rec").onclick = () => run("/api/job/recheck",
    {method: "POST", body: capForm(), headers: roa ? {"X-Youtube-Key": ryt.value.trim(), "X-Openai-Key": roa.value.trim()} : {"X-Youtube-Key": ryt.value.trim()}},
    roa ? [ryt, roa] : [ryt]);
  if (tab === null) setTab("run");
}

function loadQuote(token) {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(async () => {
    const body = $("quotebody");
    if (!body) return;
    const cap = ($("rcap") || {}).value || ($("cap") || {}).value || "";
    try {
      const q = await (await api(token, "/api/job/quote" + (cap ? "?cap_usd=" + encodeURIComponent(cap) : ""))).json();
      body.className = ""; body.innerHTML = quoteHtml(q);
      $("ackrow").hidden = q.verified;
    } catch (e) { body.textContent = "견적을 계산하지 못했어요: " + e.message; }
  }, 250);
}

// ═════════════════════════ 리포트 탭 ═════════════════════════
async function loadBoard(token, s) {
  const r = await (await api(token, "/api/job/results")).json();
  if (!r.rows) { $("tab-report").innerHTML = `<div class="card empty">아직 리포트가 없어요. 실행 탭에서 준비 분석을 먼저 하세요.</div>`; return; }
  const key = JSON.stringify([s.stage, r.counts, Object.keys(r.records).length, r.rows.map(x => x.st).join()]);
  if (key === boardKey) return;
  boardKey = key; board = r;
  $("repcnt").hidden = false; $("repcnt").textContent = r.rows.length;
  if (firstBoard && s.stage === "done" && !tabChosen) setTab("report");  // 끝난 작업을 다시 열면 리포트부터 보여준다
  firstBoard = false;
  drawReport(token, s);
  drawStepper(s);
}

function counts(rows) { return Object.fromEntries(KINDS.map(k => [k[0], rows.filter(d => k[2](d.st)).length])); }

function summaryText(rows, s) {
  const c = counts(rows), by = st => rows.filter(d => d.st === st).length;
  const parts = [];
  parts.push(`<b>${rows.length}곳</b> 중 <b>${c.ok}곳</b>은 노출이 확인됐고` + (c.ok ? ` (텍스트 ${by("확인됨(텍스트)")} · 댓글 ${by("확인됨(댓글)")} · 대본 ${by("확인됨(대본)")} · 영상 ${by("확인됨(영상)")})` : "") + `, <b>${c.sus}곳</b>은 사람이 확인할 후보예요.`);
  parts.push(`<b>${c.unk}곳</b>은 단서를 찾지 못했어요. <b>"안 올렸다"가 아니라 "도구가 못 찾았다"</b>는 뜻이라, 필요하면 재검증이나 직접 확인이 필요해요.`);
  if (c.none || c.err) parts.push(`${c.none ? `발송 후 업로드가 없는 채널 ${c.none}곳` : ""}${c.none && c.err ? ", " : ""}${c.err ? `채널을 찾지 못한 곳 ${c.err}곳` : ""}이 있어요.`);
  return parts.join(" ");
}

function rowHtml(d, r, withId) {
  const k = kindOf(d.st), v = safeUrl(d.vid), ch = safeUrl(d.url);
  const why = [d.ev, d.note].filter(Boolean).join(" — ");
  return `<tr ${withId ? `id="row-${esc(d.n)}"` : ""} class="${hitRow == d.n ? "hit" : ""}"><td class="n">${esc(d.n)}</td>
    <td class="who">${ch ? `<a href="${ch}" target="_blank" rel="noopener noreferrer">${esc(d.name)}</a>` : esc(d.name)}<div class="muted small">${esc(d.prod).slice(0, 70)}</div></td>
    <td><span class="pill s-${k}">${esc(d.st)}</span></td>
    <td>${why ? `<div class="clamp" title="${esc(why)}">${esc(why)}</div>` : `<span class="muted small">—</span>`}${v ? `<a class="small" href="${v}" target="_blank" rel="noopener noreferrer">근거 영상 열기 ↗</a>` : ""}</td>
    <td><select data-n="${esc(d.n)}" aria-label="확인 결과">${REC.map(o => `<option ${r.records[d.n] === o ? "selected" : ""}>${o}</option>`).join("")}</select></td></tr>`;
}

function miniTable(rows, r, withId) {
  return `<div class="tbl-wrap"><table class="t"><thead><tr><th>#</th><th>유튜버</th><th>판정</th><th>근거</th><th>확인 결과</th></tr></thead><tbody>${rows.map(d => rowHtml(d, r, withId)).join("")}</tbody></table></div>`;
}

function drawReport(token, s) {
  const r = board, rows = r.rows, c = counts(rows);
  const q = query.toLowerCase();
  const shown = rows.filter(d => (filter === "all" || kindOf(d.st) === filter) && (!q || (d.n + " " + d.name + " " + d.prod + " " + d.ev).toLowerCase().includes(q)));
  const confirmed = rows.filter(d => kindOf(d.st) === "ok"), suspects = rows.filter(d => kindOf(d.st) === "sus");
  $("tab-report").innerHTML = `
    <section class="card"><div class="rep-head"><div><h2>판정 리포트</h2>
        <p class="muted small" style="margin:2px 0 0">${esc(s.brand || "")} · ${esc(s.industry_label || "")} · 채널 ${rows.length}곳 · ${new Date().toLocaleDateString("ko-KR")} 기준</p></div>
      <div class="row noprint"><button class="btn" id="csv">확인 기록 CSV</button><button class="btn" id="print">인쇄 / PDF</button></div></div>
      <p class="rep-sum">${summaryText(rows, s)}</p>
      <div class="map" id="map" role="group" aria-label="채널별 판정">${rows.map(d => { const k = kindOf(d.st); return `<button type="button" class="cell s-${k} ${filter !== "all" && filter !== k ? "dim" : ""}" data-n="${esc(d.n)}" title="#${esc(d.n)} ${esc(d.name)} · ${esc(d.st)}" aria-label="${esc(d.n)}번 ${esc(d.name)} ${esc(d.st)}"></button>`; }).join("")}</div>
      <div class="legend noprint">${KINDS.map(k => `<button type="button" class="lg" data-k="${k[0]}" aria-pressed="${filter === k[0]}"><i class="sw s-${k[0]}"></i>${k[1]} <b>${c[k[0]]}</b></button>`).join("")}
        <button type="button" class="lg" data-k="all" aria-pressed="${filter === "all"}">전체 <b>${rows.length}</b></button></div></section>
    ${confirmed.length ? `<section class="card"><h2>노출이 확인된 채널 <span class="muted small">${confirmed.length}곳</span></h2>${miniTable(confirmed, r)}</section>` : ""}
    ${suspects.length ? `<section class="card"><h2>사람이 확인할 후보 <span class="muted small">${suspects.length}곳 · 근거 영상을 열어 실제로 나오는지 확인하세요</span></h2>${miniTable(suspects, r)}</section>` : ""}
    <section class="card"><h2>전체 목록 <span class="muted small">${shown.length}곳 표시</span></h2>
      <div class="filters noprint"><input type="text" id="q" placeholder="번호·이름·상품·근거 검색" value="${esc(query)}" aria-label="검색"></div>
      ${shown.length ? miniTable(shown, r, true) : `<div class="empty">조건에 맞는 채널이 없어요.</div>`}</section>`;
  const root = $("tab-report");
  root.querySelectorAll(".lg").forEach(b => b.onclick = () => { filter = b.dataset.k === filter ? "all" : b.dataset.k; hitRow = null; drawReport(token, s); });
  root.querySelectorAll(".cell").forEach(b => b.onclick = () => {
    filter = "all"; query = ""; hitRow = b.dataset.n; drawReport(token, s);
    const row = $("row-" + hitRow); if (row) row.scrollIntoView({block: "center", behavior: "smooth"});
  });
  const qi = root.querySelector("#q");
  qi.oninput = e => { query = e.target.value; hitRow = null; drawReport(token, s); const i = $("tab-report").querySelector("#q"); i.focus(); i.setSelectionRange(query.length, query.length); };
  root.querySelectorAll("select[data-n]").forEach(sel => sel.onchange = async () => {
    try {
      await api(token, "/api/job/records/" + sel.dataset.n, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({value: sel.value})});
      r.records[sel.dataset.n] = sel.value;
      root.querySelectorAll(`select[data-n="${sel.dataset.n}"]`).forEach(o => { o.value = sel.value; });  // 같은 채널이 여러 표에 있으면 같이 맞춘다
    } catch (e) { alert(e.message); }
  });
  $("csv").onclick = async () => {
    const blob = await (await api(token, "/api/job/export", {method: "POST"})).blob();
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "확인기록.csv"; a.click();
  };
  $("print").onclick = () => window.print();
}

$("theme").onclick = () => {
  const r = document.documentElement;
  const dark = r.dataset.theme === "dark" || (!r.dataset.theme && matchMedia("(prefers-color-scheme:dark)").matches);
  r.dataset.theme = dark ? "light" : "dark";
};
window.addEventListener("hashchange", () => { boardKey = ""; board = null; lastState = ""; flash = ""; filter = "all"; query = ""; hitRow = null; tab = null; route(); });
route();
