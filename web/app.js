"use strict";
// API 키는 이 파일의 변수에도, 브라우저 저장소에도 남기지 않는다. 요청 헤더로만 보내고 입력칸을 비운다.
const $app = document.getElementById("app");
const RUNNING = ["queued", "preparing", "analyzing", "rechecking"];
const STAGE_KO = {created: "작업 만듦", queued: "순서 대기", preparing: "준비 분석 중", prepared: "준비 완료",
  analyzing: "영상 분석 중", rechecking: "재검증 중", done: "분석 완료", failed: "실패", interrupted: "중단됨"};
const REC = ["", "착용 확인", "업로드했으나 미착용", "업로드 안 함", "확인 불가"];
const GROUPS = [["전체", null], ["확인됨", s => s.startsWith("확인됨")], ["의심", s => s.startsWith("의심")],
  ["미확인", s => s === "미확인"], ["업로드 없음", s => s === "업로드 없음"], ["채널 오류", s => s === "채널 오류"]];
let poll = null, board = null, grp = 0, query = "", lastState = "", serverVideo = false, flash = "";

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
let presets = [];
const FIELD_LABELS = {date: "발송일", no: "번호", name: "이름", product: "상품", option: "옵션", url: "채널 링크"};

function quoteHtml(q) {
  if (!q) return "";
  const a = q.analysis || {}, p = q.prepare || {}, r = q.recheck || {};
  const rows = [];
  if (q.kind === "rough") {
    rows.push(["준비 분석 (YouTube 쿼터)", `약 ${(p.yt_quota || 0).toLocaleString()} / 일일 10,000`, "OpenAI 비용 없음"]);
    rows.push(["영상 분석 (유료)", `약 $${a.usd} · ${a.time} · 영상 약 ${a.videos}개`, a.note]);
  } else {
    rows.push(["영상 분석 (유료)", a.videos === a.videos_all
      ? `전부 분석하면 약 $${a.usd} · ${a.time} · 영상 ${a.videos}개`
      : `상한 안에서 약 $${a.usd} · ${a.time} · 영상 ${a.videos}개 (전부 분석하면 $${a.usd_all} · ${a.time_all} · ${a.videos_all}개)`, a.note]);
    rows.push(["무료 재검증 (YouTube 쿼터)", `댓글 ${r.yt_quota.toLocaleString()} · 링크 ${r.links_followed.toLocaleString()}개 따라가기`, r.note]);
  }
  return `<table class="quote"><tbody>${rows.map(x => `<tr><th>${esc(x[0])}</th><td>${esc(x[1])}</td><td class="muted">${esc(x[2] || "")}</td></tr>`).join("")}</tbody></table>
    <p class="muted">${esc(q.basis || "")}</p>`;
}

