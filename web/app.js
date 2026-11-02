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
const draftButton = document.getElementById('draft-button');
const draftMessage = document.getElementById('draft-message');
const draftWrap = document.getElementById('draft-wrap');
const jobDescription = document.getElementById('job-description');
const draftResume = document.getElementById('draft-resume');
const missingSkills = document.getElementById('missing-skills');
const reviewCard = document.getElementById('review-card');
const reviewJob = document.getElementById('review-job');
const reviewJobLink = document.getElementById('review-job-link');
const reviewDescription = document.getElementById('review-description');
const reviewHighlights = document.getElementById('review-highlights');
const reviewMessage = document.getElementById('review-message');
const reviewOriginal = document.getElementById('review-original');
const reviewTruncated = document.getElementById('review-truncated');
const reviewDownload = document.getElementById('review-download');
const reviewApprove = document.getElementById('review-approve');
const reviewSkip = document.getElementById('review-skip');
const reviewRetry = document.getElementById('review-retry');
const approvalPrompt = document.getElementById('approval-prompt');
let providers = {};
let lastEvents = '';
let settingsVersion = 0;
let connectionTicket = '';
let searchRunning = false;
let starting = false;
let draftBusy = false;
let reviewId = '';
let reviewPdfUrl = '';
let reviewPdfText = '';
let reviewBusy = false;
let reviewPreviewBusy = false;
let reviewSequence = 0;
let statusSequence = 0;
let statusApplied = 0;
let startedEmail = '';
let runObserved = false;

function updateStart() {
  start.disabled = searchRunning || starting || !connectionTicket;
  draftButton.disabled = draftBusy || !connectionTicket;
  for (const option of form.elements.resume_mode) option.disabled = searchRunning;
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function safeJobUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && !url.username && !url.password && ['boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io', 'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com'].includes(url.hostname)) return url.href;
  } catch {}
  return '';
}

function reviewButtons() {
  const currentPdf = !!reviewPdfUrl && reviewHighlights.value === reviewPdfText;
  reviewApprove.disabled = reviewBusy || reviewPreviewBusy || !currentPdf;
  reviewSkip.disabled = reviewBusy;
  reviewRetry.disabled = reviewBusy || reviewPreviewBusy;
  reviewDownload.hidden = !currentPdf;
}

async function loadReviewPdf(id, edited = false) {
  const sequence = ++reviewSequence;
  const highlights = reviewHighlights.value;
  const lines = highlights.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (edited && (!lines.length || lines.length > 6 || lines.some(line => line.length < 3 || line.length > 200))) {
    reviewMessage.textContent = 'Use 1 to 6 highlights, each 3 to 200 characters.';
    reviewButtons();
    return;
  }
  reviewPreviewBusy = true;
  reviewMessage.textContent = 'Loading your tailored resume…';
  reviewButtons();
  try {
    let response;
    if (edited) {
      const data = new FormData();
      data.set('review_id', id);
      data.set('highlights', highlights);
      response = await fetch('/api/review-preview', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
    } else {
      response = await fetch(`/api/review-resume?review_id=${encodeURIComponent(id)}`, { headers: { 'X-App-Token': token } });
    }
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not load the resume. Try again or skip this job.');
    }
    const blob = await response.blob();
    if (id !== reviewId || sequence !== reviewSequence) return;
    if (reviewPdfUrl) URL.revokeObjectURL(reviewPdfUrl);
    reviewPdfUrl = URL.createObjectURL(blob);
    reviewPdfText = highlights;
    reviewDownload.href = reviewPdfUrl;
    reviewMessage.textContent = reviewHighlights.value === highlights ? 'Review the resume, then approve or skip this job.' : 'Highlights changed. Update the PDF before approving.';
  } catch (err) {
    if (id === reviewId && sequence === reviewSequence) reviewMessage.textContent = err.message || 'Could not load the resume. Try again or skip this job.';
  } finally {
    if (id === reviewId && sequence === reviewSequence) {
      reviewPreviewBusy = false;
      reviewButtons();
    }
  }
}

