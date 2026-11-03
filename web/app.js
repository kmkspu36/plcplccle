"use strict";

const pages = [
  ["dashboard", "대시보드", "▦", "워크스페이스의 실제 진행 상태를 확인하세요."],
  ["studio", "AI 엔지니어링 작업실", "◇", "기존 AGENT 01을 통한 엔지니어링 대화"],
  ["drawings", "PLC 도면 분석", "▧", "원본을 안전하게 보관하고 텍스트·표를 추출합니다."],
  ["ladder", "PLC 작화 작성", "⊞", "설비 요구사항과 래더 작화 준비"],
  ["code", "PLC 코드 생성", "⌘", "텍스트 원본·수정안·승인본을 분리하여 관리합니다."],
  ["inspection", "오류 검사", "◎", "실제 수행한 검사만 결과로 기록합니다."],
  ["correction", "오류 자동 수정", "⇄", "수정 전후 비교와 사용자 승인"],
  ["validation", "PLC 검증", "⊡", "AI 검토와 실제 컴파일·설비 시험을 구분합니다."],
  ["reports", "보고서", "▤", "실제 저장된 프로젝트 자료와 미확인 사항"],
  ["projects", "프로젝트 관리", "▱", "Mitsubishi PLC 프로젝트와 설비 요구사항 관리"],
  ["history", "작업 이력", "◷", "이 로컬 서버에서 실제 발생한 저장 이력"],
  ["settings", "설정", "⚙", "연결 준비 상태와 개발 환경의 한계"],
];
const agents = ["통합 운영", "작화 전문가", "도면 분석", "오류 검사", "오류 수정", "최종 검증", "코드 생성", "보고서"];
const extractionLabels = { extracted: "텍스트·표 추출 완료", partial: "일부 추출·확인 필요", needs_ocr: "OCR 필요·미추출 영역", failed: "추출 실패" };
const ocrLabels = { not_run: "OCR 미실행", extracted: "OCR 문자 추출·대조 필요", partial: "OCR 일부 추출·대조 필요", no_text: "OCR 인식 문자 없음", failed: "OCR 실패" };
const state = { key: "", status: null, projects: [], projectId: "", files: [], versions: [], history: [], page: "", chat: null, subscription: null, chatTimer: null, previewURL: null, document: null, busy: false, pendingRender: false, route: 0 };
const content = document.querySelector("#content");
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const date = value => new Date(value).toLocaleString("ko-KR");
const project = () => state.projects.find(item => item.id === state.projectId);
const currentVersion = () => state.versions.filter(item => item.kind === "original" || item.kind === "approved").at(-1);
const badge = (text, type = "") => `<span class="badge ${type}">${escapeHTML(text)}</span>`;
const empty = (title, text, action = "") => `<div class="empty"><span class="empty-symbol" aria-hidden="true">▱</span><h3>${escapeHTML(title)}</h3><p>${escapeHTML(text)}</p>${action}</div>`;
const card = (title, body, extra = "") => `<section class="card"><div class="card-head"><h2>${title}</h2>${extra}</div>${body}</section>`;
const info = (text, type = "") => `<div class="info ${type}">${text}</div>`;

let noticeTimer;
function notify(message, error = false) {
  const notice = document.querySelector("#notice");
  clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.className = error ? "error" : "";
  notice.hidden = false;
  noticeTimer = setTimeout(() => { notice.hidden = true; }, error ? 10000 : 5000);
}

