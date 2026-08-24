const form = document.querySelector('#batch-form');
const taskList = document.querySelector('#task-list');
const taskCount = document.querySelector('#task-count');
const clearButton = document.querySelector('#clear-button');
const startButton = form.querySelector('button[type="submit"]');
const notice = document.querySelector('#notice');

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

function escapeText(value) {
  return String(value ?? '');
}

function renderTasks(tasks) {
  taskCount.textContent = String(tasks.length);
  if (!tasks.length) {
    taskList.innerHTML = '<div class="empty-state"><span class="empty-number">00</span><div><strong>暂无任务</strong><p>填写三组输入后开始分析。</p></div></div>';
    return;
  }
  taskList.replaceChildren(...tasks.map((task, index) => {
    const row = document.createElement('article');
    row.className = 'task-row';
    row.style.animationDelay = `${Math.min(index, 12) * 35}ms`;

    const main = document.createElement('div');
    main.className = 'task-main';
    const id = document.createElement('div');
    id.className = 'task-id';
    id.textContent = `TASK ${escapeText(task.task_id)}`;
    const email = document.createElement('div');
    email.className = 'task-email';
    email.textContent = task.email || '未识别邮箱';
    main.append(id, email);

    const stage = document.createElement('div');
    stage.className = 'task-stage';
    stage.textContent = escapeText(task.stage);
    const time = document.createElement('div');
    time.className = 'task-time';
    time.textContent = escapeText(task.created_at);
    stage.appendChild(time);

    const progress = document.createElement('div');
    progress.className = 'task-progress';
    const progressLabel = document.createElement('small');
    progressLabel.textContent = `${Number(task.progress) || 0}%`;
    const track = document.createElement('div');
    track.className = 'progress-track';
    const value = document.createElement('div');
    value.className = 'progress-value';
    value.style.width = `${Math.max(0, Math.min(100, Number(task.progress) || 0))}%`;
    track.appendChild(value);
    progress.append(progressLabel, track);

    const status = document.createElement('div');
    status.className = `task-status ${escapeText(task.status)}`;
    status.textContent = escapeText(task.status_label);
    row.append(main, stage, progress, status);
    return row;
  }));
}

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

async function refreshTasks() {
  try {
    const payload = await request('/api/tasks');
    renderTasks(payload.tasks || []);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : '任务列表刷新失败');
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  showNotice('');
  startButton.disabled = true;
  startButton.querySelector('.button-dot').style.background = 'var(--coral)';
  const body = {
    access_tokens: document.querySelector('#access-tokens').value,
    checkout_proxies: document.querySelector('#checkout-proxies').value,
    promotion_proxies: document.querySelector('#promotion-proxies').value,
  };
  try {
    const payload = await request('/api/tasks', { method: 'POST', body: JSON.stringify(body) });
    renderTasks(payload.tasks || []);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : '任务提交失败');
  } finally {
    startButton.disabled = false;
    startButton.querySelector('.button-dot').style.background = '';
  }
});

clearButton.addEventListener('click', async () => {
  try {
    const payload = await request('/api/tasks', { method: 'DELETE' });
    renderTasks(payload.tasks || []);
    showNotice('');
  } catch (error) {
    showNotice(error instanceof Error ? error.message : '清空失败');
  }
});

refreshTasks();
