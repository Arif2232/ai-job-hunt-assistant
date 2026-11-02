const token = document.querySelector('meta[name="app-token"]').content;
const form = document.getElementById('setup');
const error = document.getElementById('error');
const start = document.getElementById('start');
const stop = document.getElementById('stop');
const statusLabel = document.getElementById('status');
const events = document.getElementById('events');
const empty = document.getElementById('empty');
const provider = document.getElementById('provider');
const model = document.getElementById('model');
const customModel = document.getElementById('custom-model');
const customWrap = document.getElementById('custom-model-wrap');
const workspace = document.getElementById('workspace-id');
const workspaceWrap = document.getElementById('workspace-wrap');
const modelPrice = document.getElementById('model-price');
const keyLink = document.getElementById('key-link');
const consentText = document.getElementById('consent-text');
const privacyNote = document.getElementById('privacy-note');
const testAI = document.getElementById('test-ai');
const testResult = document.getElementById('test-ai-result');
let providers = {};
let lastEvents = '';
let settingsVersion = 0;

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

async function status() {
  try {
    const response = await fetch('/api/status', { headers: { 'X-App-Token': token } });
    if (!response.ok) throw new Error();
    const data = await response.json();
    start.disabled = data.running;
    stop.hidden = !data.running;
    if (!data.running) stop.textContent = 'Stop after current job';
    const lastKind = data.events.at(-1)?.kind;
    statusLabel.textContent = data.running ? 'In progress' : lastKind === 'error' ? 'Needs attention' : lastKind === 'stopped' ? 'Stopped' : data.events.length ? 'Finished' : 'Ready';
    const snapshot = JSON.stringify(data.events);
    if (snapshot === lastEvents) return;
    lastEvents = snapshot;
    events.replaceChildren();
    empty.hidden = data.events.length > 0;
    for (const item of data.events.slice().reverse()) {
      const li = document.createElement('li');
      const dot = document.createElement('span');
      dot.className = `event-dot ${item.kind === 'submitted' ? 'success' : item.kind === 'error' || item.kind === 'uncertain' ? 'warning' : ''}`;
      const body = document.createElement('div');
      if (item.title) {
        const name = document.createElement('strong');
        name.textContent = `${item.title} · ${item.company}`;
        body.append(name);
        if (item.posted_at) {
          const posted = document.createElement('p');
          const date = new Date(item.posted_at);
          if (!Number.isNaN(date.getTime())) posted.textContent = `Posted ${date.toLocaleDateString()}`;
          body.append(posted);
        }
      }
      const message = document.createElement('p');
      message.textContent = item.message;
      body.append(message);
      if (item.url && ['needs_attention', 'uncertain', 'skipped'].includes(item.kind)) {
        try {
          const url = new URL(item.url);
          if (url.protocol === 'https:' && ['boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io', 'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com'].includes(url.hostname)) {
            const link = document.createElement('a');
            link.href = url.href;
            link.textContent = 'Open job ↗';
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            body.append(link);
          }
        } catch {}
      }
      li.append(dot, body);
      events.append(li);
    }
  } catch {
    statusLabel.textContent = 'Disconnected';
  }
}

function updateModel() {
  const choice = providers[provider.value];
  const selected = choice?.models.find(item => item.id === model.value);
  customWrap.hidden = model.value !== 'custom';
  customModel.disabled = customWrap.hidden;
  customModel.required = !customWrap.hidden;
  modelPrice.replaceChildren();
  if (!choice) return;
  modelPrice.append(document.createTextNode(selected ? `${selected.price} per 1M tokens. ` : 'Check the provider for current model pricing. '));
  const link = document.createElement('a');
  link.href = choice.pricing_url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = 'Check current pricing ↗';
  modelPrice.append(link);
}

function updateProvider() {
  const choice = providers[provider.value];
  model.replaceChildren();
  if (!choice) return;
  for (const item of choice.models) model.add(new Option(item.label, item.id));
  model.add(new Option('Other model…', 'custom'));
  form.elements.api_key.value = '';
  workspace.value = '';
  keyLink.href = choice.key_url;
  keyLink.textContent = `Get an API key for ${choice.label} ↗`;
  workspaceWrap.hidden = provider.value !== 'claude';
  workspace.disabled = workspaceWrap.hidden;
  privacyNote.hidden = provider.value !== 'gemini';
  consentText.textContent = `I want to apply automatically. My resume text and job preferences are sent to ${choice.label} for matching, and my resume and details are sent to employers for supported applications.`;
  form.elements.consent.checked = false;
  updateModel();
}

async function loadOptions() {
  try {
    const response = await fetch('/api/options', { headers: { 'X-App-Token': token } });
    if (!response.ok) throw new Error();
    providers = await response.json();
    provider.replaceChildren();
    for (const [id, item] of Object.entries(providers)) provider.add(new Option(item.label, id));
    updateProvider();
  } catch {
    provider.replaceChildren(new Option('Could not load providers', ''));
    showError('Could not load AI providers. Refresh the page.');
  }
}

function clearTestResult() {
  settingsVersion++;
  testResult.hidden = true;
  testResult.textContent = '';
}

provider.addEventListener('change', () => { clearTestResult(); updateProvider(); });
model.addEventListener('change', () => { clearTestResult(); updateModel(); });
for (const field of [form.elements.api_key, customModel, workspace]) field.addEventListener('input', clearTestResult);

testAI.addEventListener('click', async () => {
  clearTestResult();
  if (!form.elements.api_key.reportValidity() || !customModel.reportValidity() || !workspace.reportValidity()) return;
  const version = settingsVersion;
  const data = new FormData();
  data.set('provider', provider.value);
  data.set('model', model.value === 'custom' ? customModel.value.trim() : model.value);
  data.set('api_key', form.elements.api_key.value);
  if (!workspace.disabled) data.set('workspace_id', workspace.value.trim());
  testAI.disabled = true;
  testResult.hidden = false;
  testResult.textContent = 'Testing your AI connection…';
  try {
    const response = await fetch('/api/test-ai', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not test the connection.');
    if (version === settingsVersion) testResult.textContent = result.message;
  } catch (err) {
    if (version === settingsVersion) testResult.textContent = err.message || 'Could not test the connection.';
  } finally {
    testAI.disabled = false;
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  if (!form.reportValidity()) return;
  start.disabled = true;
  try {
    const data = new FormData(form);
    if (model.value === 'custom') data.set('model', customModel.value.trim());
    const response = await fetch('/api/start', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Please check the form and try again.');
    await status();
  } catch (err) {
    showError(err.message || 'Could not start the search.');
    start.disabled = false;
  }
});

stop.addEventListener('click', async () => {
  stop.disabled = true;
  try {
    await fetch('/api/stop', { method: 'POST', headers: { 'X-App-Token': token } });
    stop.textContent = 'Stopping…';
  } catch { showError('Could not stop the search.'); }
  finally { stop.disabled = false; }
});

loadOptions();
status();
setInterval(status, 2500);
