const form = document.querySelector('#batch-form');
const taskList = document.querySelector('#task-list');
const taskCount = document.querySelector('#task-count');
const progressCount = document.querySelector('#progress-count');
const successCount = document.querySelector('#success-count');
const failureCount = document.querySelector('#failure-count');
const clearCompletedButton = document.querySelector('#clear-completed-button');
const startButton = form?.querySelector('button[type="submit"]');
const notice = document.querySelector('#notice');
const tokenInput = document.querySelector('#access-tokens');
const billingProxyInput = document.querySelector('#billing-exit-proxies');
const promotionProxyInput = document.querySelector('#promotion-exit-proxies');
const concurrencyInput = document.querySelector('#concurrency');
const retryCountInput = document.querySelector('#retry-count');
const batchTokenStorageKey = 'gcash-link-extractor.batch-tokens.v1';
const persistedFields = [
  [tokenInput, 'gcash-link-extractor.access-tokens.v1'],
  [billingProxyInput, 'gcash-link-extractor.billing-exit-proxies.v1'],
  [promotionProxyInput, 'gcash-link-extractor.promotion-exit-proxies.v1'],
  [concurrencyInput, 'gcash-link-extractor.concurrency.v1'],
  [retryCountInput, 'gcash-link-extractor.retry-count.v1'],
];
const terminalStatuses = new Set(['success', 'failed', 'invalid', 'missing_email', 'expired', 'nonzero']);

function persistField(input, storageKey) {
  if (!input) return;
  try {
    if (input.value.trim()) {
      localStorage.setItem(storageKey, input.value);
    } else {
      localStorage.removeItem(storageKey);
    }
  } catch (_) {}
}

function restoreField(input, storageKey) {
  if (!input) return;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) input.value = saved;
  } catch (_) {}
}

function showNotice(message) {
  if (!notice) return;
  notice.textContent = message;
  notice.hidden = !message;
}

function escapeText(value) {
  return String(value ?? '');
}

function safeHttpsUrl(value) {
  try {
    const url = new URL(String(value || ''));
    return url.protocol === 'https:' ? url.href : '';
  } catch (_) {
    return '';
  }
}

async function copyText(value, message = 'GCash 链接已复制') {
  try {
    await navigator.clipboard.writeText(value);
  } catch (_) {
    const temporary = document.createElement('textarea');
    temporary.value = value;
    temporary.style.position = 'fixed';
    temporary.style.opacity = '0';
    document.body.appendChild(temporary);
    temporary.select();
    document.execCommand('copy');
    temporary.remove();
  }
  showNotice(message);
}

function uniqueTokenLines(value) {
  const seen = new Set();
  return String(value || '').split(/\r?\n/).map((item) => item.trim()).filter((item) => {
    if (!item || item.startsWith('#') || seen.has(item)) return false;
    seen.add(item);
    return true;
  });
}

