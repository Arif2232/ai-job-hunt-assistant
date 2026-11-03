(() => {
  const email = form.elements.email;
  const remember = form.elements.remember_questions;
  const resumeInput = form.elements.resume;
  const resumeUpload = document.getElementById('resume-upload');
  const savedResumeCard = document.getElementById('saved-resume');
  const savedResumeName = document.getElementById('saved-resume-name');
  const resumeStatus = document.getElementById('resume-status');
  const changeResume = document.getElementById('change-resume');
  const revertResume = document.getElementById('resume-revert');
  const saveError = document.getElementById('settings-save-error');
  const names = new Set(['email', 'role', 'location', 'region', 'relocation', 'experience_flexibility',
    'resume_mode', 'remember_questions', 'provider', 'model', 'custom_model', 'workspace_id']);
  const touched = new Set();
  const revisions = {};
  const outstanding = {};
  let pending = {};
  let timer;
  let writes = Promise.resolve();
  let initialized = false;
  let applying = false;
  let resumeSequence = 0;
  let savedResume = null;
  let editingResume = true;
  let selectionOwner = '';
  let uploadOwner = '';
  let uploadFile = null;
  let uploads = Promise.resolve();

  const emailKey = value => value.trim().toLowerCase();
  const validEmail = () => !!email.value.trim() && email.checkValidity();

  function showSaveError(show) {
    saveError.hidden = !show;
    saveError.textContent = show ? 'Could not save your settings on this computer. Check storage access and try again.' : '';
  }

  function valueFor(name) {
    return name === 'remember_questions' ? remember.checked : form.elements[name].value;
  }

  function record(name) {
    if (!names.has(name)) return;
    touched.add(name);
    if (name === 'email' && !remember.checked) return;
    revisions[name] = (revisions[name] || 0) + 1;
    pending[name] = valueFor(name);
    outstanding[name] = { value: pending[name], revision: revisions[name] };
    if (initialized) {
      clearTimeout(timer);
      timer = setTimeout(() => flush().catch(() => {}), 250);
    }
  }

  function recordSelection() {
    for (const name of ['provider', 'model', 'custom_model', 'workspace_id']) record(name);
  }

  function flush() {
    clearTimeout(timer);
    if (!Object.keys(pending).length) return writes;
    const changes = pending;
    pending = {};
    const versions = Object.fromEntries(Object.keys(changes).map(name => [name, revisions[name]]));
    const operation = (async () => {
      const response = await fetch('/api/settings/preferences', {
        method: 'POST', headers: { 'X-App-Token': token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes, client_id: saveClientId, revisions: versions }), keepalive: true,
      });
      if (!response.ok) throw new Error('Could not save settings.');
      for (const name of Object.keys(changes)) {
        if (outstanding[name]?.revision === versions[name]) delete outstanding[name];
      }
      if (!Object.keys(pending).length) showSaveError(false);
    })();
    const handled = operation.catch(error => {
      for (const [name, value] of Object.entries(changes)) {
        if (revisions[name] === versions[name] && !(name in pending)) pending[name] = value;
      }
      showSaveError(true);
      throw error;
    });
    writes = Promise.all([writes.catch(() => {}), handled]).then(() => {});
    return writes;
  }

  function preferenceChanged(event) {
    if (applying) return;
    const name = event.target.name;
    if (!names.has(name)) return;
    if (name === 'remember_questions') {
      if (!remember.checked) {
        delete pending.email;
        delete outstanding.email;
        revisions.email = (revisions.email || 0) + 1;
      }
      else record('email');
    }
    if (name === 'provider') recordSelection();
    else {
      record(name);
      if (name === 'model') record('custom_model');
    }
  }

  form.addEventListener('input', preferenceChanged);
  form.addEventListener('change', preferenceChanged);
  apiKey.addEventListener('input', () => { if (!initialized) recordSelection(); });
  function saveOutstanding() {
    const entries = Object.entries(outstanding);
    if (!entries.length) return;
    const changes = Object.fromEntries(entries.map(([name, item]) => [name, item.value]));
    const versions = Object.fromEntries(entries.map(([name, item]) => [name, item.revision]));
    const body = JSON.stringify({ changes, client_id: saveClientId, revisions: versions, _token: token });
    if (!navigator.sendBeacon || !navigator.sendBeacon('/api/settings/preferences', new Blob([body], { type: 'application/json' }))) {
      fetch('/api/settings/preferences', { method: 'POST', headers: { 'X-App-Token': token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes, client_id: saveClientId, revisions: versions }), keepalive: true }).catch(() => {});
    }
  }
  window.addEventListener('pagehide', saveOutstanding);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') saveOutstanding(); });
  window.addEventListener('beforeunload', event => {
    if ((resumeBusy && uploadOwner) || Object.keys(outstanding).length || window.hasPendingApplicationDetails?.()) {
      saveOutstanding();
      window.savePendingApplicationDetails?.();
      event.preventDefault();
      event.returnValue = '';
    }
  });

  function showResumeStatus(text, isError = false) {
    resumeStatus.hidden = !text;
    resumeStatus.className = isError ? 'error' : 'model-hint';
    resumeStatus.textContent = text;
  }

  function renderResume() {
    const current = savedResume && savedResume.owner === emailKey(email.value);
    const ready = current && !editingResume;
    savedResumeCard.hidden = !current;
    savedResumeName.textContent = current ? savedResume.filename : '';
    resumeUpload.hidden = !!ready;
    resumeInput.disabled = !!ready || resumeBusy;
    resumeInput.required = !ready;
    changeResume.disabled = resumeBusy;
    revertResume.hidden = !current || !editingResume;
    revertResume.disabled = resumeBusy;
    if (ready) resumeInput.setCustomValidity('');
  }

  async function saveSelectedResume() {
    const file = resumeInput.files[0];
    if (!file) return;
    if (file.size > 5_000_000) {
      resumeInput.value = '';
      selectionOwner = '';
      resumeInput.setCustomValidity('Choose a PDF resume smaller than 5 MB.');
      showResumeStatus('Choose a PDF resume smaller than 5 MB.', true);
      return;
    }
    if (!validEmail()) {
      selectionOwner = '';
      showResumeStatus('Enter your application email above to save your resume.', true);
      return;
    }
    const owner = emailKey(email.value);
    if (resumeBusy && uploadOwner === owner && uploadFile === file) return;
    selectionOwner = owner;
    const version = ++resumeSequence;
    uploadOwner = owner;
    uploadFile = file;
    resumeBusy = true;
    showResumeStatus('Saving your resume…');
    renderResume();
    updateStart();
    const data = new FormData();
    data.set('email', email.value.trim());
    data.set('resume', file);
    const operation = uploads.catch(() => {}).then(async () => {
      const response = await fetch('/api/settings/resume', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not save the resume.');
      return result;
    });
    uploads = operation.catch(() => {});
    try {
      const result = await operation;
      if (version !== resumeSequence || owner !== emailKey(email.value)) return;
      if (!result.filename || !/^[0-9a-f]{64}$/.test(result.digest || '')) throw new Error('Could not confirm the saved resume.');
      savedResume = { owner, filename: result.filename, digest: result.digest };
      editingResume = false;
      selectionOwner = '';
      resumeInput.value = '';
      resumeInput.setCustomValidity('');
      showResumeStatus('');
    } catch (error) {
      if (version !== resumeSequence || owner !== emailKey(email.value)) return;
      resumeInput.value = '';
      selectionOwner = '';
      resumeInput.setCustomValidity('Choose a valid PDF resume.');
      showResumeStatus(error instanceof TypeError ? 'Could not save your resume. Check that the app is open.' : error.message || 'Could not save the resume. Choose the file again.', true);
    } finally {
      if (version === resumeSequence) {
        resumeBusy = false;
        uploadOwner = '';
        uploadFile = null;
        renderResume();
        updateStart();
      }
    }
  }

  async function loadResume() {
    if (!validEmail()) return;
    if (resumeInput.files[0]) return saveSelectedResume();
    const owner = emailKey(email.value);
    if (savedResume?.owner === owner && editingResume) return;
    const version = ++resumeSequence;
    resumeBusy = true;
    updateStart();
    try {
      await uploads;
      if (version !== resumeSequence) return;
      const data = new FormData();
      data.set('email', email.value.trim());
      const response = await fetch('/api/settings/resume-info', { method: 'POST', headers: { 'X-App-Token': token }, body: data });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error('Could not load your saved resume.');
      if (version !== resumeSequence || owner !== emailKey(email.value)) return;
      if (result.filename && !/^[0-9a-f]{64}$/.test(result.digest || '')) throw new Error('Could not confirm the saved resume.');
      savedResume = result.filename ? { owner, filename: result.filename, digest: result.digest } : null;
      editingResume = !savedResume;
      showResumeStatus('');
    } catch (error) {
      if (version === resumeSequence) showResumeStatus(error.message || 'Could not load your saved resume.', true);
    } finally {
      if (version === resumeSequence) {
        resumeBusy = false;
        renderResume();
        updateStart();
      }
    }
  }

  email.addEventListener('input', () => {
    const owner = emailKey(email.value);
    if (savedResume && savedResume.owner !== owner) savedResume = null;
    if (selectionOwner && selectionOwner !== owner) {
      resumeInput.value = '';
      selectionOwner = '';
    }
    if (uploadOwner && uploadOwner !== owner) {
      resumeSequence++;
      uploadOwner = '';
      uploadFile = null;
      resumeBusy = false;
      updateStart();
    }
    if (!savedResume) editingResume = true;
    resumeInput.setCustomValidity('');
    showResumeStatus('');
    renderResume();
  });
  email.addEventListener('change', loadResume);
  resumeInput.addEventListener('change', () => {
    resumeInput.setCustomValidity('');
    saveSelectedResume();
  });
  changeResume.addEventListener('click', () => {
    editingResume = true;
    resumeSequence++;
    showResumeStatus('');
    renderResume();
    resumeInput.focus();
  });
  revertResume.addEventListener('click', () => {
    if (resumeBusy) return;
    resumeSequence++;
    resumeInput.value = '';
    selectionOwner = '';
    editingResume = false;
    showResumeStatus('');
    renderResume();
  });

  function checkResume() {
    if (resumeBusy) return false;
    if (!validEmail()) return email.reportValidity();
    if (savedResume && savedResume.owner === emailKey(email.value) && !editingResume) return true;
    return resumeInput.reportValidity();
  }

  async function initialize() {
    await optionsReady;
    let values = {};
    try {
      const response = await fetch('/api/settings/preferences', { headers: { 'X-App-Token': token } });
      if (!response.ok) throw new Error();
      values = await response.json();
      if (!values || typeof values !== 'object' || Array.isArray(values)) throw new Error();
    } catch { values = {}; showSaveError(true); }
    applying = true;
    for (const name of ['role', 'location', 'region', 'relocation', 'experience_flexibility']) {
      if (!touched.has(name) && typeof values[name] === 'string') form.elements[name].value = values[name];
    }
    if (!searchRunning && !touched.has('resume_mode') && values.resume_mode) {
      const option = [...form.elements.resume_mode].find(item => item.value === values.resume_mode);
      if (option) option.checked = true;
    }
    if (!touched.has('provider') && [...provider.options].some(option => option.value === values.provider)) {
      provider.value = values.provider;
      updateProvider();
    }
    if (!touched.has('model') && values.model) {
      model.value = [...model.options].some(option => option.value === values.model) ? values.model : 'custom';
      if (model.value === 'custom' && values.model !== 'custom') customModel.value = values.model;
    }
    const fallbackModel = !touched.has('model') && values.model && values.model !== 'custom' && model.value === 'custom';
    if (!touched.has('custom_model') && !fallbackModel && typeof values.custom_model === 'string') customModel.value = values.custom_model;
    if (!touched.has('workspace_id') && typeof values.workspace_id === 'string') workspace.value = values.workspace_id;
    updateModel();
    updateConsent();
    if (!touched.has('remember_questions') && typeof values.remember_questions === 'boolean') remember.checked = values.remember_questions;
    remember.dispatchEvent(new Event('change', { bubbles: true }));
    if (!touched.has('email') && typeof values.email === 'string') email.value = values.email;
    email.dispatchEvent(new Event('change', { bubbles: true }));
    applying = false;
    initialized = true;
    restoringSettings = false;
    updateAiSummary();
    updateStart();
    if (Object.keys(pending).length) flush().catch(() => {});
    await refreshCredentialStatus({ collapse: true });
  }

  window.appPersistence = { flush, checkResume, resumeVersion: () => resumeSequence,
    resumeDigest: () => savedResume?.owner === emailKey(email.value) && !editingResume ? savedResume.digest : '',
    reloadResume: loadResume, rememberAiSelection: recordSelection };
  initialize();
})();
