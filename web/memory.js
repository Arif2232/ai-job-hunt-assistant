(() => {
  const form = document.getElementById('setup');
  const token = document.querySelector('meta[name="app-token"]').content;
  const emailField = form.elements.email;
  const message = document.getElementById('memory-message');
  const container = document.getElementById('memory-questions');
  const prompt = document.getElementById('memory-prompt');
  const profileFields = ['first_name', 'last_name', 'phone', 'current_city', 'current_country', 'preferred_first_name', 'phone_country'];
  const dirty = new Set();
  let loadedEmail = '';
  let profileLoadedEmail = '';
  let requestVersion = 0;
  let editVersion = 0;
  let questions = [];
  let pendingSummary = null;
  let loadedProfileValues = {};

  function emailKey(value) { return value.trim().toLowerCase(); }
  function setMessage(value, isError = false) {
    message.textContent = value;
    message.className = isError ? 'error' : '';
  }
  function currentEmail(showError = true) {
    if (!emailField.value.trim() || !emailField.checkValidity()) {
      if (showError) setMessage('Enter a valid application email above first.', true);
      return '';
    }
    return emailField.value.trim();
  }
  async function post(path, values) {
    const data = new FormData();
    for (const [key, value] of Object.entries(values)) data.set(key, String(value));
    let response;
    try { response = await fetch(path, { method: 'POST', headers: { 'X-App-Token': token }, body: data }); }
    catch { throw new Error('Could not reach the app. Check that it is still open.'); }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not update memory.');
    return result;
  }
  function updatePending() {
    const count = questions.filter(item => item.pending).length;
    prompt.hidden = count === 0 || !loadedEmail || emailKey(emailField.value) !== loadedEmail;
    if (pendingSummary) pendingSummary.textContent = `Questions and saved answers (${count} need attention)`;
  }
  function questionRow(item, email) {
    const row = document.createElement('div');
    row.className = 'memory-question';
    const title = document.createElement('h3');
    title.textContent = item.label;
    row.append(title);
    const context = document.createElement('p');
    context.textContent = item.company ? [item.company, item.title, item.location].filter(Boolean).join(' · ') : 'For the same question at any employer';
    row.append(context);
    const url = safeJobUrl(item.url);
    if (url) {
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Open job ↗';
      row.append(link);
    }
    const rowMessage = document.createElement('p');
    rowMessage.setAttribute('role', 'status');
    rowMessage.textContent = item.pending ? 'Needs your answer or review.' : 'Saved for next time.';
    let answer, reuse;
    if (item.kind === 'manual') {
      rowMessage.textContent = 'Complete this directly on the employer’s form.';
    } else {
      const label = document.createElement('label');
      label.textContent = 'Your answer';
      if (item.kind === 'choice' && item.options.length) {
        answer = document.createElement('select');
        answer.add(new Option('Choose an answer…', ''));
        for (const option of item.options) answer.add(new Option(option, option));
        answer.value = item.answer;
      } else {
        answer = document.createElement('textarea');
        answer.rows = 2;
        answer.maxLength = 2000;
        answer.value = item.answer;
        if (item.kind === 'choice') answer.placeholder = 'Enter the option exactly as the employer shows it';
      }
      label.append(answer);
      row.append(label);
      const reuseLabel = document.createElement('label');
      reuseLabel.className = 'consent';
      reuse = document.createElement('input');
      reuse.type = 'checkbox';
      reuse.checked = item.reuse_globally;
      reuse.disabled = !item.can_reuse_globally || !item.company;
      const reuseLocked = reuse.disabled;
      const reuseText = document.createElement('span');
      reuseText.textContent = !item.company ? 'This answer is used for the same question at any employer.' :
        item.can_reuse_globally ? 'Also use for this exact question at other employers and locations.' :
        'This answer depends on the job or location and stays limited to this job.';
      reuseLabel.append(reuse, reuseText);
      row.append(reuseLabel);
      if (item.company && item.can_reuse_globally) {
        const scope = document.createElement('small');
        scope.textContent = 'Otherwise, this answer is used only at this employer and location.';
        row.append(scope);
      }
      const changed = () => { dirty.add(item.id); editVersion++; };
      answer.addEventListener('input', changed);
      answer.addEventListener('change', changed);
      reuse.addEventListener('change', changed);
    }
    const actions = document.createElement('div');
    actions.className = 'actions';
    if (item.kind !== 'manual') {
      const save = document.createElement('button');
      save.type = 'button';
      save.className = 'secondary';
      save.textContent = 'Save answer';
      save.addEventListener('click', async () => {
        if (emailKey(emailField.value) !== emailKey(email)) return setMessage('Load answers for the current email first.', true);
        save.disabled = true;
        answer.disabled = true;
        reuse.disabled = true;
        requestVersion++;
        try {
          const result = await post('/api/memory/answer', { email, question_id: item.id, answer: answer.value.trim(), reuse_globally: reuse.checked });
          if (emailKey(emailField.value) !== emailKey(email)) return;
          item.answer = answer.value.trim();
          item.reuse_globally = reuse.checked;
          item.pending = false;
          dirty.delete(item.id);
          forget.textContent = 'Remove';
          rowMessage.textContent = result.message;
          updatePending();
        } catch (err) { rowMessage.textContent = err.message || 'Could not save this answer.'; }
        finally { save.disabled = false; answer.disabled = false; reuse.disabled = reuseLocked; }
      });
      actions.append(save);
    }
    const forget = document.createElement('button');
    forget.type = 'button';
    forget.className = 'secondary';
    forget.textContent = item.pending ? 'Dismiss' : 'Remove';
    forget.addEventListener('click', async () => {
      if (emailKey(emailField.value) !== emailKey(email)) return setMessage('Load answers for the current email first.', true);
      forget.disabled = true;
      requestVersion++;
      try {
        await post('/api/memory/forget', { email, question_id: item.id });
        if (emailKey(emailField.value) !== emailKey(email)) return;
        questions = questions.filter(question => question.id !== item.id);
        dirty.delete(item.id);
        row.remove();
        updatePending();
      } catch (err) { rowMessage.textContent = err.message || 'Could not remove this answer.'; }
      finally { forget.disabled = false; }
    });
    actions.append(forget);
    row.append(actions, rowMessage);
    return row;
  }
  function render(items, email) {
    questions = items;
    dirty.clear();
    container.replaceChildren();
    pendingSummary = null;
    if (items.length) {
      const group = document.createElement('details');
      group.open = items.some(item => item.pending);
      const summary = document.createElement('summary');
      pendingSummary = summary;
      group.append(summary);
      for (const item of items) group.append(questionRow(item, email));
      container.append(group);
    }
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'memory-note';
      empty.textContent = 'No saved answers or unanswered questions yet.';
      container.append(empty);
    }
    updatePending();
  }
  async function loadMemory({ fillProfile = true, automatic = false } = {}) {
    const email = currentEmail(!automatic);
    if (!email) return;
    if (dirty.size) {
      if (automatic) return setMessage(emailKey(email) !== loadedEmail ? 'Return to the previous email to save edits, or load memory to discard them.' : 'Save your edited answers, then load memory to see any new questions.');
      if (!window.confirm('Discard unsaved answer edits and reload memory?')) return;
    }
    const version = ++requestVersion;
    const edits = editVersion;
    try {
      const result = await post('/api/memory/list', { email });
      if (version !== requestVersion || emailKey(emailField.value) !== emailKey(email)) return;
      if (edits !== editVersion) return setMessage('Save your edited answers, then load memory to see any new questions.');
      const previousProfile = loadedEmail === emailKey(email) ? loadedProfileValues : {};
      loadedEmail = emailKey(email);
      container.hidden = false;
      if (fillProfile) {
        loadedProfileValues = {};
        for (const name of profileFields) {
          const field = form.elements[name];
          if (!field.value.trim() || (previousProfile[name] && field.value === previousProfile[name])) {
            field.value = result.profile[name] || '';
            if (field.value) loadedProfileValues[name] = field.value;
          }
        }
        profileLoadedEmail = emailKey(email);
      }
      render(result.questions, email);
      setMessage(result.questions.some(item => item.pending) ? 'Some questions need your answers for next time.' : 'Your saved memory is ready.');
    } catch (err) {
      if (version === requestVersion) setMessage(err.message || 'Could not load saved answers.', true);
    }
  }
  emailField.addEventListener('input', () => {
    requestVersion++;
    if (loadedEmail && emailKey(emailField.value) !== loadedEmail) {
      for (const [name, value] of Object.entries(loadedProfileValues)) {
        if (form.elements[name].value === value) form.elements[name].value = '';
      }
      loadedProfileValues = {};
      profileLoadedEmail = '';
    }
    container.hidden = !!loadedEmail && emailKey(emailField.value) !== loadedEmail;
    updatePending();
  });
  emailField.addEventListener('change', () => { if (emailKey(emailField.value) !== loadedEmail) loadMemory({ automatic: true }); });
  document.getElementById('memory-load').addEventListener('click', () => loadMemory());
  document.getElementById('memory-save-profile').addEventListener('click', async () => {
    const email = currentEmail();
    if (!email) return;
    if (profileLoadedEmail !== emailKey(email)) {
      setMessage('Load saved answers first, then save your details.');
      await loadMemory();
      if (profileLoadedEmail === emailKey(email)) setMessage('Saved details are loaded. Select “Save my details” to store your changes.');
      return;
    }
    const values = { email };
    for (const name of profileFields) values[name] = form.elements[name].value.trim();
    requestVersion++;
    try {
      const result = await post('/api/memory/profile', values);
      if (emailKey(emailField.value) === emailKey(email)) {
        const changed = profileFields.some(name => form.elements[name].value.trim() !== values[name]);
        setMessage(changed ? 'Earlier details were saved. Save again to keep your latest edits.' : result.message);
      }
    } catch (err) { setMessage(err.message || 'Could not save your details.', true); }
  });
  document.getElementById('memory-clear').addEventListener('click', async () => {
    const email = currentEmail();
    if (!email || !window.confirm('Delete saved details and answers for this email? Application history will be kept.')) return;
    requestVersion++;
    try {
      const result = await post('/api/memory/clear', { email });
      if (emailKey(emailField.value) !== emailKey(email)) return;
      loadedEmail = emailKey(email);
      container.hidden = false;
      render([], email);
      setMessage(result.message);
    } catch (err) { setMessage(err.message || 'Could not clear saved memory.', true); }
  });
  document.getElementById('memory-add').addEventListener('click', async () => {
    const email = currentEmail();
    if (!email) return;
    const label = document.getElementById('memory-new-label');
    const kind = document.getElementById('memory-new-kind');
    const answer = document.getElementById('memory-new-answer');
    requestVersion++;
    try {
      const result = await post('/api/memory/add', { email, label: label.value.trim(), kind: kind.value, answer: answer.value.trim() });
      if (emailKey(emailField.value) !== emailKey(email)) return;
      label.value = '';
      answer.value = '';
      if (dirty.size) setMessage(`${result.message} Save other edits, then load memory to see it.`);
      else await loadMemory({ fillProfile: false, automatic: true });
    } catch (err) { setMessage(err.message || 'Could not save this answer.', true); }
  });
  document.addEventListener('job-search-finished', event => {
    if (emailKey(emailField.value) === emailKey(event.detail.email)) loadMemory({ fillProfile: false, automatic: true });
  });
})();
