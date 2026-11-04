"use strict";
// API 키는 이 파일의 변수에도, 브라우저 저장소에도 남기지 않는다. 요청 헤더로만 보내고 입력칸을 비운다.
const $app = document.getElementById("app");
const RUNNING = ["queued", "preparing", "analyzing", "rechecking"];
const STAGE_KO = {created: "작업 만듦", queued: "순서 대기", preparing: "준비 분석 중", prepared: "준비 완료",
  analyzing: "영상 분석 중", rechecking: "재검증 중", done: "분석 완료", failed: "실패", interrupted: "중단됨"};
const REC = ["", "착용 확인", "업로드했으나 미착용", "업로드 안 함", "확인 불가"];
const GROUPS = [["전체", null], ["확인됨", s => s.startsWith("확인됨")], ["의심", s => s.startsWith("의심")],
  ["미확인", s => s === "미확인"], ["업로드 없음", s => s === "업로드 없음"], ["채널 오류", s => s === "채널 오류"]];
let poll = null, board = null, grp = 0, query = "";

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => (/^https:\/\/(www\.)?(youtube\.com|youtu\.be)\//.test(u) ? esc(u) : "");
const cls = s => s.startsWith("확인됨") ? "b-ok" : s.startsWith("의심") ? "b-sus" : s === "미확인" ? "b-unk" : s === "업로드 없음" ? "b-none" : "b-err";

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

// ───── 새 작업 화면
function homeView() {
  $app.innerHTML = `
  <div class="card"><h2>새 작업 만들기</h2>
    <p class="muted">시딩 리스트(엑셀)를 올리면, 채널별로 발송일 이후 영상을 모아 제품 노출 여부를 1차로 골라줍니다.</p>
    <div class="notice">업로드한 리스트에는 제3자 정보가 들어 있을 수 있습니다. 서버에는 <b>7일</b> 보관되고 언제든 직접 삭제할 수 있습니다. API 키는 저장하지 않습니다.</div>
    <form id="f">
      <label>시딩 리스트 (xlsx, 5MB 이하)</label>
      <input type="file" name="xlsx" accept=".xlsx" required>
      <p class="muted">3행부터 읽습니다. 열: 발송일 · 번호 · 유튜브 아이디 · 상품명[색상] · 옵션 · 채널 링크</p>
      <label>브랜드명 (쉼표로 구분, 표기 변형도 함께)</label>
      <input type="text" name="brand" placeholder="예: 브랜드명, brandname" required maxlength="400">
      <label>상품명 키워드 (선택, 쉼표로 구분)</label>
      <input type="text" name="product" placeholder="예: 풋볼져지, 크롭티" maxlength="600">
      <label>상품 참고 사진 (선택, 최대 30장)</label>
      <div class="reflist" id="refs"></div>
      <button type="button" id="addref" class="ghost">+ 사진 추가</button>
      <div class="row" style="margin-top:16px"><button class="primary" type="submit">작업 만들기</button><span id="msg" class="muted"></span></div>
    </form></div>`;
  const refs = document.getElementById("refs");
  document.getElementById("addref").onclick = () => {
    if (refs.children.length >= 30) return;
    const d = document.createElement("div");
    d.className = "r";
    d.innerHTML = `<input type="file" accept="image/png,image/jpeg,image/webp,image/gif"><input type="text" placeholder="상품명 [색상] (엑셀과 같게)" maxlength="80"><button type="button" class="ghost">삭제</button>`;
    d.querySelector("button").onclick = () => d.remove();
    refs.appendChild(d);
  };
  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    const msg = document.getElementById("msg");
    const fd = new FormData();
    fd.append("xlsx", e.target.xlsx.files[0]);
    fd.append("brand", e.target.brand.value);
    fd.append("product", e.target.product.value);
    const labels = [];
    refs.querySelectorAll(".r").forEach(r => {
      const f = r.querySelector("input[type=file]").files[0];
      if (f) { fd.append("refs", f); labels.push(r.querySelector("input[type=text]").value || "상품"); }
    });
    fd.append("ref_labels", JSON.stringify(labels));
    msg.textContent = "올리는 중…";
    try {
      const r = await fetch("/api/job", {method: "POST", body: fd});
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const {token} = await r.json();
      sessionStorage.setItem("new_token_" + token.slice(0, 8), "1");
      location.hash = "#/j/" + token;
    } catch (err) { msg.textContent = "⚠ " + err.message; }
  };
}