async function api(path, options = {}) {
  const headers = { "X-Local-Session": state.key, ...options.headers };
  if (typeof options.body === "string") headers["Content-Type"] = "application/json";
  const response = await fetch(`/api${path}`, { ...options, headers, cache: "no-store", credentials: "omit" });
  if (!response.ok) {
    let message = `서버 요청 실패 (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = "입력 형식 또는 길이를 확인하세요.";
    } catch { /* Non-JSON HTTP errors retain their status code. */ }
    throw new Error(message);
  }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function projectPath(tail = "") { return `/projects/${state.projectId}${tail}`; }

function projectSelector() {
  return `<label class="subtle" for="active-project">현재 프로젝트
    <select id="active-project" class="project-select"><option value="" ${state.projects.length ? "disabled" : ""}>프로젝트 선택</option>${state.projects.map(p => `<option value="${p.id}" ${p.id === state.projectId ? "selected" : ""}>${escapeHTML(p.name)}</option>`).join("")}</select></label>`;
}
function heading(page) {
  return `<div class="page-heading"><div><div class="eyebrow">ENGINEERING WORKSPACE / ${escapeHTML(page[0].toUpperCase())}</div><h1>${page[1]}</h1><p class="subtle">${page[3]}</p></div>${projectSelector()}</div>`;
}
function requireProject() {
  return project() ? "" : info('프로젝트를 먼저 생성하거나 선택하세요. <a href="#projects">프로젝트 관리로 이동 →</a>', "warning");
}
function agentRows(all = true) {
  return agents.slice(0, all ? 8 : 1).map((name, index) => `<div class="agent-row"><span class="agent-number">${String(index + 1).padStart(2, "0")}</span><div class="agent-name">${name}<small>${index ? "01에서 실제 호출·입출력 확인 필요" : "기존 Copilot Studio · 기본 진입점"}</small></div>${badge("연결 확인 필요", "warn")}</div>`).join("");
}
function historyList(limit = 6) {
  return state.history.length ? `<div class="card-body">${state.history.slice(0, limit).map(item => `<div class="history-item"><strong>${escapeHTML(item.detail)}</strong><small>${escapeHTML(date(item.created_at))} · ${escapeHTML(state.projects.find(p => p.id === item.project_id)?.name ?? "프로젝트")}</small></div>`).join("")}</div>` : empty("아직 기록된 작업이 없습니다", "프로젝트 생성, 파일 보관, 버전 저장 후 실제 이력이 표시됩니다.");
}
function dashboard() {
  const p = project();
  return `<section class="welcome"><div><div class="eyebrow">YOUR ENGINEERING, CONNECTED</div><h2>${p ? `${escapeHTML(p.name)} 작업을 이어가세요` : "엔지니어링의 시작, 하나의 워크스페이스"}</h2><p>도면에서 코드, 검사와 검증까지. 확인된 사실을 바탕으로 작업하세요.</p><div class="actions"><button class="primary" data-go="projects">＋ 새 프로젝트</button><button data-go="studio">AI 작업실 열기 ↗</button></div></div><div class="welcome-mark" aria-hidden="true">─[ ]─</div></section>
  <div class="metrics">
    <div class="metric"><div class="metric-label">로컬 프로젝트 <span>▱</span></div><div class="metric-value">${state.projects.length}</div><small>실제 저장된 프로젝트</small></div>
    <div class="metric"><div class="metric-label">현재 프로젝트 자료 <span>▧</span></div><div class="metric-value">${state.files.length}</div><small>${p ? "원본 보관 파일" : "프로젝트 미선택"}</small></div>
    <div class="metric"><div class="metric-label">최근 오류 검사 <span>◎</span></div><div class="metric-value text">미검사</div><small>검사 에이전트 연동 필요</small></div>
    <div class="metric"><div class="metric-label">AI 에이전트 <span>◇</span></div><div class="metric-value text">연결 확인 필요</div><small>AGENT 01 · 실행 실적 미확인</small></div>
  </div>
  <div class="grid two-col"><div>
    ${card("최근 프로젝트", state.projects.length ? `<div class="table-wrap"><table><thead><tr><th>프로젝트</th><th>PLC 환경</th><th>상태</th><th></th></tr></thead><tbody>${state.projects.slice(0, 5).map(item => `<tr><td class="file-name">${escapeHTML(item.name)}<div class="subtle">${escapeHTML(date(item.created_at))}</div></td><td>${escapeHTML(item.gx_works_version)}<div class="subtle">CPU ${escapeHTML(item.cpu_model || "미정")}</div></td><td>${badge("미검사")}</td><td><button data-project="${item.id}">열기</button></td></tr>`).join("")}</tbody></table></div>` : empty("첫 프로젝트를 만들어보세요", "프로젝트 이름과 GX Works 환경부터 설정할 수 있습니다.", '<button data-go="projects">프로젝트 생성 →</button>'), '<a class="subtle" href="#projects">전체 보기 →</a>')}
    ${card("빠른 작업 시작", `<div class="quick-grid">${[["drawings", "▧", "자료 업로드", "무료 텍스트·표 추출"], ["ladder", "⊞", "작화 준비", "설비 요구사항 작성"], ["code", "⌘", "텍스트 편집", "원본·수정안 관리"], ["validation", "⊡", "검증 현황", "미수행 항목 확인"]].map(([id, icon, title, sub]) => `<a class="quick" href="#${id}"><span aria-hidden="true">${icon}</span><strong>${title}</strong><small>${sub}</small></a>`).join("")}</div>`)}
    ${card("최근 작업 이력", historyList(4), '<a class="subtle" href="#history">전체 보기 →</a>')}
  </div><div>
    ${card("에이전트 연결 현황", `<div class="card-body">${agentRows()}<p class="subtle">이름·AI 응답 문장은 전문 에이전트 실행 증거가 아닙니다.</p></div>`)}
    ${card("PLC 작업 환경", `<div class="card-body"><dl class="definition"><dt>제조사</dt><dd>Mitsubishi</dd><dt>소프트웨어</dt><dd>${escapeHTML(p?.gx_works_version ?? "미정")}</dd><dt>CPU 모델</dt><dd>${escapeHTML(p?.cpu_model || "미정")}</dd><dt>프로그램 언어</dt><dd>Ladder Diagram 중심</dd><dt>실제 컴파일</dt><dd>${badge("미수행")}</dd></dl></div>`)}
  </div></div>`;
}

function projectForm(edit = false) {
  const p = edit ? project() : null;
  return `<form id="${edit ? "edit-project" : "new-project"}" class="card-body">
    <div class="form-grid"><div class="field wide"><label for="${edit ? "edit" : "new"}-name">프로젝트 이름</label><input id="${edit ? "edit" : "new"}-name" name="name" maxlength="100" required value="${escapeHTML(p?.name ?? "")}" placeholder="설비 또는 프로젝트 이름"></div>
    <div class="field"><label for="${edit ? "edit" : "new"}-gx">GX Works 버전</label><select id="${edit ? "edit" : "new"}-gx" name="gx_works_version">${["미정", "GX Works2", "GX Works3"].map(v => `<option ${v === p?.gx_works_version ? "selected" : ""}>${v}</option>`).join("")}</select></div>
    <div class="field"><label for="${edit ? "edit" : "new"}-cpu">CPU 모델</label><input id="${edit ? "edit" : "new"}-cpu" name="cpu_model" maxlength="100" placeholder="미정" value="${escapeHTML(p?.cpu_model ?? "")}"></div>
    <div class="field wide"><label for="${edit ? "edit" : "new"}-requirements">설비 요구사항</label><textarea id="${edit ? "edit" : "new"}-requirements" name="requirements" rows="7" maxlength="20000" placeholder="설비 목적, 동작 순서, 입력·출력 조건, 정지 조건, 인터록 및 추가 요구사항">${escapeHTML(p?.requirements ?? "")}</textarea></div></div>
    <p class="subtle">Mitsubishi · Ladder 중심. AI에 자동 전달되지 않습니다.</p><button class="primary" type="submit">${edit ? "프로젝트 정보 저장" : "프로젝트 생성"}</button></form>`;
}
function projectsPage() {
  return `<div class="grid equal-col"><div>${card("새 프로젝트", projectForm())}</div><div>${project() ? card("현재 프로젝트 정보 수정", projectForm(true)) : card("프로젝트 정보", empty("프로젝트가 선택되지 않았습니다", "새 프로젝트를 생성하거나 상단에서 선택하세요."))}</div></div>`;
}

function fileRows() {
  return state.files.length ? `<div class="table-wrap"><table><thead><tr><th>파일</th><th>업로드 / 추출 / OCR</th><th>AI 전달</th><th>자료 확인</th></tr></thead><tbody>${state.files.map(file => `<tr><td class="file-name">${escapeHTML(file.name)}<div class="subtle">${(file.size / 1024).toFixed(1)} KB · ${escapeHTML(date(file.created_at))}</div></td><td>${badge("원본 보관", "blue")}<p>${badge(extractionLabels[file.extraction_status] ?? file.extraction_status, file.extraction_status === "extracted" ? "blue" : "warn")}</p>${[".pdf", ".png", ".jpg", ".jpeg"].includes(file.suffix) ? `<p>${badge(ocrLabels[file.ocr.status], file.ocr.status === "not_run" ? "" : "warn")}${file.ocr.page ? `<span class="subtle"> 최근 ${file.ocr.page}페이지</span>` : ""}</p>` : ""}</td><td>${badge("미연결", "warn")}</td><td><div class="file-actions"><button data-extract="${file.id}">추출 보기</button><button data-download="${file.id}">원본</button>${[".pdf", ".png", ".jpg", ".jpeg"].includes(file.suffix) ? `<button data-preview="${file.id}">미리 보기 / OCR</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>` : empty("등록된 파일이 없습니다", "업로드한 자료는 이 PC의 비공개 로컬 폴더에 보관됩니다.");
}
function drawings() {
  return `${requireProject()}${info("파일 보관·무료 추출·로컬 OCR과 PLC 도면 AI 분석은 서로 다른 단계입니다. OCR은 문자만 추출하며 기호·회로 연결은 판독하지 않습니다. AGENT 01 → 03 전달은 미연결입니다.")}
  <div class="grid two-col"><div>
    ${card("도면 및 엔지니어링 자료", `<form id="upload-form" class="card-body"><div class="drop-zone"><strong>분석할 원본 파일을 선택하세요</strong><small>PDF · JPG · JPEG · PNG · XLSX · CSV · TXT</small><input type="file" id="upload-file" aria-label="업로드 파일 선택" accept=".pdf,.jpg,.jpeg,.png,.xlsx,.csv,.txt" required ${!project() ? "disabled" : ""}><small>파일당 15MB · PDF 40페이지 · 전체 보관량 250MB</small></div><div class="actions section-space"><button class="primary" type="submit" ${!project() ? "disabled" : ""}>업로드 및 무료 추출</button><span id="upload-status" class="subtle" role="status">AI 자동 전달 없음</span></div><progress id="upload-progress" hidden aria-label="업로드 및 추출 중"></progress></form>`)}
    ${card("보관 파일", `<div id="file-list">${fileRows()}</div>`, badge(`${state.files.length}개`))}
  </div><div>
    ${card("처리 단계", '<div class="card-body"><div class="check-row"><div>01 · 원본 업로드<small>형식·서명·크기 검사 후 비공개 보관</small></div>' + badge("구현", "blue") + '</div><div class="check-row"><div>02 · 무료 전처리<small>PDF 텍스트·표, Excel, CSV, TXT</small></div>' + badge("구현", "blue") + '</div><div class="check-row"><div>03 · 로컬 OCR<small>선택한 1페이지의 문자만 추출 · 기호 판독 아님</small></div>' + badge(state.status.ocr.status === "configured" ? "설정됨·실행 필요" : "설정 필요", "warn") + '</div><div class="check-row"><div>04 · AI 전달·도면 분석<small>01 → 03 원본·추출 전달 경로 확인 필요</small></div>' + badge("미연결", "warn") + '</div></div>')}
    ${card("파일 확인", '<div id="file-detail" class="card-body"><p class="subtle">미리 보기 / OCR에서 PDF 페이지를 이동하거나 이미지의 문자를 추출할 수 있습니다. OCR 결과는 원본과 별도 보관하며 실제 실행하지 않은 페이지는 미실행으로 표시합니다.</p></div>')}
  </div></div>`;
}

function disconnected(agent, description) {
  return info(`<strong>AGENT ${agent} · 미연결 / 실행 확인 필요</strong><p>${description}</p><p>기존 에이전트 지침은 변경하지 않았습니다. <a href="#studio">AGENT 01 작업실 →</a></p>`, "warning");
}
function ladder() {
  const p = project();
  return `${requireProject()}${disconnected("02", "실제 래더 렌더러·작화 생성 및 I/O 구조 추출은 아직 연결되지 않았습니다.")}
  <div class="grid equal-col">${card("설비 동작 요구사항", `<div class="card-body">${p ? `<label for="ladder-requirements">목적 · 동작 순서 · I/O · 정지 조건 · 인터록</label><textarea id="ladder-requirements" rows="15" maxlength="20000">${escapeHTML(p.requirements)}</textarea><div class="actions section-space"><button class="primary" id="save-requirements">요구사항 저장</button><button disabled>AI 작화 생성 · 미연결</button></div>` : empty("프로젝트 선택 필요", "요구사항을 프로젝트별로 보관합니다.")}</div>`)}
  ${card("래더 / I/O / 디바이스", `<div class="card-body">${info("실제 래더 다이어그램 표시 기능은 미구현입니다. 텍스트 편집 결과를 래더 또는 GX 네이티브 파일로 표시하지 않습니다.")}<div class="check-row"><span>I/O 목록</span>${badge("미추출")}</div><div class="check-row"><span>디바이스 목록</span>${badge("미추출")}</div><div class="check-row"><span>네트워크 구성</span>${badge("미생성")}</div><button disabled class="section-space">래더 표시 · 렌더러 필요</button></div>`)}</div>`;
}
function codePage() {
  const p = project();
  const current = currentVersion();
  return `${requireProject()}${disconnected("07", "AI 코드 생성·변수/디바이스 추출은 미연결입니다. 현재 편집기는 PC·모바일 공통 경량 텍스트 편집기입니다.")}
  <div class="grid two-col"><div>${card("텍스트 코드 편집기", `<div class="card-body"><div class="actions"><span class="badge">Mitsubishi</span><span class="badge">${escapeHTML(p?.gx_works_version ?? "미정")}</span><span class="badge">CPU ${escapeHTML(p?.cpu_model || "미정")}</span>${badge("미검사", "warn")}</div><label class="section-space" for="code-editor">${current ? "현재 기준 버전에서 수정안 작성" : "원본 텍스트 등록"}</label><textarea id="code-editor" class="code-editor" spellcheck="false" maxlength="150000" aria-describedby="code-format" ${!p ? "disabled" : ""}>${escapeHTML(current?.text ?? "")}</textarea><div id="code-format" class="code-meta"><span>일반 텍스트 · GX 네이티브 프로젝트 아님</span><span>최대 150,000자</span></div><div class="actions section-space"><button id="save-code" class="primary" ${!p ? "disabled" : ""}>${current ? "수정안으로 저장" : "원본으로 저장"}</button><button id="copy-code" ${!p ? "disabled" : ""}>텍스트 복사</button><button disabled>AI 코드 생성 · 미연결</button></div></div>`, badge(current ? `${state.versions.length}개 버전` : "원본 없음"))}</div><div>
  ${card("버전 기록", state.versions.length ? `<div class="card-body">${state.versions.map(v => `<div class="history-item"><strong>${escapeHTML({ original: "원본", proposal: "수정안", approved: "사용자 승인본" }[v.kind])}</strong> ${badge("미검사")}<small>${escapeHTML(date(v.created_at))}</small><button class="section-space" data-version="${v.id}">텍스트 보기</button></div>`).join("")}</div>` : empty("아직 코드가 없습니다", "원본을 저장하면 수정안과 승인본을 별도 보관합니다."))}
  ${card("버전 내용", '<div class="card-body"><pre id="version-detail">버전을 선택하세요.</pre></div>')}
  </div></div>`;
}
function inspection() {
  return `${requireProject()}${disconnected("04", "이중코일·중복 출력·미선언 변수·인터록·문법·작화 일치 검사를 실제로 실행하지 않았습니다.")}
    ${card("오류 검사 결과", empty("검사 결과가 없습니다", "검사 에이전트 연결과 실제 입출력 확인 후 결과를 기록할 수 있습니다.", '<button disabled>오류 검사 실행 · 미연결</button>'), badge("미검사", "warn"))}
    ${card("검사 범위", '<div class="card-body"><p class="subtle">이중코일 / 중복 출력 / 변수·선언 / 디바이스 주소 / 자기유지·인터록 / 타이머·카운터 / 명령어·문법 / 작화와 코드 불일치</p><p class="subtle">오류 ID·유형·심각도·위치·원인·영향·수정 제안은 실제 결과가 수신된 뒤 표시해야 합니다.</p></div>')}`;
}
function correction() {
  const current = currentVersion();
  const proposals = state.versions.filter(v => v.kind === "proposal" && v.base_id === current?.id);
  return `${requireProject()}${disconnected("05", "AI 자동 수정은 연결되지 않았습니다. 아래 비교·승인은 사용자가 직접 작성한 텍스트 수정안에만 적용됩니다.")}
    <div class="steps"><span>오류 확인 · 미연결</span><span>AI 수정 · 미연결</span><span class="current">수동 수정안 비교</span><span>사용자 승인</span><span>새 버전 보관</span><span>재검사 · 미연결</span></div>
    ${card("수정 전후 비교", `<div class="card-body">${proposals.length ? `<div class="field"><label for="proposal-select">현재 기준 버전의 수정안</label><select id="proposal-select"><option value="">수정안 선택</option>${proposals.map(v => `<option value="${v.id}">${escapeHTML(date(v.created_at))} · ${escapeHTML(v.id.slice(0, 8))}</option>`).join("")}</select></div><div class="grid equal-col"><div><h3>수정 전 · 원본/현재 승인본</h3><pre>${escapeHTML(current.text)}</pre></div><div><h3>수정 후 · 미승인 수정안</h3><pre id="proposal-text">수정안을 선택하세요.</pre></div></div><div id="diff-summary" class="info section-space">수정안 선택 후 비교 내역이 표시됩니다.</div><label class="check-label section-space"><input type="checkbox" id="approve-check"><span>전후 내용을 검토했습니다. 승인본은 미검사 상태로 저장되며 실제 설비 사용 전 별도 검증이 필요함을 확인합니다.</span></label><button id="approve-version" class="primary section-space" disabled>수정안 승인 · 새 버전 저장</button>` : empty("비교할 수정안이 없습니다", "코드 화면에서 원본을 먼저 보관한 후 수정안을 저장하세요.", '<button data-go="code">텍스트 편집으로 이동 →</button>')}</div>`)}`;
}
function validation() {
  const rows = [
    ["AI 논리 검토", "AGENT 06 실제 실행·결과 연결 필요"],
    ["규칙 기반 검사", "규칙 검사 엔진 미구현"],
    ["GX Works 실제 컴파일", "GX Works2/3 환경 및 CPU 확정 필요"],
    ["시뮬레이션", "GX Simulator 실행·결과 수집 미연결"],
    ["실제 설비 시험", "초기 범위 밖 · 현장 엔지니어의 독립 검증 필요"],
  ];
  return `${requireProject()}${info("AI 답변은 실제 컴파일·시뮬레이션·설비 시험의 증거가 아닙니다. PLC 직접 다운로드 및 자동 제어는 제공하지 않습니다.", "warning")}${card("독립 검증 상태", `<div class="card-body">${rows.map(([title, detail]) => `<div class="check-row"><div>${title}<small>${detail}</small></div>${badge("미수행", "warn")}</div>`).join("")}</div>`)}`;
}
function reports() {
  const p = project();
  return `${requireProject()}${disconnected("08", "AI 보고서 작성과 PDF·Excel 출력은 추가 개발이 필요합니다. 아래에는 실제 로컬 저장 자료만 표시됩니다.")}
    ${card("프로젝트 자료 현황", `<div class="card-body"><dl class="definition"><dt>프로젝트</dt><dd>${escapeHTML(p?.name ?? "미선택")}</dd><dt>환경</dt><dd>Mitsubishi / ${escapeHTML(p?.gx_works_version ?? "미정")} / CPU ${escapeHTML(p?.cpu_model || "미정")}</dd><dt>보관 자료</dt><dd>${state.files.length}개 · AI 분석 미실행</dd><dt>텍스트 버전</dt><dd>${state.versions.length}개 · 미검사</dd><dt>검사 / 재검사</dt><dd>미수행</dd><dt>실제 검증</dt><dd>컴파일·시뮬레이션·설비 시험 미수행</dd><dt>미확인 사항</dt><dd>에이전트 실행·파일 전달·CPU·GX 연결·OCR 문자 원본 대조 및 도면 의미 판독</dd></dl><h3 class="section-space">저장된 설비 요구사항</h3><pre>${escapeHTML(p?.requirements || "등록된 요구사항 없음")}</pre><div class="actions section-space"><button disabled>AI 보고서 ·미연결</button><button disabled>PDF / Excel 출력 · 미구현</button></div></div>`)}`;
}
function studio() {
  const ready = state.status.broker_enabled && state.status.broker_configured;
  return `${requireProject()}${info("이 채팅의 연결 대상은 기존 브로커가 가리키는 에이전트입니다. AGENT 01인지와 02~08 실제 호출은 운영 환경 확인이 필요합니다. 프로젝트·업로드 파일은 자동 전송되지 않습니다.", "warning")}
  <div class="grid two-col"><div>${card("AGENT 01 · 통합 운영 작업실", `<div class="card-body"><div class="connection-line"><span id="chat-status" role="status">${badge("미연결", "warn")}</span><div class="actions"><button id="connect-chat" class="primary" ${!ready ? "disabled" : ""}>기존 Web Chat 연결</button><button id="disconnect-chat" disabled>연결 종료</button></div></div>
    ${!ready ? `<p class="subtle">${!state.status.broker_enabled ? "PLC_ENABLE_BROKER 비활성. 운영 확인 후 명시적 활성화가 필요합니다." : ""} ${!state.status.broker_configured ? "서버 비밀값 PLC_BROKER_SEALED 설정이 필요합니다." : ""}</p>` : '<p class="subtle">연결 버튼을 누르면 기존 브로커와 Microsoft Direct Line에 실제 요청합니다. 기존 서비스의 사용량·요금 정책이 적용될 수 있습니다.</p>'}
    <div id="webchat" class="chat-frame" role="region" aria-label="AGENT 01 채팅"><div class="empty"><span class="empty-symbol">◇</span><h3>엔지니어링 대화를 준비하세요</h3><p>Web Chat 4.18.0 · 한국어 · 실제 연결 상태로 판정</p></div></div></div>`, badge("기존 에이전트 보존", "blue"))}</div><div>
    ${card("작업 컨텍스트", `<div class="card-body"><label for="task-type">작업 유형 · 참고용</label><select id="task-type">${["PLC 작화", "도면 분석", "PLC 코드 생성", "오류 검사", "오류 수정", "최종 검증", "보고서 작성"].map(t => `<option>${t}</option>`).join("")}</select><p class="subtle">선택만으로 에이전트가 실행되거나 channelData가 매핑되지 않습니다. 채팅에서 요청 내용을 직접 입력하세요.</p><dl class="definition section-space"><dt>프로젝트</dt><dd>${escapeHTML(project()?.name ?? "미선택")}</dd><dt>파일 첨부</dt><dd>웹 자체 보관만 지원 · Direct Line 전달 확인 필요</dd><dt>전문 에이전트</dt><dd>01 → 02~08 입출력 미확인</dd></dl><button data-go="drawings" class="section-space">로컬 파일 업로드</button></div>`)}
    ${card("실행 확인 원칙", '<div class="card-body"><p class="subtle">연결됨은 Direct Line Online 상태만 의미합니다. AI 답변 문장으로 전문 에이전트 실행·검사·검증의 성공을 추정하지 않습니다.</p><p class="subtle">현재 대화는 페이지를 이동하면 종료되며 로컬 프로젝트 이력에 저장되지 않습니다. 민감한 내용은 운영·보관 정책 확인 후 입력하세요.</p></div>')}
  </div></div>`;
}
function settings() {
  return `${info("<strong>공개 배포 금지 · 로컬 단일사용 개발 범위</strong><p>로컬 세션 보호와 프로젝트별 자료 경로 검사는 구현되어 있으나 로그인·다중 사용자 소유권·조직 권한·운영 감사·백업/삭제 정책은 완성되지 않았습니다. 이 PC의 다른 프로세스와 사용자를 격리하는 보안 경계가 아닙니다.</p>", "warning")}
  <div class="grid equal-col">${card("연결 설정 · 읽기 전용", `<div class="card-body"><dl class="definition"><dt>Web Chat</dt><dd>4.18.0 · 연결 시 CDN 로드</dd><dt>브로커 호출</dt><dd>${badge(state.status.broker_enabled ? "명시적 활성화" : "비활성", "warn")}</dd><dt>서버 비밀값</dt><dd>${badge(state.status.broker_configured ? "설정됨 · 값 비공개" : "설정 필요", "warn")}</dd><dt>실제 연결</dt><dd>미확인 · 작업실에서 명시적 연결 필요</dd><dt>기본 대상</dt><dd>AGENT 01 · 브로커 대상 확인 필요</dd><dt>전문 호출</dt><dd>기존 Studio 연결·권한·게시 상태 확인 필요</dd><dt>파일 전달</dt><dd>Direct Line 원본 / 추출 텍스트 경로 미확인</dd></dl><p class="subtle section-space">비밀값은 UI에 입력하거나 저장하지 않습니다. 서버 환경변수로만 관리하세요. 기존 브로커·에이전트 설정 및 지침은 변경하지 않았습니다.</p></div>`)}
  ${card("파일 처리 및 제한", `<div class="card-body"><dl class="definition"><dt>보관</dt><dd>서버 비공개 디렉터리 · SQLite 메타데이터</dd><dt>지원</dt><dd>PDF/JPG/JPEG/PNG/XLSX/CSV/TXT</dd><dt>한도</dt><dd>파일 15MB / PDF 40페이지 / 이미지 2천만 픽셀</dd><dt>로컬 한도</dt><dd>프로젝트 50 / 파일 100 / 원본 총 250MB / OCR 500회</dd><dt>추출</dt><dd>별도 프로세스 · 25초 · 텍스트 150,000자 · 표 10,000셀</dd><dt>페이지 미리 보기</dt><dd>PDF·이미지 PNG 렌더링 · 최대 긴 변 1,600px</dd><dt>로컬 OCR</dt><dd>${escapeHTML(state.status.ocr.reason)}<p class="subtle">Tesseract · 한국어+영어 · 페이지별 실행 · 외부 전송 없음 · 최대 32초</p></dd><dt>Excel</dt><dd>무료 openpyxl 추출 · 수식 실행 안 함 · 직접 AI 전달 미지원</dd></dl><p class="subtle section-space">추출은 PLC 판단 AI가 아닙니다. 손상/암호화 문서·과도한 압축률·매크로/외부 연결 XLSX를 거부합니다. 바이러스 검사 및 Windows OS 수준 파서 샌드박스는 미구현입니다.</p></div>`)}</div>
  ${card("구현과 후속 연결 구분", '<div class="card-body"><details open><summary>이번 기반에서 제공하는 기능</summary><p>12개 화면 탐색, 프로젝트·요구사항 저장, 원본 파일 보관 및 무료 추출, PDF 페이지 미리 보기, 선택 페이지의 무료 로컬 OCR, 텍스트 버전 저장·비교·승인, 실제 작업 이력, 명시적 설정 기반 기존 토큰 중계와 Web Chat 연결 코드.</p></details><details><summary>실제 환경 확인이 필요한 항목</summary><p>AGENT 01 대상·실제 응답, 02~08 호출과 입출력, 업로드 활성화, Web Chat 4.18.0 첨부 호환, 01에서03 원본 전달 및 추출 텍스트 매핑. OCR 문자는 항상 원본과 대조해야 합니다.</p></details><details><summary>추가 개발 / 초기 범위 밖</summary><p>도면 기호·회로 연결 판독, 래더 렌더러, GX 네이티브 파일·실제 컴파일·시뮬레이션, AI 결과 구조화, 보고서 출력, 다중 사용자 인증. PLC 다운로드 및 설비 자동제어는 초기 범위 밖입니다.</p></details></div>')}`;
}

function stopChat() {
  clearTimeout(state.chatTimer);
  state.subscription?.unsubscribe();
  state.chat?.end();
  state.subscription = null;
  state.chat = null;
}
async function render() {
  if (state.busy) { state.pendingRender = true; return; }
  state.pendingRender = false;
  const route = ++state.route;
  const id = location.hash.slice(1) || "dashboard";
  const page = pages.find(p => p[0] === id) ?? pages[0];
  stopChat();
  if (state.previewURL) { URL.revokeObjectURL(state.previewURL); state.previewURL = null; }
  state.document = null;
  state.page = page[0];
  document.querySelector("#sidebar").classList.remove("open");
  document.querySelector("#menu-toggle").setAttribute("aria-expanded", "false");
  document.querySelector("#menu-toggle").setAttribute("aria-label", "메뉴 열기");
  document.querySelector("#breadcrumb").textContent = page[1];
  document.title = `${page[1]} · PLC Engineering AI`;
  document.querySelectorAll(".nav-item").forEach(a => {
    const active = a.hash === `#${state.page}`;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  try {
    const projects = await json("/projects");
    const projectId = projects.some(p => p.id === state.projectId) ? state.projectId : projects[0]?.id ?? "";
    const [files, versions, history] = await Promise.all([
      projectId ? json(`/projects/${projectId}/files`) : [],
      projectId ? json(`/projects/${projectId}/versions`) : [],
      json(page[0] === "history" && projectId ? `/history?project_id=${projectId}` : "/history"),
    ]);
    if (route !== state.route) return;
    Object.assign(state, { projects, projectId, files, versions, history });
    document.querySelector("footer span").textContent = `CPU ${project()?.cpu_model || "미정"} · 실제 설비 제어 범위 밖`;
    const views = { dashboard, projects: projectsPage, drawings, ladder, code: codePage, inspection, correction, validation, reports, studio, settings, history: () => card("실제 작업 이력 · 최근 200건", historyList(200), badge(project() ? "현재 프로젝트" : "전체 프로젝트")) };
    content.innerHTML = heading(page) + views[state.page]();
    bindPage();
  } catch (error) {
    if (route === state.route) content.innerHTML = heading(page) + info(`<strong>서버 데이터를 읽지 못했습니다.</strong><p>${escapeHTML(error.message)}</p><button id="reload-app">다시 연결</button>`, "danger");
    document.querySelector("#reload-app")?.addEventListener("click", () => location.reload());
  }
}

async function action(button, callback) {
  if (state.busy) return;
  state.busy = true;
  button.disabled = true;
  try { await callback(); }
  catch (error) { notify(error.message, true); }
  finally {
    state.busy = false;
    if (button.isConnected) button.disabled = false;
    if (state.pendingRender) await render();
  }
}
function bindPage() {
  document.querySelector("#active-project").addEventListener("change", event => {
    if (state.busy) { event.target.value = state.projectId; notify("진행 중인 작업이 끝난 뒤 프로젝트를 변경하세요.", true); return; }
    state.projectId = event.target.value;
    render();
  });
  for (const id of ["new-project", "edit-project"]) {
    document.querySelector(`#${id}`)?.addEventListener("submit", event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(event.target));
      action(event.submitter, async () => {
        const result = await json(id === "new-project" ? "/projects" : projectPath(), { method: id === "new-project" ? "POST" : "PUT", body: JSON.stringify(values) });
        state.projectId = result.id;
        notify(id === "new-project" ? "프로젝트를 생성했습니다." : "프로젝트 정보를 저장했습니다.");
        state.busy = false;
        await render();
      });
    });
  }
  document.querySelector("#save-requirements")?.addEventListener("click", event => action(event.target, async () => {
    await json(projectPath(), { method: "PUT", body: JSON.stringify({ ...project(), requirements: document.querySelector("#ladder-requirements").value }) });
    notify("요구사항을 로컬에 저장했습니다. AI에 전송하지 않았습니다.");
  }));
  document.querySelector("#upload-form")?.addEventListener("submit", uploadFile);
  document.querySelector("#save-code")?.addEventListener("click", event => action(event.target, async () => {
    const text = document.querySelector("#code-editor").value;
    if (!text.trim()) throw new Error("저장할 텍스트를 입력하세요.");
    const base = currentVersion();
    await json(projectPath("/versions"), { method: "POST", body: JSON.stringify({ kind: base ? "proposal" : "original", text, base_id: base?.id ?? null }) });
    notify(base ? "수정안을 별도 버전으로 저장했습니다. 오류 자동 수정 화면에서 비교·승인하세요." : "원본 텍스트를 보관했습니다. 아직 검사하지 않았습니다.");
    state.busy = false;
    await render();
  }));
  document.querySelector("#copy-code")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(document.querySelector("#code-editor").value); notify("텍스트를 복사했습니다."); }
    catch { notify("클립보드 권한이 없습니다. 편집기에서 직접 선택하여 복사하세요.", true); }
  });
  const updateApproval = () => {
    const selected = document.querySelector("#proposal-select").value;
    const proposal = state.versions.find(v => v.id === selected);
    document.querySelector("#proposal-text").textContent = proposal?.text ?? "수정안을 선택하세요.";
    const before = currentVersion()?.text.split("\n") ?? [];
    const after = proposal?.text.split("\n") ?? [];
    const changed = proposal ? Array.from({ length: Math.max(before.length, after.length) }, (_, i) => before[i] !== after[i] ? i + 1 : null).filter(Boolean) : [];
    document.querySelector("#diff-summary").textContent = proposal ? `같은 줄 위치 기준 변경: ${changed.length}개 줄. 위치: ${changed.slice(0, 40).join(", ") || "없음"}${changed.length > 40 ? " …" : ""}. 삽입/삭제에 따른 줄 밀림을 포함하며 의미 검사는 아닙니다.` : "수정안을 선택하세요.";
    document.querySelector("#approve-version").disabled = !proposal || !document.querySelector("#approve-check").checked;
  };
  document.querySelector("#proposal-select")?.addEventListener("change", () => { document.querySelector("#approve-check").checked = false; updateApproval(); });
  document.querySelector("#approve-check")?.addEventListener("change", updateApproval);
  document.querySelector("#approve-version")?.addEventListener("click", event => action(event.target, async () => {
    const id = document.querySelector("#proposal-select").value;
    if (!id || !document.querySelector("#approve-check").checked) throw new Error("수정안 선택과 사용자 검토 확인이 필요합니다.");
    await json(projectPath(`/versions/${id}/approve`), { method: "POST" });
    notify("승인본을 새 버전으로 보관했습니다. 재검사는 미연결·미수행 상태입니다.");
    state.busy = false;
    await render();
  }));
  document.querySelector("#connect-chat")?.addEventListener("click", connectChat);
  document.querySelector("#disconnect-chat")?.addEventListener("click", () => {
    stopChat();
    document.querySelector("#webchat").replaceChildren();
    document.querySelector("#chat-status").textContent = "연결 종료";
    document.querySelector("#connect-chat").disabled = false;
    document.querySelector("#disconnect-chat").disabled = true;
  });
}

