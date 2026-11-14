/* Native Chinese dashboard. All server-derived display values use textContent. */
(function startE2BPanel() {
  'use strict';
  const POLL_MS = 10000;
  const PAGE_SIZE = 20;
  const FAILURE_LIMIT = 3;
  const BRIDGE_READY_MS = 8000;
  const STATUS_LABELS = Object.freeze({running: '运行中', paused: '已暂停', starting: '启动中', unknown: '待确认'});
  const BLOCKED_LABELS = Object.freeze({
    busy: '此会话正在执行或清理；管理操作不会排队。',
    owner_missing: '原所属 Key 未记录或未配置，禁止换 Key 操作。',
    identity_unconfirmed: '尚无已确认的目标沙箱 ID，不做自动恢复。',
    stopping: '插件正在停止，暂不接受操作。',
  });
  const $ = (id) => document.getElementById(id);
  const dom = {
    refresh: $('refresh-button'), auto: $('auto-refresh'), poll: $('poll-state'),
    notice: $('notice'), list: $('session-list'), search: $('session-search'), filter: $('status-filter'),
    previous: $('previous-page'), next: $('next-page'), page: $('page-label'), keys: $('key-list'),
    dialog: $('confirm-dialog'), confirm: $('confirm-action'), cancel: $('cancel-action'),
    pauseAck: $('pause-ack'), killId: $('kill-id'), feedback: $('action-feedback'),
  };
  let client = null;
  let snapshot = null;
  let snapshotReceivedAt = 0;
  let pollTimer = null;
  let cooldownTimer = null;
  let searchTimer = null;
  let readyTimer = null;
  let unsubscribeContext = null;
  let lastFocusedElement = null;
  let readController = null;
  let readSequence = 0;
  let failures = 0;
  let pageIndex = 0;
  let pendingTarget = null;
  let actionBusy = false;
  let disposed = false;
  const keyCountdowns = [];

  const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const text = (node, value) => {
    if (!node) return;
    node.textContent = value == null || Number.isNaN(value) ? '—' : String(value);
  };
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) text(node, value);
    return node;
  };
  const number = (value) => typeof value === 'number' && Number.isFinite(value) ? value : 0;
  const displayNumber = (value) => typeof value === 'number' && Number.isFinite(value) ? value : '—';
  const safeLabel = (value, fallback = '—') => typeof value === 'string' && value.trim() ? value : fallback;
  const formatTime = (value, fallback = '未记录') => {
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return fallback;
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString('zh-CN', {hour12: false});
  };
  const applyContextTheme = (context) => {
    if (context && typeof context.isDark === 'boolean' && document.documentElement) {
      document.documentElement.setAttribute('data-theme', context.isDark ? 'dark' : 'light');
    }
  };
  const restoreFocus = () => {
    if (lastFocusedElement && lastFocusedElement.isConnected && !lastFocusedElement.disabled) {
      lastFocusedElement.focus();
    } else if (!dom.refresh.disabled) {
      dom.refresh.focus();
    } else {
      const section = $('sessions-section');
      if (section && typeof section.focus === 'function') section.focus();
    }
    lastFocusedElement = null;
  };
  const setNotice = (message, kind = '') => {
    text(dom.notice, message);
    dom.notice.className = kind ? `notice notice--${kind}` : 'notice';
  };
  const safeFailure = (error) => {
      return error instanceof window.E2BPanelBridge.PanelClientError
      ? error.message : '请求未完成，请检查登录状态并手动刷新。';
  };

  function stopPolling() {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function updatePollLabel() {
    if (document.hidden) text(dom.poll, '页面在后台 · 轮询已暂停');
    else if (actionBusy) text(dom.poll, '管理操作中 · 暂停刷新');
    else if (dom.dialog.open) text(dom.poll, '二次确认中 · 暂停刷新');
    else if (!dom.auto.checked) text(dom.poll, '自动刷新已关闭');
    else if (failures >= FAILURE_LIMIT) text(dom.poll, '连续失败 · 请手动刷新');
    else text(dom.poll, '每 10 秒读取本地快照');
  }

  function schedulePoll() {
    stopPolling();
    updatePollLabel();
    if (!client || disposed || document.hidden || !dom.auto.checked || actionBusy
        || dom.dialog.open || failures >= FAILURE_LIMIT) return;
    pollTimer = window.setTimeout(() => { void refresh(false); }, POLL_MS);
  }

  function cancelRead() {
    readSequence += 1;
    if (readController) readController.abort();
    readController = null;
    dom.list.setAttribute('aria-busy', 'false');
    text(dom.refresh, '刷新快照');
  }

  function invalidateActions() {
    if (snapshot && Array.isArray(snapshot.sessions)) {
      snapshot.sessions.forEach((session) => {
        if (!session || typeof session !== 'object') return;
        session.can_pause = false;
        session.can_kill = false;
      });
      renderSessions();
    }
  }

  function validateSnapshot(value) {
    if (!value || typeof value !== 'object' || value.source !== 'local_tracking_only'
        || !value.counts || typeof value.counts !== 'object'
        || !value.capacity || typeof value.capacity !== 'object'
        || !Array.isArray(value.sessions) || !Array.isArray(value.keys)) {
      throw new Error('invalid snapshot');
    }
    return value;
  }

  async function refresh(manual) {
    if (!client || disposed || document.hidden || actionBusy || dom.dialog.open) return false;
    if (manual) failures = 0;
    stopPolling();
    cancelRead();
    const sequence = readSequence;
    const controller = new AbortController();
    readController = controller;
    dom.list.setAttribute('aria-busy', 'true');
    text(dom.refresh, '刷新中…');
    setNotice('正在读取本地追踪快照，不会连接或恢复沙箱。');
    try {
      const result = validateSnapshot(await client.getSnapshot(controller.signal));
      if (disposed || sequence !== readSequence || controller.signal.aborted) return false;
      snapshot = result;
      snapshotReceivedAt = Date.now();
      failures = 0;
      renderSnapshot();
      setNotice('快照已更新。管理操作仅作用于已有目标；暂停与销毁都需要再次确认。', 'success');
      return true;
    } catch (error) {
      if (disposed || sequence !== readSequence || (error && error.name === 'AbortError')) return false;
      failures += 1;
      invalidateActions();
      setNotice(`${safeFailure(error)}${failures >= FAILURE_LIMIT ? ' 自动刷新已暂停。' : ''}`, 'error');
      return false;
    } finally {
      if (sequence === readSequence) {
        readController = null;
        dom.list.setAttribute('aria-busy', 'false');
        text(dom.refresh, '刷新快照');
        schedulePoll();
      }
    }
  }

  function renderSnapshot() {
    ['total', 'running', 'paused', 'unknown'].forEach((name) => text($(`count-${name}`), displayNumber(snapshot.counts[name])));
    text($('busy-count'), `忙碌 ${displayNumber(snapshot.counts.busy)}`);
    const capacity = snapshot.capacity;
    text($('capacity-used'), displayNumber(capacity.occupied));
    text($('capacity-limit'), displayNumber(capacity.limit));
    text($('capacity-free'), displayNumber(capacity.available));
    text($('capacity-queue'), `${displayNumber(capacity.waiting)} / ${displayNumber(capacity.max_queue)}`);
    text($('capacity-timeout'), typeof capacity.wait_timeout_seconds === 'number' && Number.isFinite(capacity.wait_timeout_seconds)
      ? `${capacity.wait_timeout_seconds} 秒` : '—');
    const maxLimit = Math.max(1, number(capacity.limit));
    $('capacity-progress').max = maxLimit;
    $('capacity-progress').value = Math.min(maxLimit, Math.max(0, number(capacity.occupied)));
    $('capacity-pending').hidden = !(typeof capacity.runtime_limit === 'number' && Number.isFinite(capacity.runtime_limit)
      && typeof capacity.limit === 'number' && Number.isFinite(capacity.limit) && capacity.runtime_limit !== capacity.limit);
    text($('last-updated'), `最近更新：${formatTime(snapshot.generated_at, '—')}`);
    renderSessions();
    renderKeys();
  }

  function matchesFilter(session, query, filter) {
    const labelText = typeof session.session_label === 'string' ? session.session_label : '';
    const sandboxText = typeof session.sandbox_id === 'string' ? session.sandbox_id : '';
    const haystack = `${labelText}\n${sandboxText}`.toLocaleLowerCase('zh-CN');
    if (query && !haystack.includes(query)) return false;
    if (filter === 'busy') return session.busy === true;
    if (filter === 'unknown') return session.status !== 'running' && session.status !== 'paused';
    return filter === 'all' || session.status === filter;
  }

  function renderSessions() {
    if (!snapshot) return;
    const query = dom.search.value.trim().toLocaleLowerCase('zh-CN');
    const allSessions = snapshot.sessions.filter((session) => session && typeof session === 'object');
    const sessions = allSessions.filter((session) => matchesFilter(session, query, dom.filter.value));
    const pageCount = Math.max(1, Math.ceil(sessions.length / PAGE_SIZE));
    pageIndex = Math.min(pageIndex, pageCount - 1);
    const fragment = document.createDocumentFragment();
    const visible = sessions.slice(pageIndex * PAGE_SIZE, (pageIndex + 1) * PAGE_SIZE);
    visible.forEach((session) => fragment.appendChild(sessionCard(session)));
    if (!visible.length) {
      fragment.appendChild(element('p', 'empty-state', allSessions.length
        ? '没有符合筛选条件的会话。' : '暂无已有沙箱。面板不会为你新建或恢复沙箱。'));
    }
    dom.list.replaceChildren(fragment);
    text($('session-summary'), `本地记录 ${allSessions.length} 条 · 当前筛选 ${sessions.length} 条`);
    text(dom.page, `第 ${pageIndex + 1} / ${pageCount} 页`);
    dom.previous.disabled = pageIndex === 0;
    dom.next.disabled = pageIndex >= pageCount - 1;
  }

  function sessionCard(session) {
    const card = element('article', 'session-card');
    const top = element('div', 'session-top');
    top.appendChild(element('h3', 'session-name', safeLabel(session.session_label, '未命名会话')));
    const badges = element('div', 'badges');
    const status = typeof session.status === 'string' && hasOwn(STATUS_LABELS, session.status) ? session.status : 'unknown';
    badges.appendChild(element('span', `badge badge--${status}`, STATUS_LABELS[status]));
    if (session.busy === true) badges.appendChild(element('span', 'badge', '忙碌'));
    top.appendChild(badges);
    card.appendChild(top);
    const id = element('div', 'session-id');
    id.appendChild(element('span', 'muted small', '沙箱 ID'));
    const sandboxText = typeof session.sandbox_id === 'string' ? (session.sandbox_id || '尚未确认') : '—';
    id.appendChild(element('span', 'mono', sandboxText));
    card.appendChild(id);
    const meta = element('div', 'session-meta');
    meta.appendChild(element('span', '', safeLabel(session.key_label, '—')));
    meta.appendChild(element('span', '', `最近活动 ${formatTime(session.last_active)}`));
    if (typeof session.paused_at === 'number' && Number.isFinite(session.paused_at) && session.paused_at > 0) {
      meta.appendChild(element('span', '', `暂停于 ${formatTime(session.paused_at)}`));
    }
    card.appendChild(meta);
    const bottom = element('div', 'session-bottom');
    const blockedKey = typeof session.blocked_reason === 'string' ? session.blocked_reason : '';
    const reason = hasOwn(BLOCKED_LABELS, blockedKey) ? BLOCKED_LABELS[blockedKey]
      : (status === 'paused' ? '已暂停；此页不提供恢复。' : '操作前会再次校验完整 ID 与版本。');
    bottom.appendChild(element('p', 'session-blocked', reason));
    const actions = element('div', 'session-actions');
    [['pause', '暂停', 'button button--quiet', session.can_pause === true],
      ['kill', '销毁', 'button button--danger-quiet', session.can_kill === true]].forEach(([action, label, className, allowed]) => {
      const button = element('button', className, label);
      button.type = 'button';
      button.disabled = !allowed || actionBusy;
      button.addEventListener('click', () => openConfirmation(session, action));
      actions.appendChild(button);
    });
    bottom.appendChild(actions);
    card.appendChild(bottom);
    return card;
  }

  function renderKeys() {
    keyCountdowns.length = 0;
    const validKeys = snapshot.keys.filter((key) => key && typeof key === 'object');
    text($('key-count'), `${validKeys.length} 个`);
    const fragment = document.createDocumentFragment();
    validKeys.forEach((key) => {
      const item = element('div', 'key-item');
      item.appendChild(element('strong', '', safeLabel(key.label)));
      const countdown = element('span', 'key-cooldown');
      item.appendChild(countdown);
      item.appendChild(element('p', 'muted small', `绑定 ${displayNumber(key.bound_sessions)} 个已有会话`));
      const validCooldown = typeof key.cooldown_seconds === 'number' && Number.isFinite(key.cooldown_seconds);
      keyCountdowns.push({node: countdown, valid: validCooldown, remaining: number(key.cooldown_seconds)});
      fragment.appendChild(item);
    });
    if (!validKeys.length) fragment.appendChild(element('p', 'muted small', '暂无已配置 Key。此页不显示或修改配置。'));
    dom.keys.replaceChildren(fragment);
    updateCooldowns();
    window.clearInterval(cooldownTimer);
    cooldownTimer = !disposed && !document.hidden ? window.setInterval(updateCooldowns, 1000) : null;
  }

  function updateCooldowns() {
    const elapsed = Math.max(0, (Date.now() - snapshotReceivedAt) / 1000);
    keyCountdowns.forEach((item) => {
      if (!item.valid) {
        item.node.className = 'key-cooldown';
        text(item.node, '—');
        return;
      }
      const remaining = Math.max(0, Math.ceil(item.remaining - elapsed));
      item.node.className = remaining ? 'key-cooldown' : 'key-ready';
      text(item.node, remaining ? `冷却 ${Math.floor(remaining / 60)}分${String(remaining % 60).padStart(2, '0')}秒`
        : item.remaining > 0 ? '冷却已到期 · 待刷新' : '可用于新建轮询');
    });
  }

  function openConfirmation(session, action) {
    if (disposed || actionBusy || dom.dialog.open) return;
    if (!session || session[action === 'pause' ? 'can_pause' : 'can_kill'] !== true) return;
    if (typeof session.session_ref !== 'string' || !session.session_ref
        || typeof session.sandbox_id !== 'string' || !session.sandbox_id
        || typeof session.version !== 'string' || !session.version) return;
    if (typeof dom.dialog.showModal !== 'function') {
      setNotice('当前浏览器不支持安全确认对话框，管理操作已禁用。请更新浏览器。', 'error');
      invalidateActions();
      return;
    }
    stopPolling();
    cancelRead();
    lastFocusedElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // A frozen snapshot of identity. Never substitute the latest row after the
    // administrator has confirmed a different sandbox or lifecycle version.
    pendingTarget = Object.freeze({session: {...session}, action});
    text($('confirm-title'), action === 'kill' ? '销毁这个沙箱？' : '暂停这个沙箱？');
    text($('confirm-description'), action === 'kill'
      ? '此操作不可撤销。这个沙箱的云端文件、变量与进程会被永久删除；不会影响其他会话。'
      : '暂停会停止云端运行并保留变量与文件。只暂停下方目标，不会恢复或新建沙箱。');
    text($('confirm-session'), safeLabel(session.session_label, '未命名会话'));
    text($('confirm-sandbox'), safeLabel(session.sandbox_id));
    text($('confirm-key'), safeLabel(session.key_label));
    $('pause-ack-label').hidden = action !== 'pause';
    $('kill-input-label').hidden = action !== 'kill';
    dom.pauseAck.checked = false;
    dom.killId.value = '';
    dom.feedback.textContent = '';
    dom.confirm.className = action === 'kill' ? 'button button--danger' : 'button button--primary';
    text(dom.confirm, action === 'kill' ? '确认销毁' : '确认暂停');
    dom.confirm.disabled = true;
    dom.cancel.disabled = false;
    dom.dialog.showModal();
    updatePollLabel();
    if (action === 'kill') dom.killId.focus();
    else dom.pauseAck.focus();
  }

  function updateConfirmation() {
    if (!pendingTarget || actionBusy) return;
    dom.confirm.disabled = pendingTarget.action === 'kill'
      ? dom.killId.value !== pendingTarget.session.sandbox_id : !dom.pauseAck.checked;
  }

  function closeConfirmation() {
    if (actionBusy) return;
    pendingTarget = null;
    if (dom.dialog.open && typeof dom.dialog.close === 'function') dom.dialog.close();
    restoreFocus();
    schedulePoll();
  }

  async function performAction() {
    if (disposed || !pendingTarget || actionBusy || dom.confirm.disabled) return;
    const {session, action} = pendingTarget;
    actionBusy = true;
    stopPolling();
    cancelRead();
    dom.confirm.disabled = true;
    dom.cancel.disabled = true;
    dom.refresh.disabled = true;
    dom.killId.disabled = true;
    dom.pauseAck.disabled = true;
    text(dom.confirm, '处理中…');
    text(dom.feedback, '正在校验目标版本与会话锁。请勿重复提交；请求超时不代表云端操作停止。');
    renderSessions();
    updatePollLabel();
    let success = false;
    let message;
    try {
      const result = await client.act({
        action,
        session_ref: session.session_ref,
        expected_sandbox_id: session.sandbox_id,
        expected_version: session.version,
        confirm: true,
      });
      if (!result || result.action !== action || result.sandbox_id !== session.sandbox_id
          || !['paused', 'already_missing', 'killed'].includes(result.outcome)) throw new Error('invalid result');
      success = true;
      message = result.outcome === 'killed' ? '销毁已确认。' : result.outcome === 'paused' ? '暂停已确认。' : '云端目标已不存在，本地记录已清除。';
    } catch (error) {
      message = safeFailure(error);
    } finally {
      actionBusy = false;
      pendingTarget = null;
      dom.killId.disabled = false;
      dom.pauseAck.disabled = false;
      dom.cancel.disabled = false;
      dom.refresh.disabled = false;
      text(dom.confirm, action === 'kill' ? '确认销毁' : '确认暂停');
      if (dom.dialog.open && typeof dom.dialog.close === 'function') dom.dialog.close();
      if (!disposed) {
        invalidateActions();
        setNotice(`${message} 正在读取最新状态。`, success ? 'success' : 'error');
        restoreFocus();
        if (!document.hidden) {
          const refreshed = await refresh(true);
          if (!disposed && refreshed) setNotice(`${message} 请以最新快照为准。`, success ? 'success' : 'error');
        } else {
          setNotice(`${message} 请以最新快照为准。`, success ? 'success' : 'error');
        }
        schedulePoll();
      }
    }
  }

  async function initialize() {
    const sdk = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!sdk || typeof sdk.ready !== 'function' || !window.E2BPanelBridge || window.parent === window) {
      setNotice('宿主不支持插件页面桥接，或此页未从插件详情打开。面板已安全降级，不会发起管理请求。', 'error');
      text(dom.poll, '面板不可用');
      return;
    }
    try {
      const context = await Promise.race([
        sdk.ready(),
        new Promise((_, reject) => {
          readyTimer = window.setTimeout(() => reject(new Error('ready timeout')), BRIDGE_READY_MS);
        }),
      ]);
      if (disposed) return;
      if (!context || context.pluginName !== 'astrbot_plugin_e2b_sandbox') throw new Error('wrong plugin');
      applyContextTheme(context);
      if (typeof sdk.onContext === 'function') {
        const maybeUnsub = sdk.onContext((nextContext) => {
          if (!disposed) applyContextTheme(nextContext);
        });
        if (typeof maybeUnsub === 'function') unsubscribeContext = maybeUnsub;
      }
      client = window.E2BPanelBridge.createClient(window, 30000);
      dom.refresh.disabled = false;
      if (document.hidden) {
        dom.list.setAttribute('aria-busy', 'false');
        setNotice('页面当前在后台，切换到前台后将读取快照。');
        schedulePoll();
      } else {
        await refresh(true);
      }
    } catch (error) {
      if (disposed) return;
      const detail = error instanceof window.E2BPanelBridge.PanelClientError
        ? error.message
        : '无法建立可信宿主桥接。请登录 WebUI，从 E2B 插件详情重新打开；旧版宿主可能不支持此页。';
      setNotice(detail, 'error');
      text(dom.poll, '桥接不可用');
    } finally {
      window.clearTimeout(readyTimer);
      readyTimer = null;
    }
  }

  function destroy() {
    if (disposed) return;
    disposed = true;
    stopPolling();
    cancelRead();
    window.clearInterval(cooldownTimer);
    cooldownTimer = null;
    window.clearTimeout(searchTimer);
    searchTimer = null;
    window.clearTimeout(readyTimer);
    readyTimer = null;
    if (typeof unsubscribeContext === 'function') {
      unsubscribeContext();
      unsubscribeContext = null;
    }
    if (dom.dialog.open && typeof dom.dialog.close === 'function') dom.dialog.close();
    if (client) {
      client.destroy();
      client = null;
    }
  }

  dom.refresh.addEventListener('click', () => { void refresh(true); });
  dom.auto.addEventListener('change', schedulePoll);
  dom.search.addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { pageIndex = 0; renderSessions(); }, 200);
  });
  dom.filter.addEventListener('change', () => { pageIndex = 0; renderSessions(); });
  dom.previous.addEventListener('click', () => { pageIndex = Math.max(0, pageIndex - 1); renderSessions(); });
  dom.next.addEventListener('click', () => { pageIndex += 1; renderSessions(); });
  dom.pauseAck.addEventListener('change', updateConfirmation);
  dom.killId.addEventListener('input', updateConfirmation);
  dom.killId.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && pendingTarget && pendingTarget.action === 'kill'
        && !dom.confirm.disabled && !actionBusy) {
      event.preventDefault();
      void performAction();
    }
  });
  dom.cancel.addEventListener('click', closeConfirmation);
  dom.confirm.addEventListener('click', () => { void performAction(); });
  dom.dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (!actionBusy) closeConfirmation();
  });
  document.addEventListener('visibilitychange', () => {
    if (disposed) return;
    stopPolling();
    window.clearInterval(cooldownTimer);
    cooldownTimer = null;
    if (document.hidden) {
      cancelRead();
      updatePollLabel();
      return;
    }
    if (snapshot) {
      updateCooldowns();
      cooldownTimer = window.setInterval(updateCooldowns, 1000);
    }
    if (client && dom.auto.checked && failures < FAILURE_LIMIT && !actionBusy && !dom.dialog.open) void refresh(false);
    else schedulePoll();
  });
  window.addEventListener('pagehide', destroy, {once: true});
  void initialize();
})();