function showReview(review) {
  if (!review) {
    reviewId = '';
    reviewSequence++;
    reviewPreviewBusy = false;
    if (reviewPdfUrl) URL.revokeObjectURL(reviewPdfUrl);
    reviewPdfUrl = '';
    reviewPdfText = '';
    reviewHighlights.value = '';
    reviewOriginal.textContent = '';
    reviewDescription.textContent = '';
    reviewJob.textContent = '';
    reviewJobLink.removeAttribute('href');
    reviewDownload.removeAttribute('href');
    reviewDownload.hidden = true;
    reviewCard.hidden = true;
    reviewButtons();
    return;
  }
  if (review.id === reviewId) return;
  if (reviewPdfUrl) URL.revokeObjectURL(reviewPdfUrl);
  reviewPdfUrl = '';
  reviewPdfText = '';
  reviewSequence++;
  reviewPreviewBusy = false;
  reviewId = review.id;
  reviewDownload.hidden = true;
  reviewJob.textContent = `${review.title} · ${review.company}`;
  const jobUrl = safeJobUrl(review.url);
  reviewJobLink.hidden = !jobUrl;
  if (jobUrl) reviewJobLink.href = jobUrl;
  reviewDescription.textContent = review.description || 'No job description is available.';
  reviewOriginal.textContent = review.original_resume;
  reviewTruncated.hidden = !review.resume_truncated;
  reviewHighlights.value = review.highlights.join('\n');
  reviewCard.hidden = false;
  reviewButtons();
  loadReviewPdf(review.id);
}

async function decideReview(action) {
  if (!reviewId || reviewBusy || (action === 'approve' && (reviewPreviewBusy || !reviewPdfUrl || reviewHighlights.value !== reviewPdfText))) return;
  const id = reviewId;
  reviewBusy = true;
  reviewButtons();
  reviewMessage.textContent = action === 'approve' ? 'Approving this application…' : 'Skipping this job…';
  try {
    const data = new FormData();
    data.set('review_id', id);
    data.set('action', action);
    if (action === 'approve') data.set('highlights', reviewHighlights.value);
    const response = await fetch('/api/review', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not save your decision.');
    await status();
  } catch (err) {
    if (id === reviewId) reviewMessage.textContent = err.message || 'Could not save your decision. Try again.';
  } finally {
    reviewBusy = false;
    reviewButtons();
  }
}

reviewApprove.addEventListener('click', () => decideReview('approve'));
reviewSkip.addEventListener('click', () => decideReview('skip'));
reviewRetry.addEventListener('click', () => { if (reviewId) loadReviewPdf(reviewId, true); });
reviewHighlights.addEventListener('input', () => {
  if (!reviewId) return;
  reviewMessage.textContent = reviewHighlights.value === reviewPdfText ? 'Review the resume, then approve or skip this job.' : 'Highlights changed. Update the PDF before approving.';
  reviewButtons();
});

async function status() {
  const sequence = ++statusSequence;
  try {
    const response = await fetch('/api/status', { headers: { 'X-App-Token': token } });
    if (!response.ok) throw new Error();
    const data = await response.json();
    if (sequence < statusApplied) return;
    statusApplied = sequence;
    searchRunning = data.running;
    if (startedEmail && data.running) runObserved = true;
    if (startedEmail && !data.running && (runObserved || !starting)) {
      const email = startedEmail;
      startedEmail = '';
      document.dispatchEvent(new CustomEvent('job-search-finished', { detail: { email } }));
    }
    if (data.running && data.resume_mode && form.elements.resume_mode.value !== data.resume_mode) {
      const selected = [...form.elements.resume_mode].find(option => option.value === data.resume_mode);
      if (selected) {
        selected.checked = true;
        form.elements.consent.checked = false;
        updateConsent();
      }
    }
    updateStart();
    stop.hidden = !data.running;
    if (!stop.disabled) stop.textContent = data.review ? 'Stop search' : 'Stop after current job';
    const lastKind = data.events.at(-1)?.kind;
    statusLabel.textContent = data.running ? (data.review ? 'Waiting for approval' : 'In progress') : lastKind === 'error' ? 'Needs attention' : lastKind === 'stopped' ? 'Stopped' : data.events.length ? 'Finished' : 'Ready';
    approvalPrompt.hidden = !data.review;
    showReview(data.review);
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
      const jobUrl = safeJobUrl(item.url);
      if (jobUrl && ['needs_attention', 'uncertain', 'skipped'].includes(item.kind)) {
        const link = document.createElement('a');
        link.href = jobUrl;
        link.textContent = 'Open job ↗';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        body.append(link);
      }
      li.append(dot, body);
      events.append(li);
    }
  } catch {
    if (sequence >= statusApplied) {
      statusApplied = sequence;
      statusLabel.textContent = 'Disconnected';
    }
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
  updateConsent();
  form.elements.consent.checked = false;
  updateModel();
}

function updateConsent() {
  const providerName = providers[provider.value]?.label || 'my AI provider';
  const action = form.elements.resume_mode.value === 'review' ? 'I want to review and approve each application before it is sent.' : 'I want to apply automatically.';
  consentText.textContent = `${action} My resume text and job preferences are sent to ${providerName} for matching and, if selected, tailoring. My resume, details, and matching saved answers are sent to employers for supported applications.`;
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
  connectionTicket = '';
  updateStart();
  testResult.hidden = true;
  testResult.textContent = '';
  testResult.className = 'notice';
}

provider.addEventListener('change', () => { clearTestResult(); updateProvider(); });
model.addEventListener('change', () => { clearTestResult(); updateModel(); });
for (const option of form.elements.resume_mode) option.addEventListener('change', () => {
  form.elements.consent.checked = false;
  updateConsent();
});
for (const field of [form.elements.api_key, customModel, workspace]) {
  field.addEventListener('input', clearTestResult);
  field.addEventListener('change', clearTestResult);
}

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
    if (version === settingsVersion) {
      connectionTicket = result.verification || '';
      if (!connectionTicket) throw new Error('Connection test did not finish. Please try again.');
      testResult.textContent = result.message;
      updateStart();
    }
  } catch (err) {
    if (version === settingsVersion) {
      testResult.className = 'error';
      testResult.textContent = err instanceof TypeError || err instanceof SyntaxError ? 'Connection failed. Check your internet and try again.' : err.message || 'Connection failed. Please try again.';
    }
  } finally {
    testAI.disabled = false;
  }
});

