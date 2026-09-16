const $ = (id) => document.getElementById(id);
let token = '', selected = null, runs = [], polling = false, mic = null;
const audioUrls = new Map();
const terminal = new Set(['completed', 'failed', 'aborted']);
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
function selectedValues(id) { return [...$(id).selectedOptions].map(o => o.value); }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {Authorization: `Bearer ${token}`, ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers}});
  if (!response.ok) { let data; try { data = await response.json(); } catch { /* display HTTP status */ } throw new Error(typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status})`); }
  return response;
}
const json = async (path, options) => (await api(path, options)).json();
function fillSelect(id, values, label, initial = false) {
  $(id).replaceChildren(...values.map((v, i) => { const option = node('option', label(v)); option.value = v.id; option.selected = initial && i === 0; return option; }));
}
function disconnect() {
  stopMic(true); token = ''; selected = null; runs = []; $('key').value = ''; $('workspace').hidden = true; $('login').hidden = false;
  audioUrls.forEach(URL.revokeObjectURL); audioUrls.clear(); notice('Disconnected. Submitted evaluations continue on the server.');
}
$('signout').onclick = disconnect;
$('login-form').onsubmit = async (event) => {
  event.preventDefault(); token = $('key').value.trim();
  try {
    const data = await json('/v1/workshop/catalog');
    fillSelect('patient', data.catalog.data.filter(m => m.patient_eligible), m => m.id, true);
    fillSelect('clinicians', data.catalog.data.filter(m => m.clinician_eligible), m => m.id, true);
    fillSelect('profiles', data.profiles.data, p => `${p.id} · ${p.name}, ${p.age}`, true);
    $('team').textContent = `${data.identity.tenant_id} / ${data.identity.principal_id}`;
    $('limits').textContent = `Up to ${data.limits.workers_per_team} simultaneous calls · ${data.limits.profiles} profiles per batch`;
    $('judge-info').textContent = `Fixed calibrated judge: ${data.catalog.judge_model}. Judge family: ${data.catalog.judge_family}.`;
    $('key').value = ''; $('login').hidden = true; $('workspace').hidden = false;
    await refresh(); notice('Connected. Choose your profiles and clinician models.');
  } catch (error) { token = ''; notice(error.message, true); }
};
$('mode').onchange = () => {
  const spoken = $('mode').value === 'spoken'; $('voices').hidden = !spoken;
  $('mode-help').textContent = spoken ? 'Generate speech and transcribe each turn before the other model replies. This measures the spoken experience, not the canonical text benchmark.' : 'Original MindEval dialogue and scoring. Untouched text runs can be compared together.';
};
$('create-form').onsubmit = async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const profiles = selectedValues('profiles'); if (profiles.length > 20) throw new Error('Choose at most 20 patient profiles.');
    const data = await json('/v1/workshop/runs', {method: 'POST', headers: {'Idempotency-Key': crypto.randomUUID()}, body: JSON.stringify({
      profile_ids: profiles, patient_model: $('patient').value, clinician_models: selectedValues('clinicians'),
      mode: $('mode').value, max_turns: Number($('rounds').value), max_completion_tokens: Number($('tokens').value),
      patient_voice: $('patient-voice').value, clinician_voice: $('clinician-voice').value,
    })});
    selected = data.data[0]?.id; await refresh(); notice(`${data.data.length} evaluations accepted. Extra work stays in the queue.`);
  } catch (error) { notice(error.message, true); } finally { button.disabled = false; }
};
function renderRuns() {
  $('runs').replaceChildren();
  if (!runs.length) $('runs').append(node('p', 'No runs yet.'));
  for (const run of runs) {
    const button = node('button', undefined, `run-card ${run.id === selected ? 'selected' : ''}`);
    const c = run.state.config;
    button.append(node('strong', c.clinician_model.split('/').pop()), node('span', `${c.profile_id} · ${c.mode} · ${run.status}`), node('small', run.id));
    button.onclick = async () => { selected = run.id; renderRuns(); await showRun(); }; $('runs').append(button);
  }
  const table = node('table'); const head = node('tr'); ['Clinician', 'Profile', 'Mean score / 6', 'Run'].forEach(v => head.append(node('th', v))); table.append(head);
  for (const run of runs.filter(r => r.status === 'completed' && r.state.benchmark_eligible)) {
    const row = node('tr'); const s = run.state;
    [s.config.clinician_model, s.config.profile_id, Number(s.judgment.overall_score).toFixed(2), run.id.slice(0, 8)].forEach(v => row.append(node('td', v))); table.append(row);
  }
  $('comparison').replaceChildren(table);
}
async function refresh() {
  if (polling || !token) return; polling = true;
  try { runs = (await json('/v1/workshop/runs')).data; renderRuns(); if (selected) await showRun(); }
  catch (error) { notice(`${error.message}. Existing runs are retained; retry Refresh.`, true); }
  finally { polling = false; }
}
$('refresh').onclick = refresh;
setInterval(refresh, 3000);
let renderedVersion = '';
async function showRun() {
  const id = selected; const run = await json(`/v1/workshop/runs/${id}`); if (id !== selected) return;
  $('detail').hidden = false; $('run-status').textContent = `${run.status} · ${run.id}`;
  $('run-title').textContent = `${run.state.config.profile_id} · ${run.state.config.clinician_model.split('/').pop()}`;
  const s = run.state; const fingerprint = `${id}:${run.version}`; if (fingerprint === renderedVersion) return; renderedVersion = fingerprint;
  $('run-labels').replaceChildren(node('span', s.config.mode === 'canonical' ? 'Text benchmark' : 'Spoken experience', 'pill'), node('span', s.intervened ? 'Human intervention · excluded from default comparison' : 'No interventions', 'pill'));
  if (s.error) $('run-labels').append(node('p', `${s.error.code}: ${s.error.message}`, 'error'));
  if (run.status === 'interrupted') $('run-labels').append(node('p', 'Execution was interrupted. Inspect the last event, then Resume. An in-flight provider call may have incurred usage.', 'error'));
  document.querySelectorAll('[data-action]').forEach(b => { b.disabled = terminal.has(run.status); });
  $('transcript').replaceChildren();
  for (const turn of s.transcript) {
    const article = node('article', undefined, `turn ${turn.role}`);
    article.append(node('strong', `${turn.role} · ${turn.human ? 'human' : turn.seed ? 'initial greeting' : 'model'}`), node('p', turn.content));
    if (turn.generated_content) { const d = node('details'); d.append(node('summary', 'Original generated text'), node('p', turn.generated_content)); article.append(d); }
    if (turn.completion?.telemetry) article.append(node('small', `Queue ${Number(turn.completion.telemetry.queue_ms || 0).toFixed(0)} ms · response ${Number(turn.completion.telemetry.latency_ms || 0).toFixed(0)} ms`));
    if (turn.audio_url) {
      const play = node('button', 'Load recording', 'secondary');
      play.onclick = async () => { try { let url = audioUrls.get(turn.audio_url); if (!url) { url = URL.createObjectURL(await (await api(turn.audio_url)).blob()); audioUrls.set(turn.audio_url, url); } const player = node('audio'); player.controls = true; player.src = url; play.replaceWith(player); await player.play(); } catch (error) { notice(error.message, true); } }; article.append(play);
    }
    $('transcript').append(article);
  }
  $('scores').replaceChildren();
  if (s.judgment?.judgment) {
    $('scores').append(node('h3', 'Five-criterion judgment'));
    const grid = node('div', undefined, 'score-grid');
    Object.entries(s.judgment.judgment).forEach(([name, score]) => { const box = node('div'); box.append(node('strong', Number(score).toFixed(2)), node('span', name)); grid.append(box); }); $('scores').append(grid);
  }
  if (s.classification) { const box = node('details'); box.append(node('summary', 'MindGuard · observational classification, not a clinical verdict'), node('pre', JSON.stringify(s.classification, null, 2))); $('scores').append(box); }
  const events = await json(`/v1/workshop/runs/${id}/events`); if (selected === id) $('events').textContent = events.data.map(e => `${e.created_at} ${e.kind}\n${JSON.stringify(e.data, null, 2)}`).join('\n\n');
}
async function command(action, text) {
  if (!selected) return;
  try { await json(`/v1/workshop/runs/${selected}/interventions`, {method: 'POST', body: JSON.stringify({action, role: $('role').value, ...(text ? {text} : {})})}); $('message').value = ''; await refresh(); notice(`Recorded: ${action}. This run is labeled as intervened.`); }
  catch (error) { notice(error.message, true); }
}
document.querySelectorAll('[data-action]').forEach(b => { b.onclick = () => command(b.dataset.action); });
$('nudge').onclick = () => command('nudge', $('message').value);
$('intervention-form').onsubmit = e => { e.preventDefault(); command('say', $('message').value); };
$('download').onclick = async () => {
  try { const url = URL.createObjectURL(await (await api(`/v1/workshop/runs/${selected}/report`)).blob()); const a = node('a'); a.href = url; a.download = `${selected}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch (error) { notice(error.message, true); }
};
async function stopMic(cancel = false) {
  if (!mic) return; const current = mic; mic = null;
  current.processor?.disconnect(); current.source?.disconnect(); current.stream?.getTracks().forEach(t => t.stop()); await current.context?.close();
  if (current.socket?.readyState === WebSocket.OPEN) current.socket.send(JSON.stringify({type: cancel ? 'session.cancel' : 'session.finish'}));
  $('stop-microphone').hidden = true; $('microphone').disabled = false; $('mic-status').textContent = cancel ? 'Recording cancelled.' : 'Finishing transcription…';
}
$('stop-microphone').onclick = () => stopMic();
$('microphone').onclick = async () => {
  if (!selected || mic) return;
  try {
    // The server validates current role ownership again before opening ASR.
    const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true}});
    const context = new AudioContext({sampleRate: 16000});
    if (context.sampleRate !== 16000) { stream.getTracks().forEach(t => t.stop()); await context.close(); throw new Error('This browser cannot record 16 kHz PCM. Use typed takeover or another browser.'); }
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/v1/workshop/runs/${selected}/microphone`);
    mic = {stream, context, socket}; $('microphone').disabled = true;
    socket.onopen = () => socket.send(JSON.stringify({token, model: 'nemotron-speech-en-0-6b'}));
    socket.onmessage = async ({data}) => {
      const event = JSON.parse(data);
      if (event.type === 'session.ready' && mic?.socket === socket) {
        const source = context.createMediaStreamSource(stream); const processor = context.createScriptProcessor(2048, 1, 1);
        mic.source = source; mic.processor = processor; source.connect(processor); processor.connect(context.destination);
        processor.onaudioprocess = (e) => { const input = e.inputBuffer.getChannelData(0), buffer = new ArrayBuffer(input.length * 2), view = new DataView(buffer); for (let i = 0; i < input.length; i++) view.setInt16(i * 2, Math.max(-1, Math.min(1, input[i])) * 32767, true); if (socket.readyState === WebSocket.OPEN) socket.send(buffer); };
        $('stop-microphone').hidden = false; $('mic-status').textContent = 'Recording · finish to submit your turn.';
      } else if (event.type.startsWith('transcript.')) $('mic-status').textContent = event.text || '';
      else if (event.type === 'workshop.message_submitted') { $('mic-status').textContent = 'Your spoken turn was submitted and retained.'; await refresh(); }
      else if (event.type === 'workshop.error' || event.type === 'session.error') { notice(event.message || 'Speech session failed; use typed takeover.', true); await stopMic(true); }
    };
    socket.onerror = () => notice('Microphone connection failed. No typed message was submitted.', true);
    socket.onclose = () => { if (mic?.socket === socket) stopMic(true); };
  } catch (error) { await stopMic(true); notice(error.message, true); }
};