// ───── 작업 화면
async function jobView(token) {
  const fresh = sessionStorage.getItem("new_token_" + token.slice(0, 8));
  const url = location.href;
  $app.innerHTML = `
  ${fresh ? `<div class="notice"><b>이 주소를 꼭 저장하세요.</b> 주소를 아는 사람만 이 작업을 열 수 있고, 잃어버리면 다시 찾을 수 없습니다.<div class="tokenbox">${esc(url)}</div></div>` : ""}
  <div class="card" id="head"></div>
  <div class="steps" id="steps"></div>
  <div class="card" id="logcard" hidden><h3>진행 로그</h3><pre class="log" id="log"></pre></div>
  <div id="board"></div>
  <div class="card"><div class="row"><button class="danger" id="del" type="button">이 작업과 데이터 지금 삭제</button>
  <span class="muted">보관 기간이 지나면 자동으로 삭제됩니다.</span></div></div>`;
  document.getElementById("del").onclick = async () => {
    if (!confirm("작업과 업로드한 파일, 결과를 모두 삭제합니다. 되돌릴 수 없습니다.")) return;
    await api(token, "/api/job", {method: "DELETE"});
    location.hash = "#/";
  };
  async function refresh() {
    let s;
    try { s = await (await api(token, "/api/job")).json(); }
    catch (e) { $app.innerHTML = `<div class="card"><div class="notice err">작업을 찾을 수 없습니다. 주소가 맞는지, 보관 기간(7일)이 지나지 않았는지 확인하세요.</div><a href="#/">새 작업 만들기</a></div>`; clearInterval(poll); return; }
    renderState(token, s);
    const running = RUNNING.includes(s.stage);
    document.getElementById("logcard").hidden = !(running || s.stage === "failed");
    if (running || s.stage === "failed") document.getElementById("log").textContent = await (await api(token, "/api/job/log")).text();
    if (!running) clearInterval(poll);
    if (["prepared", "done"].includes(s.stage) || (s.stage === "failed" && board)) await loadBoard(token, s);
  }
  await refresh();
  poll = setInterval(refresh, 2500);
  window._refresh = refresh;
}

function renderState(token, s) {
  const running = RUNNING.includes(s.stage);
  document.getElementById("head").innerHTML = `<div class="row"><h2 style="margin:0">작업 상태</h2><span class="badge ${s.stage === "failed" ? "b-err" : running ? "b-sus" : "b-ok"}">${esc(STAGE_KO[s.stage] || s.stage)}</span></div>
    ${s.message && (running || s.stage === "failed" || s.stage === "interrupted") ? `<p class="${s.stage === "failed" ? "notice err" : "muted"}">${esc(s.message)}</p>` : ""}
    <p class="muted">보관 만료: ${new Date(s.expires * 1000).toLocaleString("ko-KR")}</p>`;
  const est = s.estimate;
  const canPrep = !running && ["created", "failed", "interrupted", "prepared", "done"].includes(s.stage);
  const canAnalyze = !running && est && ["prepared", "done", "failed", "interrupted"].includes(s.stage);
  const canRecheck = !running && ["prepared", "done"].includes(s.stage);
  document.getElementById("steps").innerHTML = `
    <div class="step ${["created"].includes(s.stage) ? "on" : ""}"><h3>① 준비 분석</h3>
      <p class="muted">제목·설명란·댓글에서 브랜드 언급을 찾습니다. YouTube 쿼터만 쓰고 OpenAI 비용은 없습니다.</p>
      <label>YouTube API 키</label><input type="password" id="ytkey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      <div style="margin-top:8px"><button class="primary" id="prep" ${canPrep ? "" : "disabled"}>${s.stage === "created" ? "준비 분석 시작" : "다시 실행"}</button></div></div>
    <div class="step ${s.stage === "prepared" ? "on" : ""}"><h3>② 영상 분석 (유료)</h3>
      ${est ? `<p class="muted">대상 채널 ${est.channels}곳 · 분석 가능한 영상 ${est.videos_all}개 · 전부 분석 시 약 $${est.est_usd_all} (정가 기준 추정)</p>` : `<p class="muted">① 준비 분석이 끝나면 예상 비용이 나옵니다.</p>`}
      <label>비용 상한 (USD)</label><input type="number" id="cap" min="0.5" step="0.5" value="${s.cap_usd || 5}">
      <label>OpenAI API 키</label><input type="password" id="oakey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      <p class="muted">상한을 넘지 않게 채널당 분석할 영상 수를 자동으로 줄입니다. 영상당 약 $${est ? est.cost_per_video : 0.03} 추정이며 실제 비용은 다를 수 있습니다.</p>
      <div style="margin-top:8px"><button class="primary" id="ana" ${canAnalyze ? "" : "disabled"}>분석 시작</button></div></div>
    <div class="step"><h3>③ 미확인 재검증</h3>
      <p class="muted">미확인 채널만 자막에서 브랜드 언급을 다시 찾습니다. 키는 필요 없습니다.</p>
      <div style="margin-top:8px"><button id="rec" ${canRecheck ? "" : "disabled"}>재검증 시작</button></div></div>`;
  const run = async (path, opt, keyInputs) => {
    try { await api(token, path, opt); } catch (e) { alert(e.message); return; }
    keyInputs.forEach(i => { i.value = ""; });  // 키를 화면에서도 지운다
    await window._refresh();
    clearInterval(poll); poll = setInterval(window._refresh, 2500);
  };
  const yt = document.getElementById("ytkey"), oa = document.getElementById("oakey");
  document.getElementById("prep").onclick = () => run("/api/job/prepare", {method: "POST", headers: {"X-Youtube-Key": yt.value.trim()}}, [yt]);
  document.getElementById("ana").onclick = () => {
    const fd = new FormData(); fd.append("cap_usd", document.getElementById("cap").value);
    run("/api/job/analyze", {method: "POST", body: fd, headers: {"X-Openai-Key": oa.value.trim()}}, [oa]);
  };
  document.getElementById("rec").onclick = () => run("/api/job/recheck", {method: "POST"}, []);
}