async function homeView() {
  if (!presets.length) { try { presets = await (await fetch("/api/presets")).json(); } catch (e) { presets = []; } }
  const opts = presets.map(p => `<option value="${esc(p.id)}">${esc(p.label)}${p.verified ? "" : " (영상 분석 미검증)"}</option>`).join("");
  $app.innerHTML = `
  <div class="card"><h2>새 작업 만들기</h2>
    <p class="muted">채널별로 기간 안의 영상을 모아 제품·브랜드 노출 여부를 1차로 골라줍니다.</p>
    <div class="notice">업로드한 리스트에는 제3자 정보가 들어 있을 수 있습니다. 서버에는 <b>7일</b> 보관되고 언제든 직접 삭제할 수 있습니다. API 키는 저장하지 않습니다.</div>
    <form id="f">
      <label>업종 (키워드 등 임시값을 채워줍니다. 모두 고칠 수 있어요)</label>
      <select name="industry" id="industry">${opts}</select>
      <p class="muted" id="indnote"></p>
      <label>입력 방식</label>
      <div class="row"><label style="margin:0;font-weight:400"><input type="radio" name="mode" value="xlsx" checked> 엑셀 올리기</label>
      <label style="margin:0;font-weight:400"><input type="radio" name="mode" value="text"> 채널 주소 붙여넣기</label>
      <a href="/api/template.xlsx" download>엑셀 템플릿 내려받기</a></div>
      <div id="m-xlsx">
        <input type="file" name="xlsx" accept=".xlsx">
        <p class="muted">열 순서가 달라도 됩니다. 올리면 열을 자동으로 찾아줍니다. 한 줄에 유튜버 한 명, 5MB 이하.</p>
        <div id="mapbox" hidden></div>
      </div>
      <div id="m-text" hidden>
        <textarea name="channels_text" rows="5" placeholder="한 줄에 하나씩&#10;@핸들&#10;https://www.youtube.com/@핸들&#10;채널이름 https://www.youtube.com/@핸들"></textarea>
        <p class="muted" id="textcount"></p>
      </div>
      <label>브랜드명 (쉼표로 구분, 표기 변형도 함께)</label>
      <input type="text" name="brand" placeholder="예: 브랜드명, brandname" required maxlength="400">
      <label>상품명 키워드 (선택, 쉼표로 구분)</label>
      <input type="text" name="product" id="product" maxlength="600">
      <label>상품 이름 (엑셀에 상품 열이 없을 때 쓰는 이름)</label>
      <input type="text" name="product_name" value="상품" maxlength="60">
      <label>노출 가능성이 높은 영상의 제목 키워드 (이 단어가 있는 영상을 먼저 분석)</label>
      <textarea name="title_keywords" id="titlekw" rows="2" maxlength="600"></textarea>
      <div class="row"><div><label>수집 기간 (발송일 이후 며칠)</label><input type="number" name="window_days" id="window" min="7" max="365"></div>
      <div><label>발송일이 없을 때 시작일</label><input type="date" name="since_date" id="since"></div></div>
      <div id="roughquote"></div>
      <label>상품 참고 사진 (선택, 최대 30장)</label>
      <div class="reflist" id="refs"></div>
      <button type="button" id="addref" class="ghost">+ 사진 추가</button>
      <div class="row" style="margin-top:16px"><button class="primary" type="submit">작업 만들기</button><span id="msg" class="muted"></span></div>
    </form></div>`;
  const form = document.getElementById("f");
  const applyPreset = () => {
    const p = presets.find(x => x.id === form.industry.value) || presets[0];
    if (!p) return;
    form.title_keywords.value = p.title_keywords.join(", ");
    form.window_days.value = p.window_days;
    form.product.placeholder = p.product_example;
    document.getElementById("indnote").innerHTML = p.verified
      ? `영상에서 볼 것: ${esc(p.look_for)}. <b>실제 데이터(93명)로 검증된 업종</b>입니다.`
      : `영상에서 볼 것: ${esc(p.look_for)}. <b>⚠ 영상 분석은 아직 이 업종에서 검증되지 않았습니다</b>(분석 기준이 의류 착용입니다). 제목·설명란·댓글·링크 점검은 쓸 수 있습니다.`;
  };
  form.industry.onchange = applyPreset;
  const d90 = new Date(Date.now() - 90 * 864e5); form.since_date.value = d90.toISOString().slice(0, 10);
  applyPreset();
  let mapState = null;  // 열 연결: {header_row, map}
  const showMode = () => {
    const text = form.mode.value === "text";
    document.getElementById("m-xlsx").hidden = text; document.getElementById("m-text").hidden = !text;
    if (text) showTextQuote(); else if (!form.xlsx.files[0]) document.getElementById("roughquote").innerHTML = "";
  };
  form.querySelectorAll("input[name=mode]").forEach(r => r.onchange = showMode);
  let tq = null;
  const showTextQuote = () => {
    const n = form.channels_text.value.split("\n").filter(l => l.trim()).length;
    document.getElementById("textcount").textContent = n ? `채널 ${n}곳` : "";
    clearTimeout(tq);
    tq = setTimeout(async () => {
      if (!n || n > 300) { document.getElementById("roughquote").innerHTML = ""; return; }
      const fd = new FormData(); fd.append("channels", n);
      try { document.getElementById("roughquote").innerHTML = "<h3>대략의 견적</h3>" + quoteHtml(await (await fetch("/api/quote-preview", {method: "POST", body: fd})).json()); } catch (e) { /* 견적은 참고용 */ }
    }, 400);
  };
  form.channels_text.oninput = showTextQuote;
  form.xlsx.onchange = async () => {
    const box = document.getElementById("mapbox"); mapState = null; box.hidden = true;
    const f = form.xlsx.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("xlsx", f);
    try {
      const r = await fetch("/api/xlsx-preview", {method: "POST", body: fd});
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const pv = await r.json();
      document.getElementById("roughquote").innerHTML = `<h3>대략의 견적 (채널 ${pv.channels}곳)</h3>` + quoteHtml(pv.rough);
      if (pv.standard) { box.hidden = false; box.innerHTML = `<p class="muted">✔ 템플릿 형식으로 읽었습니다. 채널 ${pv.channels}곳.</p>`; return; }
      mapState = {header_row: pv.header_row, map: pv.map};
      const colOpts = sel => `<option value="">(사용 안 함)</option>` + Array.from({length: pv.ncols}, (_, i) => `<option value="${i}" ${sel === i ? "selected" : ""}>${i + 1}번째 열: ${esc((pv.rows[Math.max(0, pv.header_row - 1)] || [])[i] || (pv.rows[0] || [])[i] || "")}</option>`).join("");
      box.hidden = false;
      box.innerHTML = `<p class="muted">열 순서가 템플릿과 달라서 자동으로 찾았습니다. 틀리면 고쳐주세요 (채널 ${pv.channels}곳 인식, 한 줄에 유튜버 한 명으로 읽습니다).</p>
        <div class="row">${Object.keys(FIELD_LABELS).map(k => `<label style="margin:0;font-weight:400">${FIELD_LABELS[k]}<select data-k="${k}">${colOpts(pv.map[k])}</select></label>`).join("")}</div>
        <div style="overflow:auto"><table class="quote"><tbody>${pv.rows.map(rw => `<tr>${rw.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
      box.querySelectorAll("select").forEach(s => s.onchange = () => { mapState.map[s.dataset.k] = s.value === "" ? null : +s.value; });
    } catch (err) { box.hidden = false; box.innerHTML = `<div class="notice err">⚠ ${esc(err.message)}</div>`; }
  };
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
    const f0 = e.target.xlsx.files[0];
    if (e.target.mode.value === "text") fd.append("channels_text", e.target.channels_text.value);
    else if (f0) { fd.append("xlsx", f0); if (mapState) fd.append("mapping", JSON.stringify(mapState)); }
    else { msg.textContent = "⚠ 엑셀 파일을 고르거나 '채널 주소 붙여넣기'를 선택하세요"; return; }
    for (const k of ["brand", "product", "product_name", "industry", "title_keywords", "window_days", "since_date"]) fd.append(k, e.target[k].value);
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
  try { serverVideo = !!(await (await fetch("/api/config")).json()).server_video; } catch (e) { serverVideo = false; }
  const fresh = sessionStorage.getItem("new_token_" + token.slice(0, 8));
  const url = location.href;
  $app.innerHTML = `
  ${fresh ? `<div class="notice"><b>이 주소를 꼭 저장하세요.</b> 주소를 아는 사람만 이 작업을 열 수 있고, 잃어버리면 다시 찾을 수 없습니다.<div class="tokenbox">${esc(url)}</div></div>` : ""}
  <div class="card" id="head"></div>
  <div class="card" id="quotecard"><h3>견적 (추정)</h3><div id="quotebody" class="muted">계산 중…</div>
    <label id="ackrow" hidden style="font-weight:400"><input type="checkbox" id="ack"> 이 업종의 영상 분석은 검증되지 않았음을 이해했고, 그래도 진행합니다</label></div>
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
    const stateKey = JSON.stringify([s.stage, s.message, s.estimate, s.cap_usd]);
    if (stateKey !== lastState) { lastState = stateKey; renderState(token, s); loadQuote(token); }  // 바뀐 게 없으면 다시 그리지 않는다(입력 중인 키·계획 출력이 지워지지 않게)
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
    ${flash ? `<p class="notice">${esc(flash)}</p>` : ""}
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
    ${serverVideo ? `<div class="step ${s.stage === "prepared" ? "on" : ""}"><h3>② 영상 분석 (유료)</h3>
      ${est ? `<p class="muted">대상 채널 ${est.channels}곳 · 분석 가능한 영상 ${est.videos_all}개 · 전부 분석 시 약 $${est.est_usd_all} (정가 기준 추정)</p>` : `<p class="muted">① 준비 분석이 끝나면 예상 비용이 나옵니다.</p>`}
      <label>비용 상한 (USD)</label><input type="number" id="cap" min="0.5" step="0.5" value="${s.cap_usd || 5}">
      <label>OpenAI API 키</label><input type="password" id="oakey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      <p class="muted">상한을 넘지 않게 채널당 분석할 영상 수를 자동으로 줄입니다. 영상당 약 $${est ? est.cost_per_video : 0.03} 추정이며 실제 비용은 다를 수 있습니다.</p>
      <div style="margin-top:8px"><button class="primary" id="ana" ${canAnalyze ? "" : "disabled"}>분석 시작</button></div></div>`
    : `<div class="step ${s.stage === "prepared" ? "on" : ""}"><h3>② 영상 분석 (유료 · 내 PC에서)</h3>
      <p class="muted">YouTube가 서버의 영상 다운로드를 막아서, 이 단계만 내 컴퓨터에서 실행합니다. 키는 내 PC에서만 입력하고 서버로 보내지 않습니다.</p>
      <label>비용 상한 (USD)</label><input type="number" id="cap" min="0.5" step="0.5" value="${s.cap_usd || 5}">
      <ol class="muted" style="padding-left:18px;margin:8px 0">
        <li>아래에서 <b>분석 패키지</b>(zip)를 받아 압축을 풉니다.</li>
        <li>폴더의 <b>읽어보세요.txt</b>대로 준비(Python·ffmpeg)하고 <b>실행.bat</b>을 실행합니다. OpenAI 키는 그 창에서 입력합니다.</li>
        <li>끝나면 생기는 <b>result.json</b>을 아래에서 올립니다.</li></ol>
      <div class="row"><button class="primary" id="pkg" ${canAnalyze ? "" : "disabled"}>분석 패키지 받기</button></div>
      <label>결과 올리기 (result.json)</label><input type="file" id="resfile" accept=".json,application/json">
      <div class="row" style="margin-top:8px"><button id="resup" ${canRecheck ? "" : "disabled"}>결과 올리기</button><span class="muted" id="resmsg"></span></div></div>`}
    <div class="step"><h3>③ 미확인 재검증</h3>
      <p class="muted">미확인 채널만 다시 점검합니다: 댓글 재검토 · 표기 변형 · 설명란 링크 · 분석 서술.${serverVideo ? " 비용 상한을 적으면 아직 안 본 영상도 분석합니다(유료)." : " 안 본 영상의 분석은 ②의 PC 도우미로 합니다."}</p>
      ${serverVideo ? `<label>영상 분석 비용 상한 (USD, 비우면 무료 점검만)</label><input type="number" id="rcap" min="0.5" step="0.5" placeholder="예: 5">` : ""}
      <label>YouTube API 키</label><input type="password" id="rytkey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">
      ${serverVideo ? `<label>OpenAI API 키 (영상 분석할 때만)</label><input type="password" id="roakey" autocomplete="off" spellcheck="false" placeholder="저장되지 않습니다">` : ""}
      <div class="row" style="margin-top:8px"><button id="rplan" ${canRecheck ? "" : "disabled"}>계획 보기</button>
      <button class="primary" id="rec" ${canRecheck ? "" : "disabled"}>재검증 시작</button></div>
      <pre class="log" id="rplanout" hidden></pre></div>`;
  const run = async (path, opt, keyInputs) => {
    try { await api(token, path, opt); } catch (e) { alert(e.message); return; }
    keyInputs.forEach(i => { i.value = ""; });  // 키를 화면에서도 지운다
    await window._refresh();
    clearInterval(poll); poll = setInterval(window._refresh, 2500);
  };
  const yt = document.getElementById("ytkey");
  document.getElementById("prep").onclick = () => run("/api/job/prepare", {method: "POST", headers: {"X-Youtube-Key": yt.value.trim()}}, [yt]);
  document.getElementById("cap").oninput = () => loadQuote(token);
  if (serverVideo) {
    const oa = document.getElementById("oakey");
    document.getElementById("ana").onclick = () => {
      const fd = new FormData(); fd.append("cap_usd", document.getElementById("cap").value);
      if (document.getElementById("ack").checked) fd.append("ack_unverified", "1");
      run("/api/job/analyze", {method: "POST", body: fd, headers: {"X-Openai-Key": oa.value.trim()}}, [oa]);
    };
  } else {
    document.getElementById("pkg").onclick = async () => {
      const fd = new FormData(); fd.append("cap_usd", document.getElementById("cap").value);
      if (document.getElementById("ack").checked) fd.append("ack_unverified", "1");
      try {
        const blob = await (await api(token, "/api/job/helper-package", {method: "POST", body: fd})).blob();
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "seeding-helper.zip"; a.click();
      } catch (e) { alert(e.message); }
    };
    document.getElementById("resup").onclick = async () => {
      const f = document.getElementById("resfile").files[0], msg = document.getElementById("resmsg");
      if (!f) { msg.textContent = "result.json 파일을 고르세요"; return; }
      const fd = new FormData(); fd.append("result", f);
      msg.textContent = "올리는 중…";
      try {
        const r = await (await api(token, "/api/job/helper-result", {method: "POST", body: fd})).json();
        flash = `✔ PC 도우미 결과 영상 ${r.accepted}개를 반영했습니다` + (r.rejected_sig + r.rejected_unknown + r.rejected_bad ? ` (제외 ${r.rejected_sig + r.rejected_unknown + r.rejected_bad}개: 설정 불일치 ${r.rejected_sig}, 이 작업에 없는 영상 ${r.rejected_unknown}, 형식 오류 ${r.rejected_bad})` : "");
        lastState = "";  // 상태 카드를 다시 그려 안내 문구를 보여준다
        await window._refresh(); clearInterval(poll); poll = setInterval(window._refresh, 2500);
      } catch (e) { msg.textContent = "⚠ " + e.message; }
    };
  }
  const rcap = document.getElementById("rcap"), ryt = document.getElementById("rytkey"), roa = document.getElementById("roakey");
  const capForm = () => {
    const fd = new FormData();
    if (rcap && rcap.value) fd.append("cap_usd", rcap.value);
    if (document.getElementById("ack").checked) fd.append("ack_unverified", "1");
    return fd;
  };
  if (rcap) rcap.oninput = () => loadQuote(token);
  document.getElementById("rplan").onclick = async () => {
    const out = document.getElementById("rplanout");
    out.hidden = false; out.textContent = "계산 중… (아무것도 호출하거나 저장하지 않습니다)";
    try { out.textContent = await (await api(token, "/api/job/recheck-plan", {method: "POST", body: capForm()})).text(); }
    catch (e) { out.textContent = "⚠ " + e.message; }
  };
  document.getElementById("rec").onclick = () => run("/api/job/recheck",
    {method: "POST", body: capForm(), headers: roa ? {"X-Youtube-Key": ryt.value.trim(), "X-Openai-Key": roa.value.trim()} : {"X-Youtube-Key": ryt.value.trim()}},
    roa ? [ryt, roa] : [ryt]);
}

// ───── 견적 패널: 비용 상한 입력에 따라 다시 계산
let quoteTimer = null;
function loadQuote(token) {
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(async () => {
    const body = document.getElementById("quotebody");
    if (!body) return;
    const cap = (document.getElementById("rcap") || {}).value || (document.getElementById("cap") || {}).value || "";
    try {
      const q = await (await api(token, "/api/job/quote" + (cap ? "?cap_usd=" + encodeURIComponent(cap) : ""))).json();
      body.className = ""; body.innerHTML = quoteHtml(q);
      document.getElementById("ackrow").hidden = q.verified;
    } catch (e) { body.textContent = "견적을 계산하지 못했습니다: " + e.message; }
  }, 250);
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
window.addEventListener("hashchange", () => { boardKey = ""; board = null; lastState = ""; flash = ""; route(); });
route();