draftButton.addEventListener('click', async () => {
  if (!connectionTicket || !form.elements.resume.reportValidity()) return;
  const description = jobDescription.value.trim();
  if (description.length < 20) {
    draftMessage.hidden = false;
    draftMessage.className = 'error';
    draftMessage.textContent = 'Paste a longer job description first.';
    return;
  }
  const version = settingsVersion;
  const resume = form.elements.resume.files[0];
  const data = new FormData();
  data.set('provider', provider.value);
  data.set('model', model.value === 'custom' ? customModel.value.trim() : model.value);
  data.set('api_key', form.elements.api_key.value);
  data.set('workspace_id', workspace.disabled ? '' : workspace.value.trim());
  data.set('connection_ticket', connectionTicket);
  data.set('resume', resume);
  data.set('description', description);
  draftBusy = true;
  updateStart();
  draftMessage.hidden = false;
  draftMessage.className = 'notice';
  draftMessage.textContent = 'Creating your draft…';
  try {
    const response = await fetch('/api/tailor-resume', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not create the draft.');
    if (version !== settingsVersion || resume !== form.elements.resume.files[0] || description !== jobDescription.value.trim()) {
      throw new Error('Your settings changed. Generate the draft again.');
    }
    draftResume.value = result.resume;
    missingSkills.textContent = result.missing_skills.length ? `Skills to check: ${result.missing_skills.join(', ')}. Add them only if you have them.` : 'No missing skills were suggested.';
    draftWrap.hidden = false;
    draftMessage.textContent = 'Draft ready. Review and edit it before saving.';
  } catch (err) {
    draftMessage.className = 'error';
    draftMessage.textContent = err instanceof TypeError || err instanceof SyntaxError ? 'Could not create the draft. Check your connection and try again.' : err.message || 'Could not create the draft.';
  } finally {
    draftBusy = false;
    updateStart();
  }
});

document.getElementById('print-draft').addEventListener('click', () => {
  document.getElementById('print-resume').textContent = draftResume.value;
  window.print();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  if (!connectionTicket || !form.reportValidity()) return;
  starting = true;
  updateStart();
  try {
    const data = new FormData(form);
    data.set('remember_questions', form.elements.remember_questions.checked ? 'true' : 'false');
    const submittedEmail = form.elements.email.value.trim();
    if (model.value === 'custom') data.set('model', customModel.value.trim());
    if (!workspace.disabled) data.set('workspace_id', workspace.value.trim());
    data.set('connection_ticket', connectionTicket);
    const response = await fetch('/api/start', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
    const result = await response.json();
    if (!response.ok) {
      if (typeof result.detail === 'string' && result.detail.startsWith('Test your AI connection')) clearTestResult();
      throw new Error(typeof result.detail === 'string' ? result.detail : 'Please check the form and try again.');
    }
    startedEmail = submittedEmail;
    runObserved = false;
    await status();
  } catch (err) {
    showError(err.message || 'Could not start the search.');
  } finally {
    starting = false;
    updateStart();
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