// ───── 보드
let boardKey = "";
async function loadBoard(token, s) {
  const r = await (await api(token, "/api/job/results")).json();
  if (!r.rows) { document.getElementById("board").innerHTML = ""; return; }
  const key = JSON.stringify([s.stage, r.counts, Object.keys(r.records).length, r.rows.map(x => x.st).join()]);
  if (key === boardKey) return;
  boardKey = key; board = r;
  drawBoard(token);
}

function drawBoard(token) {
  const r = board, el = document.getElementById("board");
  const f = GROUPS[grp][1], q = query.toLowerCase();
  const rows = r.rows.filter(d => (!f || f(d.st)) && (!q || (d.n + " " + d.name + " " + d.prod + " " + d.ev).toLowerCase().includes(q)));
  el.innerHTML = `<div class="card"><h2>판정 보드</h2>
    <div class="stats">${GROUPS.map(([l, fn], i) => `<button class="stat ${i === grp ? "on" : ""}" data-i="${i}"><b>${fn ? r.rows.filter(d => fn(d.st)).length : r.rows.length}</b><span>${l}</span></button>`).join("")}</div>
    <div class="bar"><input type="text" id="q" placeholder="번호·이름·상품·근거 검색" value="${esc(query)}"><button id="csv">확인 기록 CSV</button></div>
    <p class="muted">${rows.length}명 표시</p>
    ${rows.map(d => {
      const v = safeUrl(d.vid), ch = safeUrl(d.url);
      return `<div class="item"><div class="num">#${esc(d.n)}</div>
      <div><div class="nm">${ch ? `<a href="${ch}" target="_blank" rel="noopener noreferrer">${esc(d.name)}</a>` : esc(d.name)}</div>
      <div class="muted">${esc(d.prod)} · 발송 ${esc(d.date)} · 이후 영상 ${esc(d.cnt)}개 ${d.scope ? "· " + esc(d.scope) : ""}</div></div>
      <span class="badge ${cls(d.st)}">${esc(d.st)}</span>
      <div class="det">${d.ev ? "근거: " + esc(d.ev) + "<br>" : ""}${v ? `<a href="${v}" target="_blank" rel="noopener noreferrer">근거 영상 열기 ↗</a> ` : ""}${d.note ? `<div class="note">${esc(d.note)}</div>` : ""}</div>
      <div class="rec">확인 결과 <select data-n="${esc(d.n)}">${REC.map(o => `<option ${r.records[d.n] === o ? "selected" : ""}>${o}</option>`).join("")}</select></div></div>`;
    }).join("")}</div>`;
  el.querySelectorAll(".stat").forEach(b => b.onclick = () => { grp = +b.dataset.i; drawBoard(token); });
  el.querySelector("#q").oninput = e => { query = e.target.value; drawBoard(token); const i = el.querySelector("#q"); i.focus(); i.setSelectionRange(query.length, query.length); };
  el.querySelectorAll(".rec select").forEach(sel => sel.onchange = async () => {
    try {
      await api(token, "/api/job/records/" + sel.dataset.n, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({value: sel.value})});
      r.records[sel.dataset.n] = sel.value;
    } catch (e) { alert(e.message); }
  });
  el.querySelector("#csv").onclick = async () => {
    const blob = await (await api(token, "/api/job/export", {method: "POST"})).blob();
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "확인기록.csv"; a.click();
  };
}

document.getElementById("theme").onclick = () => {
  const r = document.documentElement;
  const dark = r.dataset.theme === "dark" || (!r.dataset.theme && matchMedia("(prefers-color-scheme:dark)").matches);
  r.dataset.theme = dark ? "light" : "dark";
};
window.addEventListener("hashchange", () => { boardKey = ""; board = null; route(); });
route();