function readBatchTokens() {
  try {
    const value = JSON.parse(localStorage.getItem(batchTokenStorageKey) || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch (_) {
    return {};
  }
}

function rememberBatchTokens(batchId, tokens) {
  if (!batchId || !tokens.length) return;
  try {
    const batches = readBatchTokens();
    batches[String(batchId)] = tokens;
    const keys = Object.keys(batches);
    for (const key of keys.slice(0, Math.max(0, keys.length - 20))) delete batches[key];
    localStorage.setItem(batchTokenStorageKey, JSON.stringify(batches));
  } catch (_) {}
}

function taskAccessToken(task) {
  const position = Number(task?.position) - 1;
  if (!Number.isInteger(position) || position < 0) return '';
  const batches = readBatchTokens();
  const stored = batches[String(task?.batch_id || '')];
  if (Array.isArray(stored) && stored[position]) return String(stored[position]);
  const current = uniqueTokenLines(tokenInput?.value);
  return current[position] || '';
}

function isSuccessfulTask(task) {
  const amount = Number(task?.amount);
  return escapeText(task?.status) === 'success'
    && Number.isFinite(amount)
    && amount === 0
    && Boolean(task?.has_qr);
}

function isFinishedTask(task) {
  return terminalStatuses.has(escapeText(task?.status));
}

function openQrPreview(source, email) {
  const dialog = document.createElement('dialog');
  dialog.className = 'qr-dialog';
  const close = document.createElement('button');
  close.className = 'qr-dialog-close';
  close.type = 'button';
  close.textContent = '×';
  close.setAttribute('aria-label', '关闭二维码');
  const image = document.createElement('img');
  image.src = source;
  image.alt = `${email || 'GCash'} 支付二维码`;
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => dialog.remove());
  dialog.append(close, image);
  document.body.appendChild(dialog);
  dialog.showModal();
}

function updateTaskArtifacts(container, task) {
  const paymentUrl = safeHttpsUrl(task.payment_url);
  const paymentStatus = escapeText(task.payment_status);
  const renderKey = `${Boolean(task.has_qr)}\n${paymentUrl}\n${paymentStatus}`;
  if (container.dataset.renderKey === renderKey) return;
  container.replaceChildren();

  if (task.has_qr) {
    const source = `/api/tasks/${encodeURIComponent(escapeText(task.task_id))}/qr`;
    const qrButton = document.createElement('button');
    qrButton.className = 'qr-preview';
    qrButton.type = 'button';
    qrButton.setAttribute('aria-label', '查看 GCash 二维码');
    const image = document.createElement('img');
    image.src = source;
    image.alt = 'GCash 二维码';
    qrButton.append(image);
    qrButton.addEventListener('click', () => openQrPreview(source, task.email));
    container.append(qrButton);
  }

  if (paymentUrl || paymentStatus) {
    const result = document.createElement('div');
    result.className = 'task-result-actions';
    if (paymentStatus) {
      const state = document.createElement('span');
      state.className = `payment-state ${paymentStatus}`;
      state.textContent = paymentStatus === 'paid' ? '已支付' : '等待授权';
      result.append(state);
    }
    if (paymentUrl) {
      const open = document.createElement('a');
      open.className = 'task-result-link';
      open.href = paymentUrl;
      open.target = '_blank';
      open.rel = 'noopener noreferrer';
      open.textContent = '打开链接';
      const copy = document.createElement('button');
      copy.className = 'task-result-copy';
      copy.type = 'button';
      copy.textContent = '复制链接';
      copy.addEventListener('click', () => copyText(paymentUrl));
      result.append(open, copy);
    }
    container.append(result);
  }
  container.hidden = !container.children.length;
  container.dataset.renderKey = renderKey;
}

function createTaskRow(task, index) {
  const row = document.createElement('article');
  row.className = 'task-row';
  row.dataset.taskId = escapeText(task.task_id);
  row.style.animationDelay = `${Math.min(index, 12) * 35}ms`;

  const main = document.createElement('div');
  main.className = 'task-main';
  const email = document.createElement('div');
  email.className = 'task-email';
  const copyToken = document.createElement('button');
  copyToken.className = 'task-copy-token';
  copyToken.type = 'button';
  copyToken.textContent = '复制 AT';
  copyToken.addEventListener('click', () => {
    const token = taskAccessToken(row._task);
    if (token) copyText(token, 'accessToken 已复制');
  });
  const artifacts = document.createElement('div');
  artifacts.className = 'task-artifacts';
  artifacts.hidden = true;
  main.append(email, copyToken, artifacts);

  const stage = document.createElement('div');
  stage.className = 'task-stage';

  const progress = document.createElement('div');
  progress.className = 'task-progress';
  const progressLabel = document.createElement('small');
  const track = document.createElement('div');
  track.className = 'progress-track';
  const value = document.createElement('div');
  value.className = 'progress-value';
  track.appendChild(value);
  progress.append(progressLabel, track);

  const status = document.createElement('div');
  status.className = 'task-status';
  const deleteButton = document.createElement('button');
  deleteButton.className = 'task-delete';
  deleteButton.type = 'button';
  deleteButton.textContent = '×';
  deleteButton.addEventListener('click', () => deleteTask(row));
  row.append(main, stage, progress, status, deleteButton);
  updateTaskRow(row, task);
  return row;
}

function updateTaskRow(row, task) {
  const email = row.querySelector('.task-email');
  const copyToken = row.querySelector('.task-copy-token');
  const artifacts = row.querySelector('.task-artifacts');
  const stage = row.querySelector('.task-stage');
  const progressLabel = row.querySelector('.task-progress small');
  const progressValue = row.querySelector('.progress-value');
  const status = row.querySelector('.task-status');
  const deleteButton = row.querySelector('.task-delete');
  const emailText = task.email || '未识别邮箱';
  const stageText = escapeText(task.stage);
  const finishedAt = escapeText(task.finished_at);
  const stageKey = `${stageText}\n${finishedAt}`;
  const progressNumber = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const statusText = escapeText(task.status_label);
  const statusClass = `task-status ${escapeText(task.status)}`;
  const isTerminal = terminalStatuses.has(escapeText(task.status));

  row._task = task;
  row.dataset.taskStatus = escapeText(task.status);
  if (email.textContent !== emailText) email.textContent = emailText;
  if (copyToken) {
    const available = Boolean(taskAccessToken(task));
    copyToken.disabled = !available;
    copyToken.setAttribute('aria-label', available ? '复制当前任务 accessToken' : '当前任务没有可用 accessToken');
  }
  updateTaskArtifacts(artifacts, task);
  if (stage.dataset.renderKey !== stageKey) {
    stage.textContent = stageText;
    if (finishedAt) {
      const time = document.createElement('div');
      time.className = 'task-time';
      time.textContent = `结束时间：${finishedAt}`;
      stage.appendChild(time);
    }
    stage.dataset.renderKey = stageKey;
  }
  if (progressLabel.textContent !== `${progressNumber}%`) progressLabel.textContent = `${progressNumber}%`;
  if (progressValue.style.width !== `${progressNumber}%`) progressValue.style.width = `${progressNumber}%`;
  if (status && status.className !== statusClass) status.className = statusClass;
  if (status && status.textContent !== statusText) status.textContent = statusText;
  if (deleteButton) {
    const label = isTerminal ? '删除已结束任务' : '强制结束并删除任务';
    if (deleteButton.getAttribute('aria-label') !== label) {
      deleteButton.setAttribute('aria-label', label);
      deleteButton.title = label;
    }
  }
}

function renderTasks(tasks) {
  const countText = String(tasks.length);
  const inProgress = tasks.filter((task) => !isFinishedTask(task)).length;
  const successful = tasks.filter(isSuccessfulTask).length;
  const failed = tasks.filter((task) => isFinishedTask(task) && !isSuccessfulTask(task)).length;
  if (taskCount.textContent !== countText) taskCount.textContent = countText;
  if (progressCount && progressCount.textContent !== String(inProgress)) progressCount.textContent = String(inProgress);
  if (successCount && successCount.textContent !== String(successful)) successCount.textContent = String(successful);
  if (failureCount && failureCount.textContent !== String(failed)) failureCount.textContent = String(failed);
  if (!tasks.length) {
    if (!taskList.querySelector('.empty-state') || taskList.children.length !== 1) {
      taskList.innerHTML = '<div class="empty-state"><div><strong>暂无任务</strong></div></div>';
    }
    return;
  }
  taskList.querySelector('.empty-state')?.remove();
  const existingRows = new Map(
    [...taskList.querySelectorAll('.task-row')].map((row) => [row.dataset.taskId, row]),
  );
  const activeIds = new Set();
  tasks.forEach((task, index) => {
    const taskId = escapeText(task.task_id);
    activeIds.add(taskId);
    const row = existingRows.get(taskId) || createTaskRow(task, index);
    if (existingRows.has(taskId)) updateTaskRow(row, task);
    const currentAtIndex = taskList.children[index];
    if (currentAtIndex !== row) taskList.insertBefore(row, currentAtIndex || null);
  });
  for (const [taskId, row] of existingRows) {
    if (!activeIds.has(taskId)) row.remove();
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

async function deleteTask(row) {
  const taskId = row.dataset.taskId || '';
  const isTerminal = terminalStatuses.has(row.dataset.taskStatus || '');
  if (!isTerminal && !window.confirm('该任务仍在执行。确定要强制结束并删除吗？')) return;
  const button = row.querySelector('.task-delete');
  if (button) button.disabled = true;
  try {
    const suffix = isTerminal ? '' : '?force=1';
    const payload = await request(`/api/tasks/${encodeURIComponent(taskId)}${suffix}`, { method: 'DELETE' });
    renderTasks(payload.tasks || []);
    showNotice(isTerminal ? '任务已删除' : '任务已强制结束并删除');
  } catch (error) {
    if (button) button.disabled = false;
    showNotice(error instanceof Error ? error.message : '任务删除失败');
  }
}

clearCompletedButton?.addEventListener('click', async () => {
  clearCompletedButton.disabled = true;
  try {
    const payload = await request('/api/tasks/completed', { method: 'DELETE' });
    renderTasks(payload.tasks || []);
    const deleted = Number(payload.deleted) || 0;
    showNotice(deleted ? `已清理 ${deleted} 条未在进行中的任务` : '没有可清理的未在进行中任务');
  } catch (error) {
    showNotice(error instanceof Error ? error.message : '清理任务失败');
  } finally {
    clearCompletedButton.disabled = false;
  }
});

async function refreshTasks() {
  try {
    const payload = await request('/api/tasks');
    renderTasks(payload.tasks || []);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : '任务列表刷新失败');
  }
}

form?.addEventListener('submit', async (event) => {
  if (!startButton || !tokenInput || !billingProxyInput || !promotionProxyInput || !concurrencyInput || !retryCountInput) return;
  event.preventDefault();
  showNotice('');
  startButton.disabled = true;
  const buttonDot = startButton.querySelector('.button-dot');
  if (buttonDot) buttonDot.style.background = 'var(--coral)';
  const body = {
    access_tokens: tokenInput.value,
    billing_exit_proxies: billingProxyInput.value,
    promotion_exit_proxies: promotionProxyInput.value,
    concurrency: Number(concurrencyInput.value),
    retry_count: Number(retryCountInput.value),
  };
  try {
    const payload = await request('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
    rememberBatchTokens(payload.batch_id, uniqueTokenLines(tokenInput.value));
    renderTasks(payload.tasks || []);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : '任务提交失败');
  } finally {
    startButton.disabled = false;
    if (buttonDot) buttonDot.style.background = '';
  }
});

for (const [input, storageKey] of persistedFields) {
  input?.addEventListener('input', () => persistField(input, storageKey));
  restoreField(input, storageKey);
}
refreshTasks();
window.setInterval(() => {
  if (document.visibilityState === 'visible') refreshTasks();
}, 1000);
