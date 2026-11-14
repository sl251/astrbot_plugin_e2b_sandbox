/* E2B Sandbox Console. All server-derived display values use textContent only. */
(function startE2BPanel() {
  'use strict';
  const POLL_MS = 10000;
  const TICK_MS = 1000;
  const FAILURE_LIMIT = 3;
  const BRIDGE_READY_MS = 8000;
  const VALID_PAGE_SIZES = Object.freeze([10, 20, 50]);
  const STATUS_LABELS = Object.freeze({
    running: '运行中',
    paused: '已暂停',
    starting: '启动中',
    unknown: '待确认',
  });
  const BLOCKED_LABELS = Object.freeze({
    busy: '会话正在执行或清理中，操作不会排队。',
    owner_missing: '原所属 Key 未记录或已移除，禁止跨 Key 操作。',
    identity_unconfirmed: '尚未确认目标沙箱 ID，暂不支持管理操作。',
    stopping: '插件正在停止，暂不接受管理操作。',
    session_busy: '该会话正在执行或清理，操作未排队。',
    stale_target: '目标沙箱或版本已变化，请刷新后重新确认。',
    target_missing: '目标会话已不存在，请刷新面板。',
    ambiguous_target: '目标沙箱存在重复会话记录，已拒绝操作。',
    invalid_target: '目标标识无效，请刷新后重新确认。',
    pause_unconfirmed: '暂停尚未确认，会话与容量记录仍保留，请刷新检查。',
    kill_unconfirmed: '销毁尚未确认，会话记录仍保留，请刷新检查。',
    operation_failed: '云端操作未确认，请刷新检查，勿重复提交。',
  });

  const $ = (id) => document.getElementById(id);
  const dom = {
    refresh: $('refresh-button'),
    refreshIcon: $('refresh-icon'),
    refreshLabel: $('refresh-label'),
    retry: $('retry-button'),
    auto: $('auto-refresh'),
    pollPill: $('poll-pill'),
    poll: $('poll-state'),
    themeToggle: $('theme-toggle'),
    noticeBar: $('notice-bar'),
    notice: $('notice'),
    version: $('plugin-version'),
    uptime: $('plugin-uptime'),
    lastUpdated: $('last-updated'),
    sessionsRegion: $('sessions-region'),
    tableWrap: $('table-wrap'),
    list: $('session-list'),
    empty: $('session-empty'),
    emptyTitle: $('empty-title'),
    emptyDesc: $('empty-desc'),
    resetFilters: $('reset-filters'),
    search: $('session-search'),
    filter: $('status-filter'),
    sort: $('session-sort'),
    pageSize: $('page-size'),
    previous: $('previous-page'),
    next: $('next-page'),
    page: $('page-label'),
    pageSummary: $('page-summary'),
    keys: $('key-list'),
    dialog: $('confirm-dialog'),
    confirm: $('confirm-action'),
    cancel: $('cancel-action'),
    pauseAck: $('pause-ack'),
    killId: $('kill-id'),
    feedback: $('action-feedback'),
  };

  let client = null;
  let snapshot = null;
  let snapshotReceivedAt = 0;
  let baseUptimeSeconds = null;
  let pollTimer = null;
  let tickTimer = null;
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
  let isLoading = false;
  let bridgeUnavailable = false;
  let disposed = false;

  const keyCountdowns = [];
  const relativeTimeNodes = [];
  const lastKpiValues = Object.create(null);

  const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const isFiniteNum = (value) => typeof value === 'number' && Number.isFinite(value);
  const number = (value) => (isFiniteNum(value) ? value : 0);
  const displayNumber = (value) => (isFiniteNum(value) ? String(value) : '—');
  const safeLabel = (value, fallback = '—') => (typeof value === 'string' && value.trim() ? value.trim() : fallback);

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

  function formatLocalTime(tsSeconds, fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const date = new Date(tsSeconds * 1000);
    if (Number.isNaN(date.getTime())) return fallback;
    const pad = (n) => String(n).padStart(2, '0');
    const y = date.getFullYear();
    const m = pad(date.getMonth() + 1);
    const d = pad(date.getDate());
    const hh = pad(date.getHours());
    const mm = pad(date.getMinutes());
    const ss = pad(date.getSeconds());
    return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
  }

  function formatCompactTime(tsSeconds, nowMs = Date.now(), fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const date = new Date(tsSeconds * 1000);
    if (Number.isNaN(date.getTime())) return fallback;
    const pad = (n) => String(n).padStart(2, '0');
    const nowYear = new Date(nowMs).getFullYear();
    const y = date.getFullYear();
    const m = pad(date.getMonth() + 1);
    const d = pad(date.getDate());
    const hh = pad(date.getHours());
    const mm = pad(date.getMinutes());
    return y !== nowYear ? `${y}-${m}-${d} ${hh}:${mm}` : `${m}-${d} ${hh}:${mm}`;
  }

  function formatRelativeTime(tsSeconds, nowMs, fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const diffSec = Math.floor((nowMs - tsSeconds * 1000) / 1000);
    if (!Number.isFinite(diffSec)) return fallback;
    if (diffSec < 5) return '刚刚';
    if (diffSec < 60) return `${diffSec} 秒前`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
    return `${Math.floor(diffSec / 86400)} 天前`;
  }

  function formatDuration(seconds) {
    if (!isFiniteNum(seconds) || seconds < 0) return '—';
    const total = Math.floor(seconds);
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const mins = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (days > 0) return `${days}天 ${String(hours).padStart(2, '0')}小时`;
    if (hours > 0) return `${hours}小时 ${String(mins).padStart(2, '0')}分`;
    if (mins > 0) return `${mins}分 ${String(secs).padStart(2, '0')}秒`;
    return `${secs}秒`;
  }

  function formatUnit(value, unit) {
    return isFiniteNum(value) ? `${value} ${unit}` : '—';
  }

  function setKpiValue(id, valueStr) {
    const node = $(id);
    if (!node) return;
    const prev = lastKpiValues[id];
    text(node, valueStr);
    if (prev !== undefined && prev !== valueStr) {
      node.className = 'kpi-value mono is-updated';
      if (typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(() => {
          window.setTimeout(() => {
            if (!disposed) node.className = 'kpi-value mono';
          }, 260);
        });
      }
    }
    lastKpiValues[id] = valueStr;
  }

  function applyTheme(theme) {
    if (!document.documentElement) return;
    const next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    if (dom.themeToggle) {
      dom.themeToggle.setAttribute('aria-pressed', next === 'light' ? 'true' : 'false');
    }
  }

  function applyContextTheme(context) {
    if (context && typeof context.isDark === 'boolean') {
      applyTheme(context.isDark ? 'dark' : 'light');
    }
  }

  function restoreFocus() {
    if (lastFocusedElement && lastFocusedElement.isConnected && !lastFocusedElement.disabled) {
      lastFocusedElement.focus();
    } else if (dom.refresh && !dom.refresh.disabled) {
      dom.refresh.focus();
    } else {
      const section = $('sessions-section');
      if (section && typeof section.focus === 'function') section.focus();
    }
    lastFocusedElement = null;
  }

  function setNotice(message, kind = '', showRetry = false) {
    text(dom.notice, message);
    if (dom.noticeBar) {
      dom.noticeBar.className = kind ? `notice notice--${kind}` : 'notice';
    }
    if (dom.retry) {
      dom.retry.hidden = !showRetry;
    }
  }

  function safeFailure(error) {
    if (window.E2BPanelBridge && error instanceof window.E2BPanelBridge.PanelClientError) {
      if (error.code && hasOwn(BLOCKED_LABELS, error.code)) {
        return BLOCKED_LABELS[error.code];
      }
      return error.message;
    }
    return '请求未完成，请检查登录状态并手动刷新。';
  }

  function setLoadingState(loading) {
    isLoading = Boolean(loading);
    if (dom.sessionsRegion) {
      dom.sessionsRegion.setAttribute('aria-busy', isLoading ? 'true' : 'false');
    }
    if (dom.refreshIcon) {
      dom.refreshIcon.setAttribute('class', isLoading ? 'refresh-icon is-spinning' : 'refresh-icon');
    }
    text(dom.refreshLabel || dom.refresh, isLoading ? '刷新中…' : '刷新快照');
    updatePollLabel();
  }

  function stopPolling() {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function updatePollLabel() {
    let label = '已连接';
    let pillClass = 'poll-pill poll-pill--active';
    let detail = '自动刷新运行中';
    if (bridgeUnavailable) {
      label = '桥接不可用';
      pillClass = 'poll-pill poll-pill--error';
      detail = '未连接到宿主页面桥接';
    } else if (!client) {
      label = '等待宿主桥接';
      pillClass = 'poll-pill';
      detail = '正在等待宿主页面就绪';
    } else if (isLoading) {
      label = '刷新中…';
      pillClass = 'poll-pill poll-pill--active';
      detail = '正在读取本地追踪快照';
    } else if (document.hidden) {
      label = '已暂停';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '页面在后台，已暂停轮询';
    } else if (actionBusy) {
      label = '已暂停';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '正在执行管理操作，已暂停轮询';
    } else if (dom.dialog && dom.dialog.open) {
      label = '已暂停';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '等待二次确认，已暂停轮询';
    } else if (dom.auto && !dom.auto.checked) {
      label = '已暂停';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '已关闭自动刷新';
    } else if (failures >= FAILURE_LIMIT) {
      label = '连接异常';
      pillClass = 'poll-pill poll-pill--error';
      detail = '连续刷新失败，已暂停自动轮询';
    }
    text(dom.poll, label);
    if (dom.pollPill) {
      dom.pollPill.className = pillClass;
      dom.pollPill.setAttribute('title', detail);
    }
  }

  function schedulePoll() {
    stopPolling();
    updatePollLabel();
    if (!client || disposed || document.hidden || (dom.auto && !dom.auto.checked)
        || actionBusy || (dom.dialog && dom.dialog.open) || failures >= FAILURE_LIMIT) {
      return;
    }
    pollTimer = window.setTimeout(() => { void refresh(false); }, POLL_MS);
  }

  function cancelRead() {
    readSequence += 1;
    if (readController) readController.abort();
    readController = null;
    setLoadingState(false);
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
    if (!client || disposed || document.hidden || actionBusy || (dom.dialog && dom.dialog.open)) return false;
    if (manual) failures = 0;
    stopPolling();
    cancelRead();
    const sequence = readSequence;
    const controller = new AbortController();
    readController = controller;
    setLoadingState(true);
    setNotice('正在读取本地追踪快照，不会连接或唤醒云端沙箱。', '', false);
    try {
      const result = validateSnapshot(await client.getSnapshot(controller.signal));
      if (disposed || sequence !== readSequence || controller.signal.aborted) return false;
      snapshot = result;
      snapshotReceivedAt = Date.now();
      const pluginInfo = result.plugin && typeof result.plugin === 'object' ? result.plugin : null;
      baseUptimeSeconds = pluginInfo && isFiniteNum(pluginInfo.uptime_seconds) && pluginInfo.uptime_seconds >= 0
        ? pluginInfo.uptime_seconds : null;
      failures = 0;
      renderSnapshot();
      setNotice('快照已更新。所有状态来自本地追踪，暂停与销毁操作均需二次确认。', 'success', false);
      return true;
    } catch (error) {
      if (disposed || sequence !== readSequence || (error && error.name === 'AbortError')) return false;
      failures += 1;
      invalidateActions();
      const suffix = failures >= FAILURE_LIMIT ? '（自动轮询已暂停）' : '';
      setNotice(`${safeFailure(error)}${suffix}`, 'error', true);
      return false;
    } finally {
      if (sequence === readSequence) {
        readController = null;
        setLoadingState(false);
        schedulePoll();
      }
    }
  }

  function renderSnapshot() {
    if (!snapshot) return;
    const pluginInfo = snapshot.plugin && typeof snapshot.plugin === 'object' ? snapshot.plugin : {};
    text(dom.version, typeof pluginInfo.version === 'string' && pluginInfo.version.trim()
      ? `v${pluginInfo.version.trim()}` : 'v—');
    text(dom.lastUpdated, formatLocalTime(snapshot.generated_at, '—'));

    const counts = snapshot.counts;
    const cap = snapshot.capacity;
    setKpiValue('count-running', displayNumber(counts.running));
    const busyCount = isFiniteNum(counts.busy) && counts.busy > 0 ? counts.busy : 0;
    text($('running-sub'), busyCount > 0 ? `占用运行槽位 · ${busyCount} 个忙碌` : '占用运行槽位');
    setKpiValue('count-paused', displayNumber(counts.paused));
    setKpiValue('count-unknown', displayNumber(counts.unknown));
    setKpiValue('capacity-waiting', displayNumber(cap.waiting));
    text($('count-total'), displayNumber(counts.total));

    setKpiValue('capacity-used', displayNumber(cap.occupied));
    text($('capacity-limit'), displayNumber(cap.limit));

    const validOccupied = isFiniteNum(cap.occupied);
    const validLimit = isFiniteNum(cap.limit) && cap.limit > 0;
    const percentText = validOccupied && validLimit
      ? `${Math.min(100, Math.max(0, Math.round((cap.occupied / cap.limit) * 100)))}%`
      : '—';
    text($('capacity-percent'), percentText);
    text($('capacity-brief'), `可用 ${displayNumber(cap.available)} · 排队 ${displayNumber(cap.waiting)}`);

    const progress = $('capacity-progress');
    if (progress) {
      const maxLimit = Math.max(1, number(cap.limit));
      progress.max = maxLimit;
      progress.value = Math.min(maxLimit, Math.max(0, number(cap.occupied)));
    }

    text($('capacity-limit-detail'), formatUnit(cap.limit, '个'));
    text($('capacity-occupied-detail'), formatUnit(cap.occupied, '个'));
    text($('capacity-free'), formatUnit(cap.available, '个'));
    text($('capacity-queue'), `${displayNumber(cap.waiting)} / ${displayNumber(cap.max_queue)}`);
    text($('capacity-timeout'), formatUnit(cap.wait_timeout_seconds, '秒'));
    text($('policy-idle-pause'), formatUnit(cap.idle_pause_seconds, '秒'));
    text($('policy-paused-retention'), formatUnit(cap.paused_retention_hours, '小时'));
    text($('policy-upload-limit'), formatUnit(cap.upload_limit_mb, 'MB'));
    text($('policy-return-limit'), formatUnit(cap.return_file_limit_mb, 'MB'));

    const pendingBanner = $('capacity-pending');
    if (pendingBanner) {
      const mismatch = isFiniteNum(cap.runtime_limit) && isFiniteNum(cap.limit) && cap.runtime_limit !== cap.limit;
      pendingBanner.hidden = !mismatch;
      if (mismatch) {
        text(pendingBanner, `生效上限 ${cap.runtime_limit}（配置 ${cap.limit}）`);
      }
    }

    renderSessions();
    renderKeys();
    startTicker();
  }

  function getPageSize() {
    const raw = dom.pageSize ? Number(dom.pageSize.value) : 20;
    return VALID_PAGE_SIZES.includes(raw) ? raw : 20;
  }

  function matchesFilter(session, query, filter) {
    const labelText = typeof session.session_label === 'string' ? session.session_label : '';
    const sandboxText = typeof session.sandbox_id === 'string' ? session.sandbox_id : '';
    const haystack = `${labelText}\n${sandboxText}`.toLocaleLowerCase('zh-CN');
    if (query && !haystack.includes(query)) return false;
    if (filter === 'busy') return session.busy === true;
    if (filter === 'starting') return session.status === 'starting';
    if (filter === 'unknown') return session.status !== 'running' && session.status !== 'paused' && session.status !== 'starting';
    return filter === 'all' || session.status === filter;
  }

  function sortSessions(sessions, sortKey) {
    const numOrZero = (v) => (isFiniteNum(v) ? v : 0);
    const refStr = (s) => (typeof s.session_ref === 'string' ? s.session_ref : '');
    return sessions.slice().sort((a, b) => {
      let diff = 0;
      if (sortKey === 'last_active_asc') {
        diff = numOrZero(a.last_active) - numOrZero(b.last_active);
      } else if (sortKey === 'created_at_desc') {
        diff = numOrZero(b.created_at) - numOrZero(a.created_at);
      } else if (sortKey === 'created_at_asc') {
        diff = numOrZero(a.created_at) - numOrZero(b.created_at);
      } else {
        diff = numOrZero(b.last_active) - numOrZero(a.last_active);
      }
      if (diff !== 0) return diff;
      return refStr(a).localeCompare(refStr(b));
    });
  }

  function renderSessions() {
    if (!snapshot) return;
    relativeTimeNodes.length = 0;
    const query = dom.search ? dom.search.value.trim().toLocaleLowerCase('zh-CN') : '';
    const filter = dom.filter ? dom.filter.value : 'all';
    const sortKey = dom.sort ? dom.sort.value : 'last_active_desc';
    const pageSize = getPageSize();

    const allSessions = snapshot.sessions.filter((session) => session && typeof session === 'object');
    const filtered = allSessions.filter((session) => matchesFilter(session, query, filter));
    const sorted = sortSessions(filtered, sortKey);

    const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
    pageIndex = Math.min(pageIndex, pageCount - 1);
    const start = pageIndex * pageSize;
    const visible = sorted.slice(start, start + pageSize);

    const fragment = document.createDocumentFragment();
    visible.forEach((session) => fragment.appendChild(sessionRow(session)));
    if (dom.list) dom.list.replaceChildren(fragment);

    const hasRows = visible.length > 0;
    if (dom.tableWrap) dom.tableWrap.hidden = !hasRows;
    if (dom.empty) {
      dom.empty.hidden = hasRows;
      if (!hasRows) {
        const hasFilters = Boolean(query || filter !== 'all');
        text(dom.emptyTitle, hasFilters ? '没有匹配的会话' : '还没有沙箱会话');
        text(dom.emptyDesc, hasFilters
          ? '换个关键词，或把状态筛选切回全部。'
          : '对话里第一次执行代码时，这里会自动出现记录。');
        if (dom.resetFilters) dom.resetFilters.hidden = !hasFilters;
      }
    }

    text($('session-summary'), `筛选 ${sorted.length} / 总计 ${allSessions.length} 条`);
    const rangeStart = sorted.length ? start + 1 : 0;
    const rangeEnd = start + visible.length;
    text(dom.pageSummary, sorted.length ? `显示 ${rangeStart}–${rangeEnd} / 共 ${sorted.length} 条` : '共 0 条');
    text(dom.page, `${pageIndex + 1} / ${pageCount}`);
    if (dom.previous) dom.previous.disabled = pageIndex === 0;
    if (dom.next) dom.next.disabled = pageIndex >= pageCount - 1;
    updateRelativeTimes();
  }

  function sessionRow(session) {
    const tr = element('tr', 'session-row');

    // Col 1: Session label & Sandbox ID
    const tdIdentity = element('td', 'col-identity');
    tdIdentity.setAttribute('data-label', '会话 / 沙箱');
    const idBox = element('div', 'cell-identity');
    const rawLabel = safeLabel(session.session_label, '未命名会话');
    const labelNode = element('span', 'session-label', rawLabel);
    if (rawLabel !== '未命名会话') labelNode.setAttribute('title', rawLabel);
    idBox.appendChild(labelNode);

    const rawSandbox = typeof session.sandbox_id === 'string' ? session.sandbox_id.trim() : '';
    const sandboxText = rawSandbox || (typeof session.sandbox_id === 'string' ? '尚未确认' : '—');
    const sandboxNode = element('span', 'sandbox-id mono', sandboxText);
    if (rawSandbox) sandboxNode.setAttribute('title', rawSandbox);
    idBox.appendChild(sandboxNode);
    tdIdentity.appendChild(idBox);
    tr.appendChild(tdIdentity);

    // Col 2: Status
    const tdStatus = element('td', 'col-status');
    tdStatus.setAttribute('data-label', '状态');
    const statusWrap = element('div', 'cell-status');
    const status = typeof session.status === 'string' && hasOwn(STATUS_LABELS, session.status)
      ? session.status : 'unknown';
    const badge = element('span', `badge badge--${status}`);
    badge.appendChild(element('i', `dot dot--${status}`));
    badge.appendChild(element('span', '', STATUS_LABELS[status]));
    statusWrap.appendChild(badge);
    if (session.busy === true) {
      statusWrap.appendChild(element('span', 'badge badge--busy', '忙碌'));
    }
    if (session.owner_known === false) {
      statusWrap.appendChild(element('span', 'badge badge--danger', 'Key 缺失'));
    }
    tdStatus.appendChild(statusWrap);
    tr.appendChild(tdStatus);

    // Col 3: Key & Template
    const tdKey = element('td', 'col-key');
    tdKey.setAttribute('data-label', 'Key / 模板');
    const keyStack = element('div', 'cell-meta-stack');
    const keyLabel = safeLabel(session.key_label);
    const keyNode = element('span', 'cell-primary cell-key-name', keyLabel);
    if (keyLabel !== '—') keyNode.setAttribute('title', keyLabel);
    keyStack.appendChild(keyNode);
    const rawTemplate = typeof session.template === 'string' ? session.template.trim() : '';
    const tplNode = element('span', 'cell-secondary cell-template mono', rawTemplate || '—');
    if (rawTemplate) tplNode.setAttribute('title', rawTemplate);
    keyStack.appendChild(tplNode);
    tdKey.appendChild(keyStack);
    tr.appendChild(tdKey);

    // Col 4: Timestamps (last active relative + created + paused)
    const nowMs = Date.now();
    const tdTime = element('td', 'col-time');
    tdTime.setAttribute('data-label', '时间记录');
    const timeStack = element('div', 'cell-meta-stack');
    const activeRel = element('span', 'cell-primary cell-time-primary mono', formatRelativeTime(session.last_active, nowMs));
    const activeExact = formatLocalTime(session.last_active);
    if (activeExact !== '—') activeRel.setAttribute('title', `最后活动: ${activeExact}`);
    relativeTimeNodes.push({node: activeRel, ts: session.last_active});
    timeStack.appendChild(activeRel);

    const createdCompact = formatCompactTime(session.created_at, nowMs);
    const createdExact = formatLocalTime(session.created_at);
    const createdNode = element('span', 'cell-secondary cell-time-secondary mono', `创建 ${createdCompact}`);
    if (createdExact !== '—') createdNode.setAttribute('title', `创建时间: ${createdExact}`);
    timeStack.appendChild(createdNode);

    if (status === 'paused' && isFiniteNum(session.paused_at) && session.paused_at > 0) {
      const pausedCompact = formatCompactTime(session.paused_at, nowMs);
      const pausedExact = formatLocalTime(session.paused_at);
      const pausedNode = element('span', 'cell-secondary cell-time-secondary mono', `暂停于 ${pausedCompact}`);
      if (pausedExact !== '—') pausedNode.setAttribute('title', `暂停时间: ${pausedExact}`);
      timeStack.appendChild(pausedNode);
    }
    tdTime.appendChild(timeStack);
    tr.appendChild(tdTime);

    // Col 5: Files (pending / generated)
    const tdFiles = element('td', 'col-files');
    tdFiles.setAttribute('data-label', '文件数');
    const filesWrap = element('div', 'cell-files mono');
    filesWrap.appendChild(element('span', '', displayNumber(session.pending_files)));
    filesWrap.appendChild(element('span', 'file-sep', '/'));
    filesWrap.appendChild(element('span', '', displayNumber(session.generated_files)));
    filesWrap.setAttribute('title', '待传文件 / 已生成文件');
    tdFiles.appendChild(filesWrap);
    tr.appendChild(tdFiles);

    // Col 6: Actions
    const tdActions = element('td', 'col-actions');
    tdActions.setAttribute('data-label', '操作');
    const actionBox = element('div', 'cell-actions');
    const actionRow = element('div', 'action-row');
    const ownerMissing = session.owner_known === false;
    const canPause = session.can_pause === true && !ownerMissing;
    const canKill = session.can_kill === true && !ownerMissing;

    const pauseBtn = element('button', 'btn btn--ghost btn--sm', '暂停');
    pauseBtn.type = 'button';
    pauseBtn.disabled = !canPause || actionBusy;
    pauseBtn.addEventListener('click', () => openConfirmation(session, 'pause'));
    actionRow.appendChild(pauseBtn);

    const killBtn = element('button', 'btn btn--danger-ghost btn--sm', '销毁');
    killBtn.type = 'button';
    killBtn.disabled = !canKill || actionBusy;
    killBtn.addEventListener('click', () => openConfirmation(session, 'kill'));
    actionRow.appendChild(killBtn);

    actionBox.appendChild(actionRow);

    const blockedKey = typeof session.blocked_reason === 'string' ? session.blocked_reason : '';
    const reason = ownerMissing && !hasOwn(BLOCKED_LABELS, blockedKey)
      ? BLOCKED_LABELS.owner_missing
      : hasOwn(BLOCKED_LABELS, blockedKey)
        ? BLOCKED_LABELS[blockedKey]
        : '';
    if (reason) {
      actionBox.appendChild(element('span', 'blocked-hint', reason));
    }

    tdActions.appendChild(actionBox);
    tr.appendChild(tdActions);

    return tr;
  }

  function renderKeys() {
    keyCountdowns.length = 0;
    const validKeys = snapshot.keys.filter((key) => key && typeof key === 'object');
    const availableCount = validKeys.filter((key) => key.available !== false && number(key.cooldown_seconds) <= 0).length;
    text($('key-count'), validKeys.length ? `${availableCount}/${validKeys.length} 可用` : '0 个');

    const fragment = document.createDocumentFragment();
    validKeys.forEach((key) => {
      const item = element('div', 'key-item');
      const top = element('div', 'key-item-top');
      top.appendChild(element('span', 'key-name', safeLabel(key.label)));
      const stateBadge = element('span', 'badge');
      top.appendChild(stateBadge);
      item.appendChild(top);

      const bottom = element('div', 'key-item-bottom');
      bottom.appendChild(element('span', '', `绑定会话 ${displayNumber(key.bound_sessions)}`));
      const countdown = element('span', 'mono');
      bottom.appendChild(countdown);
      item.appendChild(bottom);

      const validCooldown = isFiniteNum(key.cooldown_seconds);
      keyCountdowns.push({
        badge: stateBadge,
        countdown,
        valid: validCooldown,
        remaining: number(key.cooldown_seconds),
        availableFlag: key.available !== false,
      });
      fragment.appendChild(item);
    });

    if (!validKeys.length) {
      fragment.appendChild(element('p', 'muted small', '暂无已配置 API Key。'));
    }
    if (dom.keys) dom.keys.replaceChildren(fragment);
    updateDynamicTicks();
  }

  function updateRelativeTimes() {
    const nowMs = Date.now();
    relativeTimeNodes.forEach((item) => {
      text(item.node, formatRelativeTime(item.ts, nowMs));
    });
  }

  function updateDynamicTicks() {
    const nowMs = Date.now();
    const elapsedSec = snapshotReceivedAt > 0 ? Math.max(0, (nowMs - snapshotReceivedAt) / 1000) : 0;

    if (baseUptimeSeconds !== null) {
      text(dom.uptime, formatDuration(baseUptimeSeconds + Math.floor(elapsedSec)));
    } else {
      text(dom.uptime, '—');
    }

    keyCountdowns.forEach((item) => {
      if (!item.valid) {
        item.badge.className = 'badge badge--unknown';
        text(item.badge, item.availableFlag ? '状态未知' : '不可用');
        item.countdown.className = 'mono key-unavailable';
        text(item.countdown, '—');
        return;
      }
      const remaining = Math.max(0, Math.ceil(item.remaining - elapsedSec));
      if (remaining > 0) {
        item.badge.className = 'badge badge--paused';
        text(item.badge, '冷却中');
        item.countdown.className = 'mono key-cooldown';
        const mm = Math.floor(remaining / 60);
        const ss = String(remaining % 60).padStart(2, '0');
        text(item.countdown, `剩余 ${mm}:${ss}`);
      } else if (!item.availableFlag) {
        item.badge.className = 'badge badge--unknown';
        text(item.badge, '不可用');
        item.countdown.className = 'mono key-unavailable';
        text(item.countdown, '暂不可分配');
      } else {
        item.badge.className = 'badge badge--running';
        text(item.badge, '可用');
        item.countdown.className = 'mono key-ready';
        text(item.countdown, item.remaining > 0 ? '冷却到期 · 待刷新' : '可新建轮询');
      }
    });

    updateRelativeTimes();
  }

  function stopTicker() {
    window.clearInterval(tickTimer);
    tickTimer = null;
  }

  function startTicker() {
    stopTicker();
    updateDynamicTicks();
    if (!disposed && !document.hidden) {
      tickTimer = window.setInterval(updateDynamicTicks, TICK_MS);
    }
  }

  function openConfirmation(session, action) {
    if (disposed || actionBusy || (dom.dialog && dom.dialog.open)) return;
    if (!session || session.owner_known === false) return;
    if (session[action === 'pause' ? 'can_pause' : 'can_kill'] !== true) return;
    if (typeof session.session_ref !== 'string' || !session.session_ref
        || typeof session.sandbox_id !== 'string' || !session.sandbox_id
        || typeof session.version !== 'string' || !session.version) {
      return;
    }
    if (!dom.dialog || typeof dom.dialog.showModal !== 'function') {
      setNotice('当前浏览器不支持安全确认对话框，管理操作已禁用。', 'error', false);
      invalidateActions();
      return;
    }

    stopPolling();
    cancelRead();
    lastFocusedElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    pendingTarget = Object.freeze({session: {...session}, action});

    text($('confirm-title'), action === 'kill' ? '永久销毁此沙箱？' : '暂停此沙箱？');
    text($('confirm-description'), action === 'kill'
      ? '此操作不可撤销。该沙箱的云端进程、环境变量与未保存文件将被永久清除。'
      : '暂停会释放运行槽位并保留云端变量与文件；面板不提供恢复操作。');
    text($('confirm-session'), safeLabel(session.session_label, '未命名会话'));
    text($('confirm-sandbox'), safeLabel(session.sandbox_id));
    text($('confirm-key'), safeLabel(session.key_label));

    const pauseLabel = $('pause-ack-label');
    const killLabel = $('kill-input-label');
    if (pauseLabel) pauseLabel.hidden = action !== 'pause';
    if (killLabel) killLabel.hidden = action !== 'kill';

    if (dom.pauseAck) dom.pauseAck.checked = false;
    if (dom.killId) dom.killId.value = '';
    if (dom.feedback) dom.feedback.textContent = '';

    dom.confirm.className = action === 'kill' ? 'btn btn--danger' : 'btn btn--primary';
    text(dom.confirm, action === 'kill' ? '确认销毁' : '确认暂停');
    dom.confirm.disabled = true;
    dom.cancel.disabled = false;

    dom.dialog.showModal();
    updatePollLabel();
    if (action === 'kill' && dom.killId) dom.killId.focus();
    else if (dom.pauseAck) dom.pauseAck.focus();
  }

  function updateConfirmation() {
    if (!pendingTarget || actionBusy || !dom.confirm) return;
    dom.confirm.disabled = pendingTarget.action === 'kill'
      ? dom.killId.value !== pendingTarget.session.sandbox_id
      : !dom.pauseAck.checked;
  }

  function closeConfirmation() {
    if (actionBusy) return;
    pendingTarget = null;
    if (dom.dialog && dom.dialog.open && typeof dom.dialog.close === 'function') {
      dom.dialog.close();
    }
    restoreFocus();
    schedulePoll();
  }

  async function performAction() {
    if (disposed || !pendingTarget || actionBusy || !dom.confirm || dom.confirm.disabled) return;
    const {session, action} = pendingTarget;
    actionBusy = true;
    stopPolling();
    cancelRead();

    dom.confirm.disabled = true;
    dom.cancel.disabled = true;
    if (dom.refresh) dom.refresh.disabled = true;
    if (dom.killId) dom.killId.disabled = true;
    if (dom.pauseAck) dom.pauseAck.disabled = true;

    text(dom.confirm, '执行中…');
    text(dom.feedback, '正在校验目标版本与会话锁，请勿重复提交。');
    renderSessions();
    updatePollLabel();

    let success = false;
    let message = '';
    try {
      const result = await client.act({
        action,
        session_ref: session.session_ref,
        expected_sandbox_id: session.sandbox_id,
        expected_version: session.version,
        confirm: true,
      });
      if (!result || result.action !== action || result.sandbox_id !== session.sandbox_id
          || !['paused', 'already_missing', 'killed'].includes(result.outcome)) {
        throw new Error('invalid result');
      }
      success = true;
      message = result.outcome === 'killed'
        ? '销毁成功。'
        : result.outcome === 'paused'
          ? '暂停成功。'
          : '云端目标已不存在，本地记录已清理。';
    } catch (error) {
      message = safeFailure(error);
    } finally {
      actionBusy = false;
      pendingTarget = null;
      if (dom.killId) dom.killId.disabled = false;
      if (dom.pauseAck) dom.pauseAck.disabled = false;
      dom.cancel.disabled = false;
      if (dom.refresh) dom.refresh.disabled = false;
      text(dom.confirm, action === 'kill' ? '确认销毁' : '确认暂停');
      if (dom.dialog && dom.dialog.open && typeof dom.dialog.close === 'function') {
        dom.dialog.close();
      }
      if (!disposed) {
        invalidateActions();
        setNotice(`${message} 正在同步最新快照…`, success ? 'success' : 'error', !success);
        restoreFocus();
        if (!document.hidden) {
          const refreshed = await refresh(true);
          if (!disposed && refreshed) {
            setNotice(`${message} 已同步最新状态。`, success ? 'success' : 'error', !success);
          }
        } else {
          setNotice(`${message} 返回前台后将刷新状态。`, success ? 'success' : 'error', !success);
        }
        schedulePoll();
      }
    }
  }

  async function initialize() {
    const sdk = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!sdk || typeof sdk.ready !== 'function' || !window.E2BPanelBridge || window.parent === window) {
      setNotice('宿主不支持插件页面桥接，或未从插件详情页打开。已安全降级为只读说明页。', 'error', false);
      bridgeUnavailable = true;
      setLoadingState(false);
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
      bridgeUnavailable = false;
      client = window.E2BPanelBridge.createClient(window, 30000);
      if (dom.refresh) dom.refresh.disabled = false;
      if (document.hidden) {
        setLoadingState(false);
        setNotice('页面当前在后台，切换到前台后将自动读取快照。', '', false);
        schedulePoll();
      } else {
        await refresh(true);
      }
    } catch (error) {
      if (disposed) return;
      const detail = window.E2BPanelBridge && error instanceof window.E2BPanelBridge.PanelClientError
        ? error.message
        : '无法建立可信宿主桥接。请登录 WebUI 后从 E2B 插件详情重新打开。';
      setNotice(detail, 'error', false);
      bridgeUnavailable = true;
      setLoadingState(false);
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
    stopTicker();
    window.clearTimeout(searchTimer);
    searchTimer = null;
    window.clearTimeout(readyTimer);
    readyTimer = null;
    if (typeof unsubscribeContext === 'function') {
      unsubscribeContext();
      unsubscribeContext = null;
    }
    if (dom.dialog && dom.dialog.open && typeof dom.dialog.close === 'function') {
      dom.dialog.close();
    }
    if (client) {
      client.destroy();
      client = null;
    }
  }

  if (dom.refresh) dom.refresh.addEventListener('click', () => { void refresh(true); });
  if (dom.retry) dom.retry.addEventListener('click', () => { void refresh(true); });
  if (dom.auto) dom.auto.addEventListener('change', schedulePoll);
  if (dom.themeToggle) {
    dom.themeToggle.addEventListener('click', () => {
      const current = document.documentElement ? document.documentElement.getAttribute('data-theme') : 'dark';
      applyTheme(current === 'light' ? 'dark' : 'light');
    });
  }
  if (dom.search) {
    dom.search.addEventListener('input', () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => { pageIndex = 0; renderSessions(); }, 180);
    });
  }
  if (dom.filter) dom.filter.addEventListener('change', () => { pageIndex = 0; renderSessions(); });
  if (dom.sort) dom.sort.addEventListener('change', () => { pageIndex = 0; renderSessions(); });
  if (dom.pageSize) dom.pageSize.addEventListener('change', () => { pageIndex = 0; renderSessions(); });
  if (dom.resetFilters) {
    dom.resetFilters.addEventListener('click', () => {
      if (dom.search) dom.search.value = '';
      if (dom.filter) dom.filter.value = 'all';
      pageIndex = 0;
      renderSessions();
    });
  }
  if (dom.previous) {
    dom.previous.addEventListener('click', () => {
      pageIndex = Math.max(0, pageIndex - 1);
      renderSessions();
    });
  }
  if (dom.next) {
    dom.next.addEventListener('click', () => {
      pageIndex += 1;
      renderSessions();
    });
  }
  if (dom.pauseAck) {
    dom.pauseAck.addEventListener('change', updateConfirmation);
    dom.pauseAck.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && pendingTarget && pendingTarget.action === 'pause'
          && !dom.confirm.disabled && !actionBusy) {
        event.preventDefault();
        void performAction();
      }
    });
  }
  if (dom.killId) {
    dom.killId.addEventListener('input', updateConfirmation);
    dom.killId.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && pendingTarget && pendingTarget.action === 'kill'
          && !dom.confirm.disabled && !actionBusy) {
        event.preventDefault();
        void performAction();
      }
    });
  }
  if (dom.cancel) dom.cancel.addEventListener('click', closeConfirmation);
  if (dom.confirm) dom.confirm.addEventListener('click', () => { void performAction(); });
  if (dom.dialog) {
    dom.dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      if (!actionBusy) closeConfirmation();
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (disposed) return;
    stopPolling();
    stopTicker();
    if (document.hidden) {
      cancelRead();
      updatePollLabel();
      return;
    }
    if (snapshot) startTicker();
    if (client && (!dom.auto || dom.auto.checked) && failures < FAILURE_LIMIT
        && !actionBusy && !(dom.dialog && dom.dialog.open)) {
      void refresh(false);
    } else {
      schedulePoll();
    }
  });

  window.addEventListener('pagehide', destroy, {once: true});
  void initialize();
})();
