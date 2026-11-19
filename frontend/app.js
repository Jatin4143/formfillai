/**
 * FieldFlow AI — Client Application Logic
 * Handles real-time chat, field tracking, review pop-up modal, and submissions.
 */

(function () {
  'use strict';

  // --- State ---
  let availableForms = [];
  let activeFormKey = null;
  let activeForm = null;
  let answers = {};
  let currentField = null;
  let isWaiting = false;

  // --- Multi-Action / Batch State ---
  let activeActions = [];
  let isBatchMode = false;

  // --- Chat History State ---
  let chats = [];
  let activeChatId = null;
  let editingChatId = null;

  // --- DOM Elements ---
  const sidebar = document.getElementById('sidebar');
  const btnToggleSidebar = document.getElementById('btn-toggle-sidebar');
  const btnNewChat = document.getElementById('btn-new-chat');
  const chatsList = document.getElementById('chats-list');
  const formsSelect = document.getElementById('forms-select');

  const activeFormCard = document.getElementById('active-form-card');
  const activeFormName = document.getElementById('active-form-name');
  const activeFormTag = document.getElementById('active-form-tag');
  const activeFormProgress = document.getElementById('active-form-progress');
  const activeFormCounts = document.getElementById('active-form-counts');
  const activeFormPercent = document.getElementById('active-form-percent');
  const activeFormChecklist = document.getElementById('active-form-checklist');
  const btnOpenReview = document.getElementById('btn-open-review');

  const currentFormIndicator = document.getElementById('current-form-indicator');
  const currentFieldIndicator = document.getElementById('current-field-indicator');
  const btnHeaderReview = document.getElementById('btn-header-review');

  const chatContainer = document.getElementById('chat-container');
  const chatMessages = document.getElementById('chat-messages');
  const welcomeCard = document.getElementById('welcome-card');
  const typingIndicator = document.getElementById('typing-indicator');
  const suggestionsBar = document.getElementById('suggestions-bar');

  const chatForm = document.getElementById('chat-form');
  const chatInput = document.getElementById('chat-input');
  const btnSend = document.getElementById('btn-send');

  // Review Modal
  const modalBackdrop = document.getElementById('modal-backdrop');
  const reviewModal = document.getElementById('review-modal');
  const modalTitle = document.getElementById('modal-title');
  const modalFormBadge = document.getElementById('modal-form-badge');
  const modalEditForm = document.getElementById('modal-edit-form');
  const btnCloseModal = document.getElementById('btn-close-modal');
  const btnModalCancel = document.getElementById('btn-modal-cancel');
  const btnModalSubmit = document.getElementById('btn-modal-submit');

  // Submissions Modal
  const submissionsBackdrop = document.getElementById('submissions-backdrop');
  const submissionsContainer = document.getElementById('submissions-container');
  const btnViewSubmissions = document.getElementById('btn-view-submissions');
  const btnCloseSubmissions = document.getElementById('btn-close-submissions');

  // Delete Conversation Modal
  const deleteModalBackdrop = document.getElementById('delete-modal-backdrop');
  const deleteChatName = document.getElementById('delete-chat-name');
  const btnCloseDeleteModal = document.getElementById('btn-close-delete-modal');
  const btnCancelDelete = document.getElementById('btn-cancel-delete');
  const btnConfirmDelete = document.getElementById('btn-confirm-delete');
  let pendingDeleteChatId = null;

  const toastContainer = document.getElementById('toast-container');

  // --- Initialization ---
  async function init() {
    setupEventListeners();
    await Promise.allSettled([fetchForms(), fetchChats()]);
  }

  function setupEventListeners() {
    // Sidebar toggle for mobile
    if (btnToggleSidebar) {
      btnToggleSidebar.addEventListener('click', () => {
        sidebar.classList.toggle('open');
      });
    }

    // New Chat button
    btnNewChat.addEventListener('click', () => createNewChat(true));

    // Chat form submit
    chatForm.addEventListener('submit', (e) => {
      e.preventDefault();
      handleSend();
    });

    // Enter key to send (Shift+Enter for newline)
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });

    // Auto-grow textarea
    chatInput.addEventListener('input', () => {
      chatInput.style.height = 'auto';
      chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
    });

    // Sample chip clicks in welcome card
    document.querySelectorAll('.chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        const text = chip.dataset.sample;
        if (text) {
          chatInput.value = text;
          chatInput.focus();
          handleSend();
        }
      });
    });

    // Review Modal Open Buttons
    btnOpenReview.addEventListener('click', () => {
      if (isBatchMode && activeActions.length) openBatchReviewModal(activeActions);
      else if (activeForm) openReviewModal(activeForm, answers);
    });
    btnHeaderReview.addEventListener('click', () => {
      if (isBatchMode && activeActions.length) openBatchReviewModal(activeActions);
      else if (activeForm) openReviewModal(activeForm, answers);
    });

    // Review Modal Close Buttons
    btnCloseModal.addEventListener('click', closeReviewModal);
    btnModalCancel.addEventListener('click', closeReviewModal);

    // Close modal on backdrop click
    modalBackdrop.addEventListener('click', (e) => {
      if (e.target === modalBackdrop) closeReviewModal();
    });

    // Submit Reviewed Form
    btnModalSubmit.addEventListener('click', submitReviewedForm);

    // Submissions drawer
    btnViewSubmissions.addEventListener('click', openSubmissionsModal);
    btnCloseSubmissions.addEventListener('click', closeSubmissionsModal);
    submissionsBackdrop.addEventListener('click', (e) => {
      if (e.target === submissionsBackdrop) closeSubmissionsModal();
    });

    // Delete Conversation Modal
    if (btnCloseDeleteModal) {
      btnCloseDeleteModal.addEventListener('click', closeDeleteModal);
    }
    if (btnCancelDelete) {
      btnCancelDelete.addEventListener('click', closeDeleteModal);
    }
    if (btnConfirmDelete) {
      btnConfirmDelete.addEventListener('click', confirmDeleteChat);
    }
    if (deleteModalBackdrop) {
      deleteModalBackdrop.addEventListener('click', (e) => {
        if (e.target === deleteModalBackdrop) closeDeleteModal();
      });
    }

    // Global keyboard listener for Escape key to close modals
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if (deleteModalBackdrop && deleteModalBackdrop.style.display !== 'none') {
          closeDeleteModal();
        } else if (modalBackdrop && modalBackdrop.style.display !== 'none') {
          closeReviewModal();
        } else if (submissionsBackdrop && submissionsBackdrop.style.display !== 'none') {
          closeSubmissionsModal();
        }
      }
    });

    // Forms dropdown change
    if (formsSelect) {
      formsSelect.addEventListener('change', (e) => {
        const selectedKey = e.target.value;
        const form = availableForms.find((f) => f.key === selectedKey);
        if (form) {
          selectFormDirectly(form);
        }
      });
    }
  }

  // --- API Functions ---
  async function fetchForms() {
    try {
      const res = await fetch('/api/forms');
      if (!res.ok) throw new Error('Failed to fetch forms');
      availableForms = await res.json();
      renderFormsCatalog();
    } catch (err) {
      console.error(err);
      showToast('Could not load forms catalog', 'error');
    }
  }

  function renderFormsCatalog() {
    if (!formsSelect) return;
    formsSelect.innerHTML = '<option value="" disabled selected>▾ Choose a form to fill...</option>';

    if (!availableForms.length) {
      formsSelect.innerHTML = '<option value="" disabled selected>No forms found</option>';
      return;
    }

    availableForms.forEach((form) => {
      const opt = document.createElement('option');
      opt.value = form.key;
      opt.textContent = form.form_name;
      if (activeFormKey === form.key) {
        opt.selected = true;
      }
      formsSelect.appendChild(opt);
    });
  }

  async function selectFormDirectly(form) {
    chatInput.value = `I want to fill ${form.form_name}`;
    handleSend();
  }

  // --- Chat & Messaging ---
  async function handleSend() {
    const text = chatInput.value.trim();
    if (!text || isWaiting) return;

    // Reset input
    chatInput.value = '';
    chatInput.style.height = 'auto';

    // Hide welcome card once conversation starts
    if (welcomeCard) welcomeCard.style.display = 'none';

    // Append User Message
    appendMessage('user', text);

    // Show typing indicator & set waiting state
    isWaiting = true;
    typingIndicator.style.display = 'flex';
    scrollToBottom();

    try {
      const payload = {
        message: text,
        form_key: activeFormKey,
        answers: answers,
        current_field: currentField,
        chat_id: activeChatId,
        actions: activeActions,
        is_batch: isBatchMode,
      };

      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        throw new Error(`Server returned ${res.status}`);
      }

      const data = await res.json();
      handleChatResponse(data);
    } catch (err) {
      console.error('Chat error:', err);
      appendMessage('assistant', `⚠️ Sorry, an error occurred: ${err.message}. Please check connection.`);
      showToast('Message sending failed', 'error');
    } finally {
      isWaiting = false;
      typingIndicator.style.display = 'none';
      scrollToBottom();
    }
  }

  function handleChatResponse(data) {
    if (data.chat_id) {
      activeChatId = data.chat_id;
    }
    if (data.chat_title) {
      const existing = chats.find((c) => c.id === activeChatId);
      if (existing) {
        existing.title = data.chat_title;
        existing.form_key = data.form_key || activeFormKey;
        existing.updated_at = new Date().toISOString();
      } else {
        chats.unshift({
          id: activeChatId,
          title: data.chat_title,
          updated_at: new Date().toISOString(),
          form_key: data.form_key || activeFormKey,
        });
      }
      renderChatsList();
    }

    if (data.actions && data.actions.length > 0) {
      activeActions = data.actions;
      isBatchMode = Boolean(data.is_batch) || activeActions.length > 1;
    } else {
      activeActions = [];
      isBatchMode = false;
    }

    if (data.status === 'cancelled') {
      activeFormKey = null;
      activeForm = null;
      answers = {};
      currentField = null;
      activeActions = [];
      isBatchMode = false;
      updateActiveFormUI();
      appendMessage('assistant', data.reply);
      updateSuggestions([]);
      return;
    }

    // Update active form & answers
    activeFormKey = data.form_key || null;
    activeForm = data.form || null;
    answers = data.answers || {};
    currentField = data.current_field || null;

    updateActiveFormUI();
    renderFormsCatalog();

    // Append assistant's reply
    if (data.reply) {
      appendMessage('assistant', data.reply);
    }

    // Dynamic suggestions based on field type
    updateFieldSuggestions(currentField);

    // ========================================================
    // POP-UP MODAL TRIGGER
    // When all fields are collected (data.ready_for_review === true),
    // automatically open the Review & Edit Modal!
    // ========================================================
    if (data.ready_for_review) {
      setTimeout(() => {
        if (isBatchMode && activeActions.length > 0) {
          openBatchReviewModal(activeActions);
        } else if (activeForm) {
          openReviewModal(activeForm, answers);
        }
      }, 400);
    }
  }

  function appendMessage(role, content, receiptData = null) {
    const row = document.createElement('div');
    row.className = `message-row ${role}`;

    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.textContent = role === 'user' ? 'You' : 'AI';

    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.innerHTML = formatMarkdown(content);

    if (receiptData) {
      const receiptCard = createReceiptCard(receiptData);
      bubble.appendChild(receiptCard);
    }

    row.appendChild(avatar);
    row.appendChild(bubble);
    chatMessages.appendChild(row);
    scrollToBottom();
  }

  function createReceiptCard(data) {
    const card = document.createElement('div');
    card.className = 'receipt-card';
    
    if (data.is_multi && data.records) {
      let itemsHtml = '';
      data.records.forEach((rec) => {
        let fieldsHtml = '';
        for (const [k, v] of Object.entries(rec.answers || {})) {
          if (v) {
            fieldsHtml += `
              <div class="receipt-field-item">
                <span>${escapeHtml(k.replace(/_/g, ' '))}</span>
                <strong>${escapeHtml(v)}</strong>
              </div>
            `;
          }
        }
        itemsHtml += `
          <div class="batch-receipt-item">
            <div class="batch-receipt-item-top">
              <span class="receipt-sub-name">${escapeHtml(rec.form_name)}</span>
              <span class="receipt-badge">#${rec.submission_id}</span>
            </div>
            <div class="receipt-fields">
              ${fieldsHtml}
            </div>
          </div>
        `;
      });

      card.innerHTML = `
        <div class="receipt-header">
          <strong style="color:#10b981;">✓ All ${data.records.length} Submissions Successful</strong>
        </div>
        <div class="batch-receipt-list">
          ${itemsHtml}
        </div>
      `;
      return card;
    }

    let fieldsHtml = '';
    for (const [k, v] of Object.entries(data.answers || {})) {
      fieldsHtml += `
        <div class="receipt-field-item">
          <span>${escapeHtml(k.replace(/_/g, ' '))}</span>
          <strong>${escapeHtml(v || 'None')}</strong>
        </div>
      `;
    }

    card.innerHTML = `
      <div class="receipt-header">
        <strong style="color:#10b981;">✓ Submission Successful</strong>
        <span class="receipt-badge">#${data.submission_id}</span>
      </div>
      <div class="receipt-fields">
        ${fieldsHtml}
      </div>
    `;
    return card;
  }

  function updateActiveFormUI() {
    if (!activeForm) {
      activeFormCard.style.display = 'none';
      btnHeaderReview.style.display = 'none';
      currentFormIndicator.textContent = 'FieldFlow AI';
      currentFieldIndicator.textContent = 'Ready to assist';
      chatInput.placeholder = 'Say hello, choose a form, or paste details...';
      return;
    }

    activeFormCard.style.display = 'flex';
    btnHeaderReview.style.display = 'inline-flex';
    activeFormName.textContent = activeForm.form_name;

    const fields = activeForm.schema ? activeForm.schema.fields : [];
    const total = fields.length;
    const answeredCount = Object.keys(answers).length;
    const percent = total > 0 ? Math.round((answeredCount / total) * 100) : 0;

    activeFormProgress.style.width = `${percent}%`;
    activeFormCounts.textContent = `${answeredCount} / ${total} fields`;
    activeFormPercent.textContent = `${percent}%`;

    const isComplete = answeredCount === total && total > 0;
    activeFormTag.textContent = isComplete ? 'Ready to Submit' : 'In Progress';
    activeFormTag.className = `status-tag ${isComplete ? 'complete' : ''}`;

    if (isComplete) {
      btnOpenReview.classList.add('highlight');
      btnHeaderReview.classList.add('btn-primary');
      btnHeaderReview.classList.remove('btn-outline');
    } else {
      btnOpenReview.classList.remove('highlight');
      btnHeaderReview.classList.remove('btn-primary');
      btnHeaderReview.classList.add('btn-outline');
    }

    // Render Checklist
    activeFormChecklist.innerHTML = '';
    fields.forEach((f) => {
      const isDone = f.name in answers;
      const li = document.createElement('li');
      li.className = `field-checklist-item ${isDone ? 'done' : ''}`;

      const icon = document.createElement('span');
      icon.className = `check-icon ${isDone ? 'done' : 'pending'}`;
      icon.innerHTML = isDone ? '✓' : '';

      const label = document.createElement('span');
      const val = answers[f.name];
      label.textContent = isDone ? `${f.label}: ${val || '(blank)'}` : f.label;

      li.appendChild(icon);
      li.appendChild(label);
      activeFormChecklist.appendChild(li);
    });

    // Update Top bar indicators
    currentFormIndicator.textContent = activeForm.form_name;
    if (currentField) {
      currentFieldIndicator.textContent = `Asking for ${currentField.label}`;
      chatInput.placeholder = currentField.options
        ? `Choose: ${currentField.options.join(', ')}...`
        : `Enter ${currentField.label.toLowerCase()}...`;
    } else {
      currentFieldIndicator.textContent = 'All fields captured';
      chatInput.placeholder = 'Review pop-up is ready, or type a new message...';
    }
  }

  function updateFieldSuggestions(field) {
    if (!field) {
      updateSuggestions([]);
      return;
    }

    const suggestions = [];
    if (field.options && field.options.length) {
      field.options.forEach((opt) => suggestions.push(opt));
    }

    if (!field.required) {
      suggestions.push('skip');
    }

    updateSuggestions(suggestions);
  }

  function updateSuggestions(items) {
    suggestionsBar.innerHTML = '';
    if (!items || !items.length) {
      suggestionsBar.style.display = 'none';
      return;
    }

    suggestionsBar.style.display = 'flex';
    items.forEach((item) => {
      const pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'suggestion-pill';
      pill.textContent = item;
      pill.addEventListener('click', () => {
        chatInput.value = item;
        handleSend();
      });
      suggestionsBar.appendChild(pill);
    });
  }

  // ========================================================
  // REVIEW & EDIT MODAL (Pop-up Dialog Logic)
  // ========================================================
  function openReviewModal(form, currentAnswers) {
    if (reviewModal) reviewModal.classList.remove('modal-dialog-batch');
    modalTitle.textContent = `Review & Confirm Form`;
    modalFormBadge.textContent = form.form_name;
    modalEditForm.innerHTML = '';
    const submitSpan = btnModalSubmit.querySelector('span');
    if (submitSpan) submitSpan.textContent = 'Confirm & Submit';

    const fields = form.schema ? form.schema.fields : [];
    fields.forEach((field) => {
      const card = document.createElement('div');
      card.className = 'modal-field-card';

      const labelRow = document.createElement('div');
      labelRow.className = 'modal-field-label-row';

      const label = document.createElement('label');
      label.className = 'modal-field-label';
      label.textContent = field.label;

      const badge = document.createElement('span');
      badge.className = `modal-field-badge ${field.required ? 'required' : 'optional'}`;
      badge.textContent = field.required ? 'Required' : 'Optional';

      labelRow.appendChild(label);
      labelRow.appendChild(badge);
      card.appendChild(labelRow);

      const val = currentAnswers[field.name] != null ? currentAnswers[field.name] : '';

      // Input matching field type
      if (field.type === 'select' && field.options && field.options.length) {
        const select = document.createElement('select');
        select.className = 'modal-input';
        select.name = field.name;

        // Default empty option if not required
        if (!field.required) {
          const opt = document.createElement('option');
          opt.value = '';
          opt.textContent = '-- None (Skip) --';
          select.appendChild(opt);
        }

        field.options.forEach((optText) => {
          const opt = document.createElement('option');
          opt.value = optText;
          opt.textContent = optText;
          if (val === optText) opt.selected = true;
          select.appendChild(opt);
        });
        card.appendChild(select);
      } else if (field.type === 'textarea') {
        const textarea = document.createElement('textarea');
        textarea.className = 'modal-input';
        textarea.name = field.name;
        textarea.rows = 3;
        textarea.value = val;
        card.appendChild(textarea);
      } else {
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'modal-input';
        input.name = field.name;
        input.value = val;
        if (field.name === 'vehicle_number') {
          input.placeholder = 'e.g. MH12AB1234';
        }
        card.appendChild(input);
      }

      modalEditForm.appendChild(card);
    });

    modalBackdrop.style.display = 'flex';
    // Focus first input
    const firstInput = modalEditForm.querySelector('input, select, textarea');
    if (firstInput) setTimeout(() => firstInput.focus(), 150);
  }

  function openBatchReviewModal(actions) {
    if (reviewModal) reviewModal.classList.add('modal-dialog-batch');
    modalTitle.textContent = `Review & Confirm (${actions.length} Items)`;
    modalFormBadge.textContent = 'Multi-Record Batch';
    modalEditForm.innerHTML = '';
    const submitSpan = btnModalSubmit.querySelector('span');
    if (submitSpan) submitSpan.textContent = `Confirm & Submit All (${actions.length} Records)`;

    actions.forEach((act, idx) => {
      const card = document.createElement('div');
      card.className = 'modal-batch-card';
      card.dataset.actionIndex = idx;
      card.dataset.formKey = act.form_key;

      const header = document.createElement('div');
      header.className = 'batch-card-header';
      header.innerHTML = `
        <span class="batch-card-title">${escapeHtml(act.title || act.form_name)}</span>
        <span class="batch-card-tag">${escapeHtml(act.form_name)}</span>
      `;
      card.appendChild(header);

      const fields = act.schema ? act.schema.fields : [];
      fields.forEach((field) => {
        const fieldCard = document.createElement('div');
        fieldCard.className = 'modal-field-card';

        const labelRow = document.createElement('div');
        labelRow.className = 'modal-field-label-row';

        const label = document.createElement('label');
        label.className = 'modal-field-label';
        label.textContent = field.label;

        const badge = document.createElement('span');
        badge.className = `modal-field-badge ${field.required ? 'required' : 'optional'}`;
        badge.textContent = field.required ? 'Required' : 'Optional';

        labelRow.appendChild(label);
        labelRow.appendChild(badge);
        fieldCard.appendChild(labelRow);

        const val = act.fields && act.fields[field.name] != null ? act.fields[field.name] : '';

        if (field.type === 'select' && field.options && field.options.length) {
          const select = document.createElement('select');
          select.className = 'modal-input';
          select.dataset.fieldName = field.name;

          if (!field.required) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = '-- None (Skip) --';
            select.appendChild(opt);
          }

          field.options.forEach((optText) => {
            const opt = document.createElement('option');
            opt.value = optText;
            opt.textContent = optText;
            if (val === optText) opt.selected = true;
            select.appendChild(opt);
          });
          fieldCard.appendChild(select);
        } else if (field.type === 'textarea') {
          const textarea = document.createElement('textarea');
          textarea.className = 'modal-input';
          textarea.dataset.fieldName = field.name;
          textarea.rows = 2;
          textarea.value = val;
          fieldCard.appendChild(textarea);
        } else {
          const input = document.createElement('input');
          input.type = 'text';
          input.className = 'modal-input';
          input.dataset.fieldName = field.name;
          input.value = val;
          fieldCard.appendChild(input);
        }

        card.appendChild(fieldCard);
      });

      modalEditForm.appendChild(card);
    });

    modalBackdrop.style.display = 'flex';
    const firstInput = modalEditForm.querySelector('input, select, textarea');
    if (firstInput) setTimeout(() => firstInput.focus(), 150);
  }

  function closeReviewModal() {
    modalBackdrop.style.display = 'none';
    if (reviewModal) reviewModal.classList.remove('modal-dialog-batch');
  }

  const INDIAN_PLATE_RE = /^(?:[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{1,3}[ -]?\d{1,4}|\d{2}[ -]?BH[ -]?\d{4}[ -]?[A-HJ-NP-Z]{1,2})$/i;

  async function submitReviewedForm() {
    if (isBatchMode && activeActions.length > 0) {
      const items = [];
      let hasValidationError = false;

      activeActions.forEach((act, idx) => {
        if (hasValidationError) return;
        const card = modalEditForm.querySelector(`[data-action-index="${idx}"]`);
        if (!card) return;

        const cardAnswers = {};
        const fields = act.schema ? act.schema.fields : [];

        for (const f of fields) {
          const input = card.querySelector(`[data-field-name="${f.name}"]`);
          let val = input ? input.value.trim() : '';
          if (f.required && !val) {
            showToast(`Please provide "${f.label}" for ${act.title || act.form_name}`, 'error');
            if (input) input.focus();
            hasValidationError = true;
            return;
          }
          if (val && (f.name.toLowerCase().includes('vehicle') || f.name.toLowerCase().includes('plate'))) {
            if (!INDIAN_PLATE_RE.test(val)) {
              showToast(`Please enter a valid Indian vehicle number plate for "${f.label}" (e.g. MH 12 AB 1234)`, 'error');
              if (input) input.focus();
              hasValidationError = true;
              return;
            }
            val = val.replace(/\s+/g, ' ').toUpperCase();
          }
          cardAnswers[f.name] = val || null;
        }

        items.push({
          form_key: act.form_key,
          answers: cardAnswers
        });
      });

      if (hasValidationError) return;

      btnModalSubmit.disabled = true;
      btnModalSubmit.innerHTML = `
        <svg class="pulse-dot" width="16" height="16" style="margin-right:6px;"></svg>
        Submitting All...
      `;

      try {
        const res = await fetch('/api/forms/submit-batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items, chat_id: activeChatId }),
        });

        if (!res.ok) throw new Error('Batch submission failed');
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'Submission failed');

        closeReviewModal();
        showToast(`All ${data.count} records submitted successfully!`, 'success');

        appendMessage('assistant', `All set! **${data.count} submissions** recorded successfully:`, {
          is_multi: true,
          records: data.records
        });

        // Reset state
        activeActions = [];
        isBatchMode = false;
        activeFormKey = null;
        activeForm = null;
        answers = {};
        currentField = null;
        updateActiveFormUI();
        renderFormsCatalog();
        updateSuggestions([]);
      } catch (err) {
        console.error('Batch submit error:', err);
        showToast(`Error: ${err.message}`, 'error');
      } finally {
        btnModalSubmit.disabled = false;
        btnModalSubmit.innerHTML = `
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
            <polyline points="20 6 9 17 4 12"></polyline>
          </svg>
          <span>Confirm & Submit</span>
        `;
      }
      return;
    }

    if (!activeFormKey || !activeForm) return;

    // Gather values from modal form
    const formData = new FormData(modalEditForm);
    const updatedAnswers = {};

    const fields = activeForm.schema ? activeForm.schema.fields : [];
    for (const f of fields) {
      let val = formData.get(f.name);
      if (typeof val === 'string') val = val.trim();
      updatedAnswers[f.name] = val || null;
    }

    // Check required fields and Indian vehicle plate format
    for (const f of fields) {
      if (f.required && !updatedAnswers[f.name]) {
        showToast(`Please provide a value for "${f.label}"`, 'error');
        const input = modalEditForm.querySelector(`[name="${f.name}"]`);
        if (input) input.focus();
        return;
      }
      if (updatedAnswers[f.name] && (f.name.toLowerCase().includes('vehicle') || f.name.toLowerCase().includes('plate'))) {
        if (!INDIAN_PLATE_RE.test(updatedAnswers[f.name])) {
          showToast(`Please enter a valid Indian vehicle number plate for "${f.label}" (e.g. MH 12 AB 1234)`, 'error');
          const input = modalEditForm.querySelector(`[name="${f.name}"]`);
          if (input) input.focus();
          return;
        }
        updatedAnswers[f.name] = updatedAnswers[f.name].replace(/\s+/g, ' ').toUpperCase();
      }
    }

    btnModalSubmit.disabled = true;
    btnModalSubmit.innerHTML = `
      <svg class="pulse-dot" width="16" height="16" style="margin-right:6px;"></svg>
      Submitting...
    `;

    try {
      const res = await fetch(`/api/forms/${activeFormKey}/submit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers: updatedAnswers, chat_id: activeChatId }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Submission failed');

      closeReviewModal();
      showToast(`Form submitted successfully as #${data.submission_id}!`, 'success');

      // Append completion receipt card to chat
      appendMessage('assistant', `All done! Your **${activeForm.form_name}** has been recorded.`, {
        submission_id: data.submission_id,
        answers: updatedAnswers,
      });

      // Clear active form state so user can immediately start another form
      activeFormKey = null;
      activeForm = null;
      answers = {};
      currentField = null;
      updateActiveFormUI();
      renderFormsCatalog();
      updateSuggestions([]);
    } catch (err) {
      console.error('Submit error:', err);
      showToast(`Error submitting form: ${err.message}`, 'error');
    } finally {
      btnModalSubmit.disabled = false;
      btnModalSubmit.innerHTML = `
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
          <polyline points="20 6 9 17 4 12"></polyline>
        </svg>
        <span>Confirm & Submit</span>
      `;
    }
  }

  // ========================================================
  // SUBMISSIONS DRAWER
  // ========================================================
  async function openSubmissionsModal() {
    submissionsBackdrop.style.display = 'flex';
    submissionsContainer.innerHTML = '<div style="color:#64748b; font-size:0.85rem;">Loading submissions...</div>';

    try {
      const res = await fetch('/api/submissions');
      if (!res.ok) throw new Error('Failed to fetch records');
      const data = await res.json();

      if (!data.length) {
        submissionsContainer.innerHTML = '<div style="color:#94a3b8; font-size:0.9rem; text-align:center; padding:2rem;">No submissions yet. Complete a form to see it here!</div>';
        return;
      }

      submissionsContainer.innerHTML = '';
      data.forEach((sub) => {
        const item = document.createElement('div');
        item.className = 'submission-item';

        let pairsHtml = '';
        for (const [k, v] of Object.entries(sub.answers || {})) {
          if (k === 'form') continue;
          pairsHtml += `
            <div class="submission-pair">
              <span>${escapeHtml(k.replace(/_/g, ' '))}</span>
              <strong>${escapeHtml(v || 'None')}</strong>
            </div>
          `;
        }

        item.innerHTML = `
          <div class="submission-top">
            <span class="sub-id-pill">#${sub.id} &bull; ${escapeHtml(sub.form_name)}</span>
            <span class="sub-date">${sub.created_at || ''}</span>
          </div>
          <div class="submission-details">
            ${pairsHtml}
          </div>
        `;
        submissionsContainer.appendChild(item);
      });
    } catch (err) {
      submissionsContainer.innerHTML = `<div style="color:#f43f5e;">Error loading records: ${err.message}</div>`;
    }
  }

  function closeSubmissionsModal() {
    submissionsBackdrop.style.display = 'none';
  }

  // ========================================================
  // CHAT HISTORY & CONVERSATION MANAGEMENT
  // ========================================================
  async function fetchChats() {
    try {
      const res = await fetch('/api/chats');
      if (!res.ok) throw new Error('Failed to load chat history');
      chats = await res.json();
      renderChatsList();
      if (chats.length > 0 && !activeChatId) {
        await openChat(chats[0].id);
      } else if (!activeChatId) {
        await createNewChat(false);
      }
    } catch (err) {
      console.error('Fetch chats error:', err);
      if (chatsList) {
        chatsList.innerHTML = '<div class="empty-chats-hint">No past conversations</div>';
      }
    }
  }

  function parseChatDate(value) {
    if (!value) return null;
    let str = String(value).trim();
    // Normalize SQLite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC)
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(str)) {
      str = str.replace(' ', 'T') + 'Z';
    }
    const d = new Date(str);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function getChatDateGroup(updatedAt) {
    const d = parseChatDate(updatedAt);
    if (!d) return 'Older';

    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterdayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    const chatDayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());

    if (chatDayStart >= todayStart) return 'Today';
    if (chatDayStart >= yesterdayStart) return 'Yesterday';
    return 'Older';
  }

  function renderChatsList() {
    if (!chatsList) return;
    chatsList.innerHTML = '';

    if (!chats || chats.length === 0) {
      chatsList.innerHTML = '<div class="empty-chats-hint">No conversations yet</div>';
      return;
    }

    const sortedChats = [...chats].sort((a, b) => {
      const da = parseChatDate(a.updated_at);
      const db = parseChatDate(b.updated_at);
      return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
    });

    const groups = {
      Today: [],
      Yesterday: [],
      Older: [],
    };

    sortedChats.forEach((chat) => {
      const groupName = getChatDateGroup(chat.updated_at);
      groups[groupName].push(chat);
    });

    ['Today', 'Yesterday', 'Older'].forEach((groupName) => {
      const groupChats = groups[groupName];
      if (!groupChats.length) return;

      const groupEl = document.createElement('div');
      groupEl.className = 'chat-history-group';

      const labelEl = document.createElement('div');
      labelEl.className = 'chat-group-label';
      labelEl.textContent = groupName;
      groupEl.appendChild(labelEl);

      groupChats.forEach((chat) => {
        const item = createChatListItem(chat);
        groupEl.appendChild(item);
      });

      chatsList.appendChild(groupEl);
    });
  }

  function createChatListItem(chat) {
      const item = document.createElement('div');
      item.className = `chat-item ${chat.id === activeChatId ? 'active' : ''}`;
      item.dataset.id = chat.id;

      if (editingChatId === chat.id) {
        item.innerHTML = `
          <div class="chat-edit-wrapper">
            <input type="text" class="chat-rename-input" value="${escapeHtml(chat.title || 'Conversation')}" maxlength="60" />
            <button class="chat-btn-save" title="Save">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="20 6 9 17 4 12"></polyline>
              </svg>
            </button>
            <button class="chat-btn-cancel" title="Cancel">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18"></line>
                <line x1="6" y1="6" x2="18" y2="18"></line>
              </svg>
            </button>
          </div>
        `;

        const input = item.querySelector('.chat-rename-input');
        const btnSave = item.querySelector('.chat-btn-save');
        const btnCancel = item.querySelector('.chat-btn-cancel');

        setTimeout(() => {
          if (input) {
            input.focus();
            input.select();
          }
        }, 10);

        item.addEventListener('click', (e) => e.stopPropagation());

        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            renameChat(chat.id, input.value);
          } else if (e.key === 'Escape') {
            e.preventDefault();
            editingChatId = null;
            renderChatsList();
          }
        });

        btnSave.addEventListener('click', (e) => {
          e.stopPropagation();
          renameChat(chat.id, input.value);
        });

        btnCancel.addEventListener('click', (e) => {
          e.stopPropagation();
          editingChatId = null;
          renderChatsList();
        });
      } else {
        item.innerHTML = `
          <div class="chat-item-main">
            <svg class="chat-item-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
            </svg>
            <span class="chat-item-title" title="${escapeHtml(chat.title || 'Conversation')}">${escapeHtml(chat.title || 'Conversation')}</span>
          </div>
          <div class="chat-item-actions">
            <button class="chat-btn-action chat-btn-edit" title="Rename conversation" data-id="${chat.id}">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 20h9"></path>
                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>
              </svg>
            </button>
            <button class="chat-btn-action chat-btn-delete" title="Delete conversation" data-id="${chat.id}">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="3 6 5 6 21 6"></polyline>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
              </svg>
            </button>
          </div>
        `;

        item.addEventListener('click', () => {
          openChat(chat.id);
        });

        const btnEdit = item.querySelector('.chat-btn-edit');
        const btnDelete = item.querySelector('.chat-btn-delete');

        btnEdit.addEventListener('click', (e) => {
          e.stopPropagation();
          editingChatId = chat.id;
          renderChatsList();
        });

        btnDelete.addEventListener('click', (e) => {
          e.stopPropagation();
          openDeleteModal(chat.id);
        });
      }

      return item;
  }

  function createNewChat(notify = true) {
    activeChatId = null;
    editingChatId = null;

    // Reset in-memory state
    activeFormKey = null;
    activeForm = null;
    answers = {};
    currentField = null;
    activeActions = [];
    isBatchMode = false;

    updateActiveFormUI();
    renderFormsCatalog();
    updateSuggestions([]);

    chatMessages.innerHTML = '';
    if (welcomeCard) {
      chatMessages.appendChild(welcomeCard);
      welcomeCard.style.display = 'flex';
    }

    renderChatsList();
    if (notify) showToast('New conversation ready', 'info');
    chatInput.value = '';
    chatInput.focus();
  }

  async function openChat(chatId) {
    if (activeChatId === chatId && chatMessages.children.length > 0) return;
    activeChatId = chatId;
    editingChatId = null;
    renderChatsList();

    try {
      const res = await fetch(`/api/chats/${chatId}`);
      if (!res.ok) throw new Error('Chat session not found');
      const chat = await res.json();

      activeFormKey = chat.form_key || null;
      activeForm = chat.form || null;
      answers = chat.answers || {};
      currentField = chat.current_field || null;
      activeActions = [];
      isBatchMode = false;

      chatMessages.innerHTML = '';
      const msgs = chat.messages || [];

      if (msgs.length === 0) {
        if (welcomeCard) {
          chatMessages.appendChild(welcomeCard);
          welcomeCard.style.display = 'flex';
        }
      } else {
        if (welcomeCard) welcomeCard.style.display = 'none';
        msgs.forEach((m) => {
          appendMessage(m.role, m.content, m.receipt_data || null);
        });
      }

      updateActiveFormUI();
      renderFormsCatalog();
      updateFieldSuggestions(currentField);
      scrollToBottom();
    } catch (err) {
      console.error('Open chat error:', err);
      showToast('Failed to load conversation', 'error');
    }
  }

  async function renameChat(chatId, newTitle) {
    const trimmed = (newTitle || '').trim();
    if (!trimmed) {
      showToast('Title cannot be empty', 'error');
      return;
    }

    try {
      const res = await fetch(`/api/chats/${chatId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: trimmed }),
      });
      if (!res.ok) throw new Error('Failed to rename conversation');
      const data = await res.json();

      const existing = chats.find((c) => c.id === chatId);
      if (existing) {
        existing.title = data.title;
        existing.title_locked = true;
      }
      editingChatId = null;
      renderChatsList();
      showToast('Conversation renamed', 'success');
    } catch (err) {
      console.error('Rename error:', err);
      showToast('Could not rename conversation', 'error');
    }
  }

  function openDeleteModal(chatId) {
    const chat = chats.find((c) => c.id === chatId);
    const title = chat && chat.title ? chat.title.trim() : 'this conversation';
    pendingDeleteChatId = chatId;

    if (deleteChatName) {
      deleteChatName.textContent = title;
      deleteChatName.title = title;
    }

    if (btnConfirmDelete) {
      btnConfirmDelete.disabled = false;
      const span = btnConfirmDelete.querySelector('span');
      if (span) span.textContent = 'Delete Conversation';
    }

    if (deleteModalBackdrop) {
      deleteModalBackdrop.style.display = 'flex';
    }

    if (btnCancelDelete) {
      setTimeout(() => btnCancelDelete.focus(), 60);
    }
  }

  function closeDeleteModal() {
    if (deleteModalBackdrop) {
      deleteModalBackdrop.style.display = 'none';
    }
    pendingDeleteChatId = null;
  }

  async function confirmDeleteChat() {
    if (!pendingDeleteChatId) return;
    const chatId = pendingDeleteChatId;

    if (btnConfirmDelete) {
      btnConfirmDelete.disabled = true;
      const span = btnConfirmDelete.querySelector('span');
      if (span) span.textContent = 'Deleting...';
    }

    try {
      const res = await fetch(`/api/chats/${chatId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('Failed to delete conversation');

      chats = chats.filter((c) => c.id !== chatId);
      closeDeleteModal();

      if (activeChatId === chatId) {
        if (chats.length > 0) {
          await openChat(chats[0].id);
        } else {
          await createNewChat(false);
        }
      } else {
        renderChatsList();
      }
      showToast('Conversation deleted', 'success');
    } catch (err) {
      console.error('Delete chat error:', err);
      showToast('Could not delete conversation', 'error');
      if (btnConfirmDelete) {
        btnConfirmDelete.disabled = false;
        const span = btnConfirmDelete.querySelector('span');
        if (span) span.textContent = 'Delete Conversation';
      }
    }
  }

  function deleteChat(chatId) {
    openDeleteModal(chatId);
  }

  function resetChat() {
    createNewChat(true);
  }

  // --- Helpers ---
  function scrollToBottom() {
    chatContainer.scrollTop = chatContainer.scrollHeight;
  }

  function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    toastContainer.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(8px)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 3200);
  }

  function formatMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);

    // Code blocks
    html = html.replace(/```([a-z]*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    // Bullet points
    html = html.replace(/^- (.*)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
    // Line breaks
    html = html.replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>');

    return `<p>${html}</p>`;
  }

  function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Run on page load
  window.addEventListener('DOMContentLoaded', init);
})();