async function uploadFile(event) {
  event.preventDefault();
  const file = document.querySelector("#upload-file").files[0];
  if (!file) return;
  if (file.size > state.status.max_upload_bytes || !file.size) { notify("빈 파일 또는 15MB 초과 파일입니다.", true); return; }
  await action(event.submitter, async () => {
    const progress = document.querySelector("#upload-progress");
    const status = document.querySelector("#upload-status");
    progress.hidden = false;
    status.textContent = "업로드·서버 검증·무료 추출 중 … AI에 전달하지 않습니다.";
    try {
      const result = await json(projectPath("/files"), { method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) }, body: file });
      notify(`원본 보관 완료 · ${extractionLabels[result.extraction_status]} · AI 전달 미연결`);
      state.busy = false;
      await render();
    } finally {
      progress.hidden = true;
      if (status.isConnected) status.textContent = "작업 종료. 오류 메시지가 있으면 원인을 확인하고 다시 시도하세요.";
    }
  });
}

async function showDocument(file, pageNumber = 1) {
  const response = await api(projectPath(`/files/${file.id}/preview?page=${pageNumber}`));
  const count = Number(response.headers.get("X-Page-Count"));
  const blob = await response.blob();
  const history = await json(projectPath(`/files/${file.id}/ocr?page=${pageNumber}`));
  if (state.previewURL) URL.revokeObjectURL(state.previewURL);
  state.previewURL = URL.createObjectURL(blob);
  state.document = { file, page: pageNumber, count };
  const result = history[0];
  const configured = state.status.ocr.status === "configured";
  document.querySelector("#file-detail").innerHTML = `<h3>${escapeHTML(file.name)}</h3>
    <div class="preview-controls actions"><button data-document-page="${pageNumber - 1}" ${pageNumber <= 1 ? "disabled" : ""} aria-label="이전 페이지">← 이전</button>
    <label for="document-page">페이지 <select id="document-page" aria-label="페이지 선택">${Array.from({ length: count }, (_, i) => `<option value="${i + 1}" ${i + 1 === pageNumber ? "selected" : ""}>${i + 1} / ${count}</option>`).join("")}</select></label>
    <button data-document-page="${pageNumber + 1}" ${pageNumber >= count ? "disabled" : ""} aria-label="다음 페이지">다음 →</button></div>
    <img class="preview-image section-space" alt="${escapeHTML(file.name)} ${pageNumber}페이지 미리 보기">
    <p class="subtle">서버에서 제한 해상도로 렌더링한 미리 보기입니다. 원본 파일은 변경하지 않습니다.</p>
    <div class="info warning">로컬 OCR은 글자만 추출합니다. 오인식·누락이 있으며 회로 기호·배선 연결·PLC 안전성은 검증하지 않습니다. 원본 대조가 필요합니다.</div>
    ${!configured ? `<p class="subtle">${escapeHTML(state.status.ocr.reason)} <a href="#settings">설정 확인 →</a></p>` : ""}
    <button id="run-local-ocr" class="primary" data-run-ocr="true" ${!configured ? "disabled" : ""}>현재 페이지 로컬 OCR ${result ? "재실행" : "실행"}</button>
    <p id="ocr-progress" class="subtle" role="status">외부 전송 없음 · 선택한 ${pageNumber}페이지에만 적용</p>
    <h3 class="section-space">선택 페이지 OCR 결과</h3>
    ${result ? `<p>${badge(ocrLabels[result.status], "warn")}</p>${result.details.map(d => `<p class="subtle">${escapeHTML(d)}</p>`).join("")}<pre id="ocr-text">${escapeHTML(result.text || "인식된 문자 없음")}</pre><p class="subtle">${escapeHTML(date(result.created_at))} · AI 전달 미연결</p>
    <details><summary>이 페이지의 최근 실행 기록 (${history.length}건, 최대 5건)</summary>${history.map(item => `<div class="history-item"><strong>${escapeHTML(ocrLabels[item.status])}</strong><small>${escapeHTML(date(item.created_at))}</small><pre>${escapeHTML(item.text || item.details.join("\n"))}</pre></div>`).join("")}</details>` : '<p class="subtle">이 페이지는 OCR 미실행 상태입니다. 업로드만으로 OCR·AI 분석이 완료되지 않습니다.</p>'}`;
  document.querySelector("#file-detail .preview-image").src = state.previewURL;
}

content.addEventListener("change", event => {
  if (event.target.id !== "document-page" || !state.document) return;
  const selected = Number(event.target.value);
  const previous = state.document.page;
  if (state.busy) { event.target.value = previous; return; }
  action(event.target, async () => {
    try { await showDocument(state.document.file, selected); }
    catch (error) { event.target.value = previous; throw error; }
  });
});

content.addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button || state.busy) return;
  if (button.dataset.go) { location.hash = button.dataset.go; return; }
  if (button.dataset.project) { state.projectId = button.dataset.project; location.hash = "projects"; if (state.page === "projects") render(); return; }
  if (button.dataset.version) {
    document.querySelector("#version-detail").textContent = state.versions.find(v => v.id === button.dataset.version)?.text ?? "";
  }
  if (button.dataset.documentPage && state.document) {
    await action(button, () => showDocument(state.document.file, Number(button.dataset.documentPage)));
    return;
  }
  if (button.dataset.runOcr && state.document) {
    await action(button, async () => {
      const { file, page } = state.document;
      document.querySelector("#ocr-progress").textContent = "로컬 OCR 실행 중 … 아직 완료되지 않았습니다.";
      let failure;
      try {
        await json(projectPath(`/files/${file.id}/ocr?page=${page}`), { method: "POST" });
      } catch (error) { failure = error; }
      state.files = await json(projectPath("/files"));
      document.querySelector("#file-list").innerHTML = fileRows();
      await showDocument(file, page);
      if (failure) throw failure;
      notify("선택 페이지 OCR 실행을 마쳤습니다. 문자 결과를 원본과 대조하세요. AI 전달 미연결.");
    });
    return;
  }
  const id = button.dataset.extract || button.dataset.download || button.dataset.preview;
  if (!id) return;
  await action(button, async () => {
    const file = state.files.find(f => f.id === id);
    if (button.dataset.extract) {
      const result = await json(projectPath(`/files/${id}/extraction`));
      state.document = null;
      if (state.previewURL) { URL.revokeObjectURL(state.previewURL); state.previewURL = null; }
      document.querySelector("#file-detail").innerHTML = `<h3>${escapeHTML(file.name)}</h3><p>${badge(extractionLabels[result.status], "blue")}</p>${result.details.map(d => `<p class="subtle">${escapeHTML(d)}</p>`).join("")}<pre>${escapeHTML(result.text || "추출 텍스트 없음")}</pre>${result.tables.map(t => `<details><summary>${escapeHTML(t.name)} · ${t.rows.length}행</summary><div class="table-wrap"><table><tbody>${t.rows.map(row => `<tr>${row.map(cell => `<td>${escapeHTML(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`).join("")}<p class="subtle">AI 전달 미연결 · PLC 의미 분석 미수행</p>`;
    } else if (button.dataset.preview) {
      await showDocument(file);
    } else {
      const blob = await (await api(projectPath(`/files/${id}/original`))).blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = file.name; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  });
});

function loadWebChat() {
  if (window.WebChat?.createDirectLine && window.WebChat?.renderWebChat) return Promise.resolve();
  return new Promise((resolve, reject) => {
    document.querySelector("#webchat-library")?.remove();
    const script = document.createElement("script");
    script.id = "webchat-library";
    script.src = "https://cdn.botframework.com/botframework-webchat/4.18.0/webchat.js";
    const timer = setTimeout(() => { script.remove(); reject(new Error("Web Chat CDN 로드 시간이 초과되었습니다.")); }, 15000);
    script.onload = () => {
      clearTimeout(timer);
      if (window.WebChat?.createDirectLine && window.WebChat?.renderWebChat) resolve();
      else reject(new Error("Web Chat 4.18.0 라이브러리를 확인할 수 없습니다."));
    };
    script.onerror = () => { clearTimeout(timer); reject(new Error("Web Chat CDN 로드 실패. 네트워크·정책을 확인하세요.")); };
    document.head.append(script);
  });
}
async function connectChat(event) {
  const button = event.target;
  const route = state.route;
  button.disabled = true;
  const chatStatus = document.querySelector("#chat-status");
  chatStatus.textContent = "라이브러리 및 토큰 준비 중 · 아직 미연결";
  try {
    stopChat();
    await loadWebChat();
    if (route !== state.route) return;
    const result = await json("/chat/token", { method: "POST" });
    if (route !== state.route) return;
    const directLine = window.WebChat.createDirectLine({ token: result.token });
    state.chat = directLine;
    window.WebChat.renderWebChat({
      directLine, locale: "ko-KR", userID: "user",
      styleOptions: { accent: "#205ed3", backgroundColor: "#ffffff", hideUploadButton: true,
        botAvatarInitials: "01", userAvatarInitials: "나", bubbleBorderRadius: 6,
        bubbleFromUserBackground: "#eaf0fb", fontSizeSmall: "12px" },
    }, document.querySelector("#webchat"));
    let started = false;
    state.chatTimer = setTimeout(() => {
      if (!started && route === state.route) {
        stopChat();
        chatStatus.textContent = "연결 시간 초과 · 다시 연결하세요.";
        button.disabled = false;
      }
    }, 25000);
    state.subscription = directLine.connectionStatus$.subscribe({
      next: value => {
        if (route !== state.route) return;
        const labels = ["미연결", "Direct Line 연결 중", "연결됨 · 에이전트 실행 여부는 별도 확인", "토큰 만료 · 재연결 필요", "연결 오류 · 네트워크/설정 확인", "연결 종료"];
        chatStatus.textContent = labels[value] ?? "연결 상태 확인 필요";
        if (value === 2) {
          clearTimeout(state.chatTimer);
          document.querySelector("#disconnect-chat").disabled = false;
          if (!started) {
            started = true;
            directLine.postActivity({ type: "event", name: "startConversation", from: { id: "user" } }).subscribe({
              error: () => { if (route === state.route) chatStatus.textContent = "Direct Line 연결됨 · 시작 이벤트 전송 실패"; },
            });
          }
        } else if (value >= 3) {
          clearTimeout(state.chatTimer);
          button.disabled = false;
          document.querySelector("#disconnect-chat").disabled = true;
        }
      },
      error: () => {
        if (route === state.route) { clearTimeout(state.chatTimer); chatStatus.textContent = "연결 상태 수신 오류 · 재연결 필요"; button.disabled = false; }
      },
    });
  } catch (error) {
    stopChat();
    if (route === state.route) {
      chatStatus.textContent = error.message;
      button.disabled = false;
      notify(error.message, true);
    }
  }
}

document.querySelector("#navigation").innerHTML = pages.map((p, index) => `${index === 9 ? '<div class="nav-separator"></div>' : ""}<a class="nav-item" href="#${p[0]}"><span class="nav-icon" aria-hidden="true">${p[2]}</span>${p[1]}</a>`).join("");
document.querySelector(".skip").addEventListener("click", event => { event.preventDefault(); content.focus(); });
document.querySelector("#menu-toggle").addEventListener("click", event => {
  const opened = document.querySelector("#sidebar").classList.toggle("open");
  event.currentTarget.setAttribute("aria-expanded", String(opened));
  event.currentTarget.setAttribute("aria-label", opened ? "메뉴 닫기" : "메뉴 열기");
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    document.querySelector("#sidebar").classList.remove("open");
    document.querySelector("#menu-toggle").setAttribute("aria-expanded", "false");
    document.querySelector("#menu-toggle").setAttribute("aria-label", "메뉴 열기");
  }
});
window.addEventListener("hashchange", () => {
  if (state.busy) { state.pendingRender = true; notify("진행 중인 작업 후 화면을 전환합니다."); return; }
  render();
});
window.addEventListener("beforeunload", event => {
  if (state.busy) { event.preventDefault(); event.returnValue = ""; }
  stopChat();
});
(async () => {
  try {
    state.key = (await (await fetch("/api/session", { credentials: "omit", cache: "no-store" })).json()).session;
    if (!state.key) throw new Error("로컬 세션 응답이 올바르지 않습니다.");
    state.status = await json("/status");
    await render();
  } catch (error) {
    content.innerHTML = info(`<strong>로컬 서버에 연결하지 못했습니다.</strong><p>${escapeHTML(error.message)}</p><p>README 실행 안내에 따라 127.0.0.1에 서버를 실행하고 이 페이지를 새로고침하세요.</p>`, "danger");
  }
})();
