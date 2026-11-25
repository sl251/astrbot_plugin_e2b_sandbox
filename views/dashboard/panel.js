/* E2B 云沙箱可视化控制台交互逻辑。所有服务端返回文本统一通过 textContent 写入以防御 XSS。 */
(function startE2BPanel() {
  'use strict';
  const POLL_MS = 10000;
  const TICK_MS = 1000;
  const TOAST_MS = 2600;
  const FAILURE_LIMIT = 3;
  const BRIDGE_READY_MS = 8000;
  const COMPACT_CARD_WIDTH = 704;
  const VALID_PAGE_SIZES = Object.freeze([10, 20, 50]);
  const THEME_MODES = Object.freeze(['auto', 'light', 'dark']);
  const THEME_LABELS = Object.freeze({auto: '跟随宿主', light: '浅色', dark: '深色'});
  const STATUS_LABELS = Object.freeze({
    running: '运行中',
    paused: '已休眠',
    starting: '唤醒中',
    unknown: '待确认',
  });
  const PLATFORM_NAMES = Object.freeze({
    aiocqhttp: 'QQ · OneBot',
    onebot: 'QQ · OneBot',
    qqofficial: 'QQ 官方',
    webchat: 'WebChat',
    telegram: 'Telegram',
    discord: 'Discord',
    lark: '飞书',
    feishu: '飞书',
    wecom: '企业微信',
    wechatpadpro: '微信',
    gewechat: '微信',
    slack: 'Slack',
    dingtalk: '钉钉',
    kook: 'KOOK',
  });
  const BLOCKED_LABELS = Object.freeze({
    busy: '正在执行中',
    owner_missing: '原 Key 已移除',
    identity_unconfirmed: '沙箱 ID 待确认',
    stopping: '插件停止中',
    session_busy: '该会话正在执行或清理，操作未排队。',
    stale_target: '目标沙箱或版本已变化，请刷新后重试。',
    target_missing: '目标会话已不存在，请刷新面板。',
    ambiguous_target: '存在重复会话记录，已拒绝操作。',
    invalid_target: '目标标识无效，请刷新面板。',
    pause_unconfirmed: '暂停未确认，请刷新检查。',
    kill_unconfirmed: '销毁未确认，请刷新检查。',
    operation_failed: '云端操作未确认，请刷新检查。',
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
    themeLabel: $('theme-label'),
    scopeBadge: $('scope-badge'),
    noticeBar: $('notice-bar'),
    notice: $('notice'),
    version: $('plugin-version'),
    uptime: $('plugin-uptime'),
    lastUpdated: $('last-updated'),
    slotMatrix: $('slot-matrix'),
    kpiRunningCard: $('kpi-running-card'),
    kpiPausedCard: $('kpi-paused-card'),
    kpiKeysCard: $('kpi-keys-card'),
    tabSessions: $('tab-sessions'),
    tabKeys: $('tab-keys'),
    tabKeysDot: $('tab-keys-dot'),
    panelSessions: $('tabpanel-sessions'),
    panelKeys: $('tabpanel-keys'),
    viewSwitchWrap: $('view-switch-wrap'),
    viewTableBtn: $('view-table-btn'),
    viewCardBtn: $('view-card-btn'),
    sessionsRegion: $('sessions-region'),
    tableWrap: $('table-wrap'),
    list: $('session-list'),
    empty: $('session-empty'),
    emptyTitle: $('empty-title'),
    resetFilters: $('reset-filters'),
    search: $('session-search'),
    filter: $('status-filter'),
    statusPills: $('status-pills'),
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
    fillKillId: $('fill-kill-id'),
    killMatchHint: $('kill-match-hint'),
    feedback: $('action-feedback'),
    toast: $('toast'),
    toastText: $('toast-text'),
    toastClose: $('toast-close'),
  };

  let client = null;
  let snapshot = null;
  let snapshotReceivedAt = 0;
  let baseUptimeSeconds = null;
  let pollTimer = null;
  let tickTimer = null;
  let searchTimer = null;
  let readyTimer = null;
  let toastTimer = null;
  let resizeObserver = null;
  let unsubscribeContext = null;
  let lastFocusedElement = null;
  let readController = null;
  let readSequence = 0;
  let failures = 0;
  let pageIndex = 0;
  let activeTab = 'sessions';
  let viewMode = 'table';
  let themeMode = 'auto';
  let hostIsDark = null;
  let pendingTarget = null;
  let actionBusy = false;
  let isLoading = false;
  let bridgeUnavailable = false;
  let disposed = false;

  const keyCountdowns = [];
  const keyCooldownPeaks = Object.create(null);
  const relativeTimeNodes = [];
  const lifespanNodes = [];
  const lastKpiValues = Object.create(null);

  const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const isFiniteNum = (value) => typeof value === 'number' && Number.isFinite(value);
  const number = (value) => (isFiniteNum(value) ? value : 0);
  const displayNumber = (value) => (isFiniteNum(value) ? String(value) : '—');
  const safeLabel = (value, fallback = '—') => (typeof value === 'string' && value.trim() ? value.trim() : fallback);

  // 中文及东亚输入法回车上屏保护（同时覆盖标准 isComposing 与旧版 WebKit 229 键码顺序问题）
  const isImeComposing = (event) => Boolean(event && (event.isComposing || event.keyCode === 229));

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

  function cloneIcon(templateId, fallbackClass = '') {
    const tpl = $(templateId);
    if (tpl && tpl.content && typeof tpl.content.cloneNode === 'function') {
      return tpl.content.cloneNode(true);
    }
    return element('span', fallbackClass);
  }

  /* 将 AstrBot 原始会话标识解析为易读来源与平台名称 */
  function parseSessionIdentity(rawLabel) {
    const raw = safeLabel(rawLabel, '');
    if (!raw) {
      return {title: '未命名会话', platform: '', isGroup: false, raw: ''};
    }
    let mainPart = raw;
    let memberId = '';
    const memberSplit = raw.indexOf('#user:');
    if (memberSplit !== -1) {
      mainPart = raw.slice(0, memberSplit);
      memberId = raw.slice(memberSplit + 6).trim();
    }
    const parts = mainPart.split(':');
    if (parts.length >= 3) {
      const rawPlatform = parts[0].trim();
      const rawKind = parts[1].trim();
      const peerId = parts.slice(2).join(':').trim();
      const lowerPlatform = rawPlatform.toLowerCase();
      const platform = hasOwn(PLATFORM_NAMES, lowerPlatform) ? PLATFORM_NAMES[lowerPlatform] : rawPlatform;
      const lowerKind = rawKind.toLowerCase();
      const isGroup = lowerKind.includes('group') || lowerKind.includes('guild') || lowerKind.includes('channel');
      if (isGroup) {
        const title = memberId
          ? `群聊 ${peerId || '未知'} · 成员 ${memberId}`
          : `群聊 ${peerId || raw}`;
        return {title, platform, isGroup: true, raw};
      }
      if (lowerKind.includes('friend') || lowerKind.includes('private') || lowerKind.includes('direct')) {
        return {title: `私聊 ${peerId || raw}`, platform, isGroup: false, raw};
      }
      return {title: peerId || raw, platform, isGroup: false, raw};
    }
    return {title: raw, platform: '', isGroup: false, raw};
  }

  function formatKeyDisplay(label, alias) {
    const base = safeLabel(label);
    const cleanAlias = typeof alias === 'string' ? alias.trim() : '';
    if (!cleanAlias || base === '—') return base;
    return `${base} · ${cleanAlias}`;
  }

  function formatLocalTime(tsSeconds, fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const date = new Date(tsSeconds * 1000);
    if (Number.isNaN(date.getTime())) return fallback;
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function formatClockTime(tsSeconds, fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const date = new Date(tsSeconds * 1000);
    if (Number.isNaN(date.getTime())) return fallback;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function formatCompactTime(tsSeconds, nowMs = Date.now(), fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const date = new Date(tsSeconds * 1000);
    if (Number.isNaN(date.getTime())) return fallback;
    const pad = (n) => String(n).padStart(2, '0');
    const y = date.getFullYear();
    const m = pad(date.getMonth() + 1);
    const d = pad(date.getDate());
    const hh = pad(date.getHours());
    const mm = pad(date.getMinutes());
    return y !== new Date(nowMs).getFullYear() ? `${y}-${m}-${d} ${hh}:${mm}` : `${m}-${d} ${hh}:${mm}`;
  }

  function formatRelativeTime(tsSeconds, nowMs, fallback = '—') {
    if (!isFiniteNum(tsSeconds) || tsSeconds <= 0) return fallback;
    const diffSec = Math.floor((nowMs - tsSeconds * 1000) / 1000);
    if (!Number.isFinite(diffSec)) return fallback;
    if (diffSec < 5) return '刚刚活跃';
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
    if (days > 0) return `${days}天 ${String(hours).padStart(2, '0')}时`;
    if (hours > 0) return `${hours}时 ${String(mins).padStart(2, '0')}分`;
    if (mins > 0) return `${mins}分 ${String(secs).padStart(2, '0')}秒`;
    return `${secs}秒`;
  }

  function formatUnit(value, unit) {
    return isFiniteNum(value) ? `${value} ${unit}` : '—';
  }

  /* 三态主题管理：跟随宿主（默认）/ 浅色 / 深色 */
  function updateMetaColorScheme(content) {
    if (!document.querySelector) return;
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta && typeof meta.setAttribute === 'function') {
      meta.setAttribute('content', content);
    }
  }

  function applyThemeState() {
    if (!document.documentElement) return;
    document.documentElement.setAttribute('data-theme-mode', themeMode);
    if (themeMode === 'auto') {
      if (typeof hostIsDark === 'boolean') {
        const resolved = hostIsDark ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', resolved);
        updateMetaColorScheme(resolved);
      } else {
        updateMetaColorScheme('light dark');
      }
    } else {
      document.documentElement.setAttribute('data-theme', themeMode);
      updateMetaColorScheme(themeMode);
    }
    if (dom.themeLabel) {
      text(dom.themeLabel, THEME_LABELS[themeMode] || '跟随宿主');
    }
    if (dom.themeToggle) {
      const hostDesc = typeof hostIsDark === 'boolean' ? (hostIsDark ? '深色' : '浅色') : '自动';
      const title = themeMode === 'auto'
        ? `当前跟随宿主（${hostDesc}），点击切换浅色`
        : themeMode === 'light'
          ? '当前手动浅色，点击切换深色'
          : '当前手动深色，点击恢复跟随宿主';
      dom.themeToggle.setAttribute('title', title);
      dom.themeToggle.setAttribute('aria-label', title);
    }
  }

  function setThemeMode(nextMode) {
    themeMode = THEME_MODES.includes(nextMode) ? nextMode : 'auto';
    applyThemeState();
  }

  function applyContextTheme(context) {
    if (context && typeof context.isDark === 'boolean') {
      hostIsDark = context.isDark;
      if (themeMode === 'auto') {
        applyThemeState();
      }
    }
  }

  /* 轻提示（Snackbar）：弹窗打开时不打扰，聚焦关闭按钮时暂停消失 */
  function hideToast() {
    window.clearTimeout(toastTimer);
    toastTimer = null;
    if (dom.toast) dom.toast.hidden = true;
  }

  function showToast(message, kind = '') {
    if (!dom.toast || !dom.toastText || disposed) return;
    if (dom.dialog && dom.dialog.open) return;
    window.clearTimeout(toastTimer);
    text(dom.toastText, message);
    dom.toast.className = kind ? `toast toast--${kind}` : 'toast';
    dom.toast.hidden = false;
    toastTimer = window.setTimeout(() => {
      if (dom.toastClose && document.activeElement === dom.toastClose) {
        return;
      }
      hideToast();
    }, TOAST_MS);
  }

  /* 顶部错误横幅：仅在报错或桥接不可用时展开，正常状态隐藏保持极简 */
  function setNotice(message, kind = '', showRetry = false) {
    text(dom.notice, message);
    if (dom.noticeBar) {
      const isError = kind === 'error';
      dom.noticeBar.className = isError ? 'alert-banner alert-banner--error' : 'alert-banner';
      dom.noticeBar.hidden = !isError;
    }
    if (dom.retry) {
      dom.retry.hidden = !showRetry;
    }
  }

  function setKpiValue(id, valueStr) {
    const node = $(id);
    if (!node) return;
    const prev = lastKpiValues[id];
    text(node, valueStr);
    if (prev !== undefined && prev !== valueStr) {
      node.className = 'overview-value mono is-updated';
      if (typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(() => {
          window.setTimeout(() => {
            if (!disposed) node.className = 'overview-value mono';
          }, 240);
        });
      }
    }
    lastKpiValues[id] = valueStr;
  }

  /* 双 Tab 切换与无障碍键盘导航 */
  function switchTab(nextTab, focusTab = false) {
    activeTab = nextTab === 'keys' ? 'keys' : 'sessions';
    const isSessions = activeTab === 'sessions';
    if (dom.tabSessions) {
      dom.tabSessions.setAttribute('aria-selected', isSessions ? 'true' : 'false');
      dom.tabSessions.setAttribute('tabindex', isSessions ? '0' : '-1');
      if (focusTab && isSessions && typeof dom.tabSessions.focus === 'function') dom.tabSessions.focus();
    }
    if (dom.tabKeys) {
      dom.tabKeys.setAttribute('aria-selected', isSessions ? 'false' : 'true');
      dom.tabKeys.setAttribute('tabindex', isSessions ? '-1' : '0');
      if (focusTab && !isSessions && typeof dom.tabKeys.focus === 'function') dom.tabKeys.focus();
    }
    if (dom.panelSessions) dom.panelSessions.hidden = !isSessions;
    if (dom.panelKeys) dom.panelKeys.hidden = isSessions;
    if (dom.viewSwitchWrap) {
      dom.viewSwitchWrap.hidden = !isSessions;
      if (isSessions && dom.sessionsRegion && isFiniteNum(dom.sessionsRegion.clientWidth) && dom.sessionsRegion.clientWidth > 0) {
        syncCompactSwitcher(dom.sessionsRegion.clientWidth);
      }
    }
  }

  function setViewMode(nextMode) {
    viewMode = nextMode === 'cards' ? 'cards' : 'table';
    if (dom.sessionsRegion) {
      dom.sessionsRegion.setAttribute('data-view', viewMode);
    }
    if (dom.viewTableBtn) {
      const active = viewMode === 'table';
      dom.viewTableBtn.className = active ? 'view-btn is-active' : 'view-btn';
      dom.viewTableBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    if (dom.viewCardBtn) {
      const active = viewMode === 'cards';
      dom.viewCardBtn.className = active ? 'view-btn is-active' : 'view-btn';
      dom.viewCardBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
  }

  function syncCompactSwitcher(width) {
    if (!dom.viewSwitchWrap || activeTab !== 'sessions') return;
    if (isFiniteNum(width) && width > 0) {
      dom.viewSwitchWrap.hidden = width < COMPACT_CARD_WIDTH;
    }
  }

  function syncStatusPills(filterValue) {
    if (!dom.statusPills || !dom.statusPills.querySelectorAll) return;
    const pills = dom.statusPills.querySelectorAll('.filter-pill');
    for (let i = 0; i < pills.length; i += 1) {
      const btn = pills[i];
      const val = btn.getAttribute('data-filter');
      const active = val === filterValue || (filterValue === 'starting' && val === 'pending') || (filterValue === 'unknown' && val === 'pending');
      btn.className = active ? 'filter-pill is-active' : 'filter-pill';
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
  }

  function setStatusFilter(value) {
    if (dom.filter) dom.filter.value = value;
    syncStatusPills(value);
    pageIndex = 0;
    renderSessions();
  }

  function copySandboxId(sandboxId) {
    if (!sandboxId) return;
    const nav = window.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
      nav.clipboard.writeText(sandboxId).then(
        () => showToast('已复制沙箱 ID', 'success'),
        () => fallbackCopyText(sandboxId),
      );
      return;
    }
    fallbackCopyText(sandboxId);
  }

  function fallbackCopyText(value) {
    try {
      if (!document.body || typeof document.execCommand !== 'function') {
        showToast('当前环境不支持剪贴板复制');
        return;
      }
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.className = 'sr-only';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(area);
      showToast(ok ? '已复制沙箱 ID' : '复制失败，请手动选中复制', ok ? 'success' : '');
    } catch {
      showToast('复制失败，请手动选中复制');
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

  function safeFailure(error) {
    if (window.E2BPanelBridge && error instanceof window.E2BPanelBridge.PanelClientError) {
      if (error.code && hasOwn(BLOCKED_LABELS, error.code)) {
        return BLOCKED_LABELS[error.code];
      }
      return error.message;
    }
    return '请求未完成，请检查登录状态后重试。';
  }

  function setLoadingState(loading) {
    isLoading = Boolean(loading);
    if (dom.sessionsRegion) {
      dom.sessionsRegion.setAttribute('aria-busy', isLoading ? 'true' : 'false');
    }
    if (dom.refreshIcon) {
      dom.refreshIcon.setAttribute('class', isLoading ? 'refresh-icon is-spinning' : 'refresh-icon');
    }
    text(dom.refreshLabel || dom.refresh, isLoading ? '同步中…' : '刷新');
    updatePollLabel();
  }

  function stopPolling() {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function updatePollLabel() {
    let label = '10s 自动同步';
    let pillClass = 'sync-pill sync-pill--active';
    if (bridgeUnavailable) {
      label = '未连接';
      pillClass = 'sync-pill sync-pill--error';
    } else if (!client) {
      label = '连接中';
      pillClass = 'sync-pill';
    } else if (isLoading) {
      label = '同步中…';
      pillClass = 'sync-pill sync-pill--active';
    } else if (document.hidden) {
      label = '后台静默';
      pillClass = 'sync-pill sync-pill--paused';
    } else if (actionBusy) {
      label = '执行中';
      pillClass = 'sync-pill sync-pill--paused';
    } else if (dom.dialog && dom.dialog.open) {
      label = '等待确认';
      pillClass = 'sync-pill sync-pill--paused';
    } else if (dom.auto && !dom.auto.checked) {
      label = '已暂停同步';
      pillClass = 'sync-pill sync-pill--paused';
    } else if (failures >= FAILURE_LIMIT) {
      label = '同步中断';
      pillClass = 'sync-pill sync-pill--error';
    }
    text(dom.poll, label);
    if (dom.pollPill) {
      dom.pollPill.className = pillClass;
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

  async function refresh(manual, announce = false) {
    if (!client || disposed || document.hidden || actionBusy || (dom.dialog && dom.dialog.open)) return false;
    if (manual) failures = 0;
    stopPolling();
    cancelRead();
    const sequence = readSequence;
    const controller = new AbortController();
    readController = controller;
    setLoadingState(true);
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
      setNotice('', '', false);
      if (announce) showToast('状态已更新', 'success');
      return true;
    } catch (error) {
      if (disposed || sequence !== readSequence || (error && error.name === 'AbortError')) return false;
      failures += 1;
      invalidateActions();
      const suffix = failures >= FAILURE_LIMIT ? '（自动同步已暂停）' : '';
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

  function renderSlotMatrix(limit, runningCount, occupiedCount) {
    if (!dom.slotMatrix) return;
    const safeLimit = isFiniteNum(limit) && limit > 0 ? Math.min(20, Math.floor(limit)) : 0;
    const safeRunning = isFiniteNum(runningCount) && runningCount > 0 ? Math.floor(runningCount) : 0;
    const safeOccupied = isFiniteNum(occupiedCount) && occupiedCount > 0 ? Math.floor(occupiedCount) : 0;
    const fragment = document.createDocumentFragment();
    for (let idx = 0; idx < safeLimit; idx += 1) {
      let cls = 'slot-cell';
      if (idx < safeRunning) cls = 'slot-cell slot-cell--running';
      else if (idx < safeOccupied) cls = 'slot-cell slot-cell--reserved';
      fragment.appendChild(element('span', cls));
    }
    dom.slotMatrix.replaceChildren(fragment);
  }

  function renderSnapshot() {
    if (!snapshot) return;
    const pluginInfo = snapshot.plugin && typeof snapshot.plugin === 'object' ? snapshot.plugin : {};
    text(dom.version, typeof pluginInfo.version === 'string' && pluginInfo.version.trim()
      ? `v${pluginInfo.version.trim()}` : 'v—');
    const updatedNode = dom.lastUpdated;
    text(updatedNode, formatClockTime(snapshot.generated_at, '—'));
    if (updatedNode && isFiniteNum(snapshot.generated_at)) {
      updatedNode.setAttribute('title', formatLocalTime(snapshot.generated_at));
    }

    const counts = snapshot.counts;
    const cap = snapshot.capacity;

    if (dom.scopeBadge) {
      const isolated = cap.sandbox_scope === 'group_member';
      dom.scopeBadge.className = isolated ? 'chip chip--running' : 'chip chip--neutral';
      text(dom.scopeBadge, isolated ? '群聊按成员独立隔离' : '群聊共享');
    }

    setKpiValue('count-running', displayNumber(counts.running));
    const busyCount = isFiniteNum(counts.busy) && counts.busy > 0 ? counts.busy : 0;
    text($('running-sub'), busyCount > 0 ? `${busyCount} 台执行中` : '占用运行槽位');

    setKpiValue('count-paused', displayNumber(counts.paused));
    text($('paused-sub'), isFiniteNum(cap.paused_retention_hours)
      ? `最长保留 ${cap.paused_retention_hours} 小时`
      : '保留状态');

    text($('count-unknown'), displayNumber(counts.unknown));
    text($('count-total'), displayNumber(counts.total));
    text($('pill-count-all'), displayNumber(counts.total));
    text($('pill-count-running'), displayNumber(counts.running));
    text($('pill-count-paused'), displayNumber(counts.paused));
    text($('pill-count-busy'), displayNumber(busyCount));

    setKpiValue('capacity-used', displayNumber(cap.occupied));
    text($('capacity-limit'), displayNumber(cap.limit));
    text($('capacity-waiting'), displayNumber(cap.waiting));

    const validOccupied = isFiniteNum(cap.occupied);
    const validLimit = isFiniteNum(cap.limit) && cap.limit > 0;
    const percentText = validOccupied && validLimit
      ? `${Math.min(100, Math.max(0, Math.round((cap.occupied / cap.limit) * 100)))}%`
      : '—';
    text($('capacity-percent'), percentText);

    const unknownCount = isFiniteNum(counts.unknown) && counts.unknown > 0 ? counts.unknown : 0;
    const briefParts = [`空闲 ${displayNumber(cap.available)}`, `排队 ${displayNumber(cap.waiting)}`];
    if (unknownCount > 0) briefParts.push(`待确认 ${unknownCount}`);
    text($('capacity-brief'), briefParts.join(' · '));
    renderSlotMatrix(cap.limit, counts.running, cap.occupied);

    const progress = $('capacity-progress');
    if (progress) {
      const maxLimit = Math.max(1, number(cap.limit));
      progress.max = maxLimit;
      progress.value = Math.min(maxLimit, Math.max(0, number(cap.occupied)));
    }

    text($('capacity-limit-detail'), formatUnit(cap.limit, '台'));
    text($('capacity-occupied-detail'), formatUnit(cap.occupied, '台'));
    text($('capacity-free'), formatUnit(cap.available, '台'));
    text($('capacity-queue'), `${displayNumber(cap.waiting)} / ${displayNumber(cap.max_queue)}`);
    text($('capacity-timeout'), formatUnit(cap.wait_timeout_seconds, '秒'));
    text($('policy-idle-pause'), formatUnit(cap.idle_pause_seconds, '秒'));
    text($('policy-paused-retention'), formatUnit(cap.paused_retention_hours, '小时'));
    text($('policy-upload-limit'), formatUnit(cap.upload_limit_mb, 'MB'));
    text($('policy-return-limit'), formatUnit(cap.return_file_limit_mb, 'MB'));

    const pendingBanner = $('capacity-pending');
    const limitMismatch = isFiniteNum(cap.runtime_limit) && isFiniteNum(cap.limit) && cap.runtime_limit !== cap.limit;
    if (pendingBanner) {
      pendingBanner.hidden = !limitMismatch;
      if (limitMismatch) {
        text(pendingBanner, `生效 ${cap.runtime_limit} / 配置 ${cap.limit}`);
      }
    }

    renderSessions();
    renderKeys(limitMismatch);
    startTicker();
  }

  function getPageSize() {
    const raw = dom.pageSize ? Number(dom.pageSize.value) : 20;
    return VALID_PAGE_SIZES.includes(raw) ? raw : 20;
  }

  function matchesFilter(session, query, filter) {
    const labelText = typeof session.session_label === 'string' ? session.session_label : '';
    const parsed = parseSessionIdentity(labelText);
    const sandboxText = typeof session.sandbox_id === 'string' ? session.sandbox_id : '';
    const aliasText = typeof session.key_alias === 'string' ? session.key_alias : '';
    const keyText = typeof session.key_label === 'string' ? session.key_label : '';
    const haystack = `${labelText}\n${parsed.title}\n${parsed.platform}\n${sandboxText}\n${keyText}\n${aliasText}`.toLocaleLowerCase('zh-CN');
    if (query && !haystack.includes(query)) return false;
    if (filter === 'busy') return session.busy === true;
    if (filter === 'pending') return session.status !== 'running' && session.status !== 'paused';
    if (filter === 'starting') return session.status === 'starting';
    if (filter === 'unknown') return session.status !== 'running' && session.status !== 'paused' && session.status !== 'starting';
    return filter === 'all' || session.status === filter;
  }

  function sortSessions(sessions, sortKey) {
    const numOrZero = (v) => (isFiniteNum(v) ? v : 0);
    const refStr = (s) => (typeof s.session_ref === 'string' ? s.session_ref : '');
    const copy = Array.from(sessions);
    copy.sort((a, b) => {
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
    return copy;
  }

  function renderSessions() {
    if (!snapshot) return;
    relativeTimeNodes.length = 0;
    lifespanNodes.length = 0;
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

    const retentionSeconds = isFiniteNum(snapshot.capacity.paused_retention_hours)
      ? snapshot.capacity.paused_retention_hours * 3600
      : 12 * 3600;

    const fragment = document.createDocumentFragment();
    visible.forEach((session) => fragment.appendChild(sessionRow(session, retentionSeconds)));
    if (dom.list) dom.list.replaceChildren(fragment);

    const hasRows = visible.length > 0;
    if (dom.tableWrap) dom.tableWrap.hidden = !hasRows;
    if (dom.empty) {
      dom.empty.hidden = hasRows;
      if (!hasRows) {
        const hasFilters = Boolean(query || filter !== 'all');
        text(dom.emptyTitle, hasFilters ? '没有匹配的云沙箱实例' : '暂无云沙箱实例');
        if (dom.resetFilters) dom.resetFilters.hidden = !hasFilters;
      }
    }

    text($('session-summary'), sorted.length === allSessions.length
      ? `共 ${allSessions.length} 台`
      : `匹配 ${sorted.length} / ${allSessions.length} 台`);
    const rangeStart = sorted.length ? start + 1 : 0;
    const rangeEnd = start + visible.length;
    text(dom.pageSummary, sorted.length ? `${rangeStart}–${rangeEnd}` : '');
    text(dom.page, `${pageIndex + 1} / ${pageCount}`);
    if (dom.previous) dom.previous.disabled = pageIndex === 0;
    if (dom.next) dom.next.disabled = pageIndex >= pageCount - 1;
    updateRelativeTimes();
  }

  function sessionRow(session, retentionSeconds) {
    const tr = element('tr', 'session-row');

    // 列 1：来源与沙箱 ID（带一键复制按钮）
    const tdIdentity = element('td', 'col-identity');
    const idBox = element('div', 'cell-identity');
    const parsed = parseSessionIdentity(session.session_label);

    const avatar = element('span', parsed.isGroup ? 'origin-avatar origin-avatar--group' : 'origin-avatar');
    avatar.appendChild(cloneIcon(parsed.isGroup ? 'tpl-icon-group' : 'tpl-icon-user'));
    idBox.appendChild(avatar);

    const idStack = element('div', 'identity-stack');
    const headline = element('div', 'identity-headline');
    const labelNode = element('span', 'session-label', parsed.title);
    if (parsed.raw) labelNode.setAttribute('title', parsed.raw);
    headline.appendChild(labelNode);
    if (parsed.platform) {
      headline.appendChild(element('span', 'platform-tag', parsed.platform));
    }
    idStack.appendChild(headline);

    const rawSandbox = typeof session.sandbox_id === 'string' ? session.sandbox_id.trim() : '';
    const sandboxText = rawSandbox || (typeof session.sandbox_id === 'string' ? 'ID 待确认' : '—');
    const sandboxLine = element('div', 'sandbox-line');
    const sandboxNode = element('span', 'sandbox-id mono', sandboxText);
    if (rawSandbox) sandboxNode.setAttribute('title', rawSandbox);
    sandboxLine.appendChild(sandboxNode);
    if (rawSandbox) {
      const copyBtn = element('button', 'copy-id-btn');
      copyBtn.type = 'button';
      copyBtn.setAttribute('aria-label', '复制沙箱 ID');
      copyBtn.setAttribute('title', '复制沙箱 ID');
      copyBtn.appendChild(cloneIcon('tpl-icon-copy'));
      copyBtn.addEventListener('click', () => copySandboxId(rawSandbox));
      sandboxLine.appendChild(copyBtn);
    }
    idStack.appendChild(sandboxLine);

    idBox.appendChild(idStack);
    tdIdentity.appendChild(idBox);
    tr.appendChild(tdIdentity);

    // 列 2：运行状态
    const tdStatus = element('td', 'col-status');
    const statusWrap = element('div', 'cell-status');
    const status = typeof session.status === 'string' && hasOwn(STATUS_LABELS, session.status)
      ? session.status : 'unknown';
    const chipClass = status === 'running' ? 'chip chip--running'
      : status === 'paused' ? 'chip chip--paused'
        : status === 'starting' ? 'chip chip--primary'
          : 'chip chip--neutral';
    const badge = element('span', chipClass);
    badge.appendChild(element('i', `dot dot--${status}`));
    badge.appendChild(element('span', '', STATUS_LABELS[status]));
    statusWrap.appendChild(badge);
    if (session.busy === true) {
      statusWrap.appendChild(element('span', 'chip chip--primary', '执行中'));
    }
    if (session.owner_known === false) {
      statusWrap.appendChild(element('span', 'chip chip--danger', 'Key 缺失'));
    }
    tdStatus.appendChild(statusWrap);
    tr.appendChild(tdStatus);

    // 列 3：绑定 Key 与模板
    const tdKey = element('td', 'col-key');
    const keyStack = element('div', 'cell-meta-stack');
    const keyDisplay = formatKeyDisplay(session.key_label, session.key_alias);
    const keyNode = element('span', 'cell-primary', keyDisplay);
    if (keyDisplay !== '—') keyNode.setAttribute('title', keyDisplay);
    keyStack.appendChild(keyNode);
    const rawTemplate = typeof session.template === 'string' ? session.template.trim() : '';
    const tplText = rawTemplate || '默认环境';
    const tplNode = element('span', 'cell-secondary mono', tplText);
    if (rawTemplate) tplNode.setAttribute('title', rawTemplate);
    keyStack.appendChild(tplNode);
    tdKey.appendChild(keyStack);
    tr.appendChild(tdKey);

    // 列 4：活跃度与休眠寿命
    const nowMs = Date.now();
    const tdTime = element('td', 'col-time');
    const timeStack = element('div', 'cell-meta-stack');
    const activeRel = element('span', 'cell-primary mono', formatRelativeTime(session.last_active, nowMs));
    const activeExact = formatLocalTime(session.last_active);
    if (activeExact !== '—') activeRel.setAttribute('title', `最后活跃 ${activeExact}`);
    relativeTimeNodes.push({node: activeRel, ts: session.last_active});
    timeStack.appendChild(activeRel);

    const createdCompact = formatCompactTime(session.created_at, nowMs);
    const createdExact = formatLocalTime(session.created_at);
    const createdNode = element('span', 'cell-secondary mono', `创建 ${createdCompact}`);
    if (createdExact !== '—') createdNode.setAttribute('title', `创建于 ${createdExact}`);
    timeStack.appendChild(createdNode);

    if (status === 'paused' && isFiniteNum(session.paused_at) && session.paused_at > 0 && retentionSeconds > 0) {
      const lifeBox = element('div', 'lifespan-box');
      const lifeLabel = element('span', 'lifespan-label mono');
      const lifeTrack = element('div', 'lifespan-track');
      const lifeFill = element('div', 'lifespan-fill');
      lifeTrack.appendChild(lifeFill);
      lifeBox.appendChild(lifeLabel);
      lifeBox.appendChild(lifeTrack);
      const pausedExact = formatLocalTime(session.paused_at);
      if (pausedExact !== '—') lifeBox.setAttribute('title', `休眠于 ${pausedExact}`);
      lifespanNodes.push({
        label: lifeLabel,
        fill: lifeFill,
        pausedAt: session.paused_at,
        retentionSeconds,
      });
      timeStack.appendChild(lifeBox);
    }
    tdTime.appendChild(timeStack);
    tr.appendChild(tdTime);

    // 列 5：文件往来（分别校验 pending_files 与 uploaded_files，避免缺失时错误归零或显示 —）
    const tdFiles = element('td', 'col-files');
    const filesWrap = element('div', 'cell-files mono');
    const hasPending = isFiniteNum(session.pending_files);
    const hasUploaded = isFiniteNum(session.uploaded_files);
    const pendingCount = hasPending ? session.pending_files : 0;
    const uploadedCount = hasUploaded ? session.uploaded_files : 0;
    const outCount = isFiniteNum(session.generated_files) ? session.generated_files : 0;

    let inLabel = '↑ 附件 —';
    if (hasPending || hasUploaded) {
      inLabel = pendingCount > 0 ? `↑ 入箱 ${uploadedCount} (+${pendingCount})` : `↑ 入箱 ${uploadedCount}`;
    }
    const inPill = element(
      'span',
      (pendingCount + uploadedCount) > 0 ? 'file-pill file-pill--in-active' : 'file-pill',
      inLabel,
    );
    inPill.setAttribute('title', `已入箱 ${displayNumber(session.uploaded_files)} · 待传 ${displayNumber(session.pending_files)}`);

    const outPill = element(
      'span',
      outCount > 0 ? 'file-pill file-pill--out-active' : 'file-pill',
      `↓ 产物 ${displayNumber(session.generated_files)}`,
    );

    filesWrap.appendChild(inPill);
    filesWrap.appendChild(outPill);
    tdFiles.appendChild(filesWrap);
    tr.appendChild(tdFiles);

    // 列 6：休眠与销毁（保持契约结构：actionBox 首子节点为 .action-row，含 [pauseBtn, killBtn]）
    const tdActions = element('td', 'col-actions');
    const actionBox = element('div', 'cell-actions');
    const actionRow = element('div', 'action-row');
    const ownerMissing = session.owner_known === false;
    const canPause = session.can_pause === true && !ownerMissing;
    const canKill = session.can_kill === true && !ownerMissing;

    const pauseBtn = element('button', 'btn btn--tonal btn--sm', '休眠');
    pauseBtn.type = 'button';
    pauseBtn.disabled = !canPause || actionBusy;
    pauseBtn.addEventListener('click', () => openConfirmation(session, 'pause'));
    actionRow.appendChild(pauseBtn);

    const killBtn = element('button', 'btn btn--tonal-danger btn--sm', '销毁');
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

  function renderKeys(limitMismatch = false) {
    keyCountdowns.length = 0;
    const validKeys = snapshot.keys.filter((key) => key && typeof key === 'object');
    const availableCount = validKeys.filter((key) => key.available !== false && number(key.cooldown_seconds) <= 0).length;
    const cooldownCount = validKeys.filter((key) => isFiniteNum(key.cooldown_seconds) && key.cooldown_seconds > 0).length;

    text($('key-count'), validKeys.length ? `${availableCount} / ${validKeys.length} 可用` : '0 把');
    text($('kpi-keys-available'), displayNumber(availableCount));
    text($('kpi-keys-total'), displayNumber(validKeys.length));

    const kpiStatus = $('kpi-keys-status');
    if (kpiStatus) {
      if (!validKeys.length) {
        kpiStatus.className = 'chip chip--neutral';
        text(kpiStatus, '未配置');
      } else if (cooldownCount > 0) {
        kpiStatus.className = 'chip chip--paused';
        text(kpiStatus, `${cooldownCount} 冷却中`);
      } else {
        kpiStatus.className = 'chip chip--running';
        text(kpiStatus, '全部可用');
      }
    }
    if (dom.tabKeysDot) {
      dom.tabKeysDot.hidden = cooldownCount === 0 && !limitMismatch;
    }

    const activeLabels = new Set();
    const fragment = document.createDocumentFragment();
    validKeys.forEach((key) => {
      const label = safeLabel(key.label);
      activeLabels.add(label);
      const validCooldown = isFiniteNum(key.cooldown_seconds);
      const initialRemaining = number(key.cooldown_seconds);

      // 记忆本次冷却周期峰值，避免每次 10 秒刷新将进度条重置回 100%
      if (validCooldown && initialRemaining > 0) {
        const defaultPeak = initialRemaining > 60 ? 300 : 60;
        const prevPeak = keyCooldownPeaks[label] || 0;
        keyCooldownPeaks[label] = Math.max(prevPeak, defaultPeak, initialRemaining);
      } else {
        delete keyCooldownPeaks[label];
      }

      const item = element('div', initialRemaining > 0 ? 'key-item key-item--cooldown' : 'key-item key-item--ready');

      const top = element('div', 'key-item-top');
      const titleWrap = element('div', 'key-title-wrap');
      const iconWrap = element('span', 'key-icon');
      iconWrap.appendChild(cloneIcon('tpl-icon-key'));
      titleWrap.appendChild(iconWrap);
      titleWrap.appendChild(element('span', 'key-name', label));
      if (typeof key.alias === 'string' && key.alias.trim()) {
        const aliasBadge = element('span', 'key-alias', key.alias.trim());
        aliasBadge.setAttribute('title', key.alias.trim());
        titleWrap.appendChild(aliasBadge);
      }
      top.appendChild(titleWrap);

      const stateBadge = element('span', 'chip chip--neutral');
      top.appendChild(stateBadge);
      item.appendChild(top);

      const bottom = element('div', 'key-item-bottom');
      bottom.appendChild(element('span', '', `绑定 ${displayNumber(key.bound_sessions)} 台`));
      const countdown = element('span', 'mono');
      bottom.appendChild(countdown);
      item.appendChild(bottom);

      let cooldownFill = null;
      if (validCooldown && initialRemaining > 0) {
        const bar = element('div', 'key-cooldown-bar');
        cooldownFill = element('div', 'key-cooldown-fill');
        bar.appendChild(cooldownFill);
        item.appendChild(bar);
      }

      keyCountdowns.push({
        card: item,
        badge: stateBadge,
        countdown,
        fill: cooldownFill,
        valid: validCooldown,
        initialTotal: keyCooldownPeaks[label] || Math.max(60, initialRemaining),
        remaining: initialRemaining,
        availableFlag: key.available !== false,
      });
      fragment.appendChild(item);
    });

    Object.keys(keyCooldownPeaks).forEach((k) => {
      if (!activeLabels.has(k)) delete keyCooldownPeaks[k];
    });

    if (!validKeys.length) {
      fragment.appendChild(element('p', 'muted small', '未配置 API Key'));
    }
    if (dom.keys) dom.keys.replaceChildren(fragment);
    updateDynamicTicks();
  }

  function updateRelativeTimes() {
    const nowMs = Date.now();
    relativeTimeNodes.forEach((item) => {
      text(item.node, formatRelativeTime(item.ts, nowMs));
    });
    lifespanNodes.forEach((item) => {
      const elapsed = Math.max(0, (nowMs / 1000) - item.pausedAt);
      const remainSec = Math.max(0, Math.ceil(item.retentionSeconds - elapsed));
      const ratio = Math.max(0, Math.min(100, Math.round((remainSec / item.retentionSeconds) * 100)));
      if (item.fill && item.fill.style) {
        item.fill.style.width = `${ratio}%`;
      }
      if (remainSec <= 0) {
        item.label.className = 'lifespan-label lifespan-label--warn mono';
        item.fill.className = 'lifespan-fill lifespan-fill--warn';
        text(item.label, '待回收');
      } else if (remainSec < 3600) {
        item.label.className = 'lifespan-label lifespan-label--warn mono';
        item.fill.className = 'lifespan-fill lifespan-fill--warn';
        text(item.label, `剩 ${formatDuration(remainSec)}`);
      } else {
        item.label.className = 'lifespan-label mono';
        item.fill.className = 'lifespan-fill';
        text(item.label, `剩 ${formatDuration(remainSec)}`);
      }
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
        item.badge.className = 'chip chip--neutral';
        text(item.badge, item.availableFlag ? '未知' : '不可用');
        item.countdown.className = 'mono key-unavailable';
        text(item.countdown, '—');
        return;
      }
      const remaining = Math.max(0, Math.ceil(item.remaining - elapsedSec));
      if (remaining > 0) {
        item.card.className = 'key-item key-item--cooldown';
        item.badge.className = 'chip chip--paused';
        text(item.badge, '冷却中');
        item.countdown.className = 'mono key-cooldown';
        const mm = String(Math.floor(remaining / 60)).padStart(2, '0');
        const ss = String(remaining % 60).padStart(2, '0');
        text(item.countdown, `剩余 ${mm}:${ss}`);
        if (item.fill && item.fill.style) {
          const pct = Math.max(0, Math.min(100, Math.round((remaining / item.initialTotal) * 100)));
          item.fill.style.width = `${pct}%`;
        }
      } else if (item.remaining > 0) {
        item.card.className = 'key-item';
        item.badge.className = 'chip chip--neutral';
        text(item.badge, '待刷新');
        item.countdown.className = 'mono';
        text(item.countdown, '冷却结束');
        if (item.fill && item.fill.style) item.fill.style.width = '0%';
      } else if (!item.availableFlag) {
        item.card.className = 'key-item';
        item.badge.className = 'chip chip--neutral';
        text(item.badge, '不可用');
        item.countdown.className = 'mono key-unavailable';
        text(item.countdown, '不可分配');
      } else {
        item.card.className = 'key-item key-item--ready';
        item.badge.className = 'chip chip--running';
        text(item.badge, '可轮询');
        item.countdown.className = 'mono key-ready';
        text(item.countdown, '正常');
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
      setNotice('当前浏览器不支持原生确认框，已禁用管理操作。', 'error', false);
      invalidateActions();
      return;
    }

    hideToast();
    stopPolling();
    cancelRead();
    lastFocusedElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    pendingTarget = Object.freeze({session: {...session}, action});

    const parsed = parseSessionIdentity(session.session_label);
    const displayOrigin = parsed.platform ? `${parsed.title}（${parsed.platform}）` : parsed.title;

    text($('confirm-title'), action === 'kill' ? '永久销毁此云沙箱？' : '休眠此云沙箱？');
    text($('confirm-description'), action === 'kill'
      ? '销毁后无法恢复，沙箱内变量与未发回文件将被清除。'
      : '暂停后立即停止计费并释放运行槽位，状态与文件保留至休眠到期。');
    text($('confirm-session'), displayOrigin);
    text($('confirm-sandbox'), safeLabel(session.sandbox_id));
    text($('confirm-key'), formatKeyDisplay(session.key_label, session.key_alias));

    const pauseLabel = $('pause-ack-label');
    const killLabel = $('kill-input-label');
    if (pauseLabel) pauseLabel.hidden = action !== 'pause';
    if (killLabel) killLabel.hidden = action !== 'kill';

    if (dom.pauseAck) dom.pauseAck.checked = false;
    if (dom.killId) dom.killId.value = '';
    if (dom.feedback) dom.feedback.textContent = '';

    dom.confirm.className = action === 'kill' ? 'btn btn--flat-danger btn--pill' : 'btn btn--flat btn--pill';
    text(dom.confirm, action === 'kill' ? '永久销毁' : '确认暂停');
    dom.confirm.disabled = true;
    dom.cancel.disabled = false;
    updateConfirmation();

    dom.dialog.showModal();
    updatePollLabel();
    if (action === 'kill') {
      if (dom.killId) dom.killId.focus();
      else if (dom.cancel) dom.cancel.focus();
    } else if (dom.pauseAck) {
      dom.pauseAck.focus();
    }
  }

  function updateConfirmation() {
    if (!pendingTarget || actionBusy || !dom.confirm) return;
    if (pendingTarget.action === 'kill') {
      const expected = pendingTarget.session.sandbox_id;
      const typed = dom.killId ? dom.killId.value : '';
      dom.confirm.disabled = dom.killId.value !== pendingTarget.session.sandbox_id;
      if (dom.killMatchHint) {
        if (!typed) {
          dom.killMatchHint.className = 'kill-match-hint mono';
          text(dom.killMatchHint, '');
        } else if (typed === expected) {
          dom.killMatchHint.className = 'kill-match-hint kill-match-hint--ok mono';
          text(dom.killMatchHint, '✓ ID 一致');
        } else if (expected.startsWith(typed)) {
          dom.killMatchHint.className = 'kill-match-hint mono';
          text(dom.killMatchHint, `${typed.length} / ${expected.length}`);
        } else {
          dom.killMatchHint.className = 'kill-match-hint kill-match-hint--mismatch mono';
          text(dom.killMatchHint, '✕ ID 不一致');
        }
      }
    } else {
      dom.confirm.disabled = !dom.pauseAck.checked;
    }
  }

  function closeConfirmation() {
    if (actionBusy) return;
    pendingTarget = null;
    if (dom.dialog && dom.dialog.open && typeof dom.dialog.close === 'function') {
      dom.dialog.close();
      dom.dialog.returnValue = '';
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
    if (dom.fillKillId) dom.fillKillId.disabled = true;
    if (dom.pauseAck) dom.pauseAck.disabled = true;

    text(dom.confirm, '执行中…');
    text(dom.feedback, '正在提交，请勿重复操作…');
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
        ? '云沙箱已销毁'
        : result.outcome === 'paused'
          ? '云沙箱已休眠'
          : '目标已不存在，记录已清理';
    } catch (error) {
      message = safeFailure(error);
    } finally {
      actionBusy = false;
      pendingTarget = null;
      if (dom.killId) dom.killId.disabled = false;
      if (dom.fillKillId) dom.fillKillId.disabled = false;
      if (dom.pauseAck) dom.pauseAck.disabled = false;
      dom.cancel.disabled = false;
      if (dom.refresh) dom.refresh.disabled = false;
      text(dom.confirm, action === 'kill' ? '永久销毁' : '确认暂停');
      if (dom.dialog && dom.dialog.open && typeof dom.dialog.close === 'function') {
        dom.dialog.close();
        dom.dialog.returnValue = '';
      }
      if (!disposed) {
        invalidateActions();
        restoreFocus();
        if (success) {
          showToast(message, 'success');
        } else {
          setNotice(message, 'error', true);
        }
        if (!document.hidden) {
          await refresh(true, false);
        }
        schedulePoll();
      }
    }
  }

  async function initialize() {
    applyThemeState();
    const sdk = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!sdk || typeof sdk.ready !== 'function' || !window.E2BPanelBridge || window.parent === window) {
      setNotice('请从 AstrBot WebUI 插件页面入口打开此控制台。', 'error', false);
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
        schedulePoll();
      } else {
        await refresh(true, false);
      }
    } catch (error) {
      if (disposed) return;
      const detail = window.E2BPanelBridge && error instanceof window.E2BPanelBridge.PanelClientError
        ? error.message
        : '无法连接宿主桥接，请登录 WebUI 后重新打开。';
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
    hideToast();
    window.clearTimeout(searchTimer);
    searchTimer = null;
    window.clearTimeout(readyTimer);
    readyTimer = null;
    if (resizeObserver && typeof resizeObserver.disconnect === 'function') {
      resizeObserver.disconnect();
      resizeObserver = null;
    }
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

  /* 事件绑定 */
  if (dom.refresh) dom.refresh.addEventListener('click', () => { void refresh(true, true); });
  if (dom.retry) dom.retry.addEventListener('click', () => { void refresh(true, true); });
  if (dom.auto) {
    dom.auto.addEventListener('change', () => {
      schedulePoll();
      showToast(dom.auto.checked ? '已开启 10s 自动同步' : '已暂停自动同步');
    });
  }
  if (dom.themeToggle) {
    dom.themeToggle.addEventListener('click', () => {
      const idx = THEME_MODES.indexOf(themeMode);
      const next = THEME_MODES[(idx + 1) % THEME_MODES.length];
      setThemeMode(next);
    });
  }
  if (dom.viewTableBtn) dom.viewTableBtn.addEventListener('click', () => setViewMode('table'));
  if (dom.viewCardBtn) dom.viewCardBtn.addEventListener('click', () => setViewMode('cards'));

  if (dom.tabSessions) {
    dom.tabSessions.addEventListener('click', () => switchTab('sessions'));
    dom.tabSessions.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'End') { e.preventDefault(); switchTab('keys', true); }
    });
  }
  if (dom.tabKeys) {
    dom.tabKeys.addEventListener('click', () => switchTab('keys'));
    dom.tabKeys.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft' || e.key === 'Home') { e.preventDefault(); switchTab('sessions', true); }
    });
  }

  if (dom.kpiRunningCard) {
    dom.kpiRunningCard.addEventListener('click', () => {
      switchTab('sessions');
      setStatusFilter('running');
    });
  }
  if (dom.kpiPausedCard) {
    dom.kpiPausedCard.addEventListener('click', () => {
      switchTab('sessions');
      setStatusFilter('paused');
    });
  }
  if (dom.kpiKeysCard) {
    dom.kpiKeysCard.addEventListener('click', () => switchTab('keys'));
  }

  if (dom.statusPills) {
    dom.statusPills.addEventListener('click', (event) => {
      const target = event.target && typeof event.target.closest === 'function'
        ? event.target.closest('.filter-pill') : null;
      if (!target) return;
      const filterVal = target.getAttribute('data-filter');
      if (filterVal) setStatusFilter(filterVal);
    });
  }

  if (dom.search) {
    dom.search.addEventListener('input', () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => { pageIndex = 0; renderSessions(); }, 180);
    });
    dom.search.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (isImeComposing(event)) return;
        window.clearTimeout(searchTimer);
        pageIndex = 0;
        renderSessions();
      }
    });
  }
  if (dom.filter) {
    dom.filter.addEventListener('change', () => {
      syncStatusPills(dom.filter.value);
      pageIndex = 0;
      renderSessions();
    });
  }
  if (dom.sort) dom.sort.addEventListener('change', () => { pageIndex = 0; renderSessions(); });
  if (dom.pageSize) dom.pageSize.addEventListener('change', () => { pageIndex = 0; renderSessions(); });
  if (dom.resetFilters) {
    dom.resetFilters.addEventListener('click', () => {
      if (dom.search) dom.search.value = '';
      setStatusFilter('all');
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
  }
  if (dom.fillKillId) {
    dom.fillKillId.addEventListener('click', () => {
      if (!pendingTarget || pendingTarget.action !== 'kill' || actionBusy || !dom.killId) return;
      dom.killId.value = pendingTarget.session.sandbox_id;
      updateConfirmation();
      dom.killId.focus();
    });
  }
  if (dom.killId) {
    dom.killId.addEventListener('input', updateConfirmation);
    dom.killId.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        if (isImeComposing(event)) return;
        if (pendingTarget && pendingTarget.action === 'kill' && !dom.confirm.disabled && !actionBusy) {
          void performAction();
        }
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
  if (dom.toastClose) {
    dom.toastClose.addEventListener('click', hideToast);
  }

  if (typeof window.ResizeObserver === 'function' && dom.sessionsRegion) {
    resizeObserver = new window.ResizeObserver((entries) => {
      if (!entries || !entries[0] || !entries[0].contentRect) return;
      syncCompactSwitcher(entries[0].contentRect.width);
    });
    resizeObserver.observe(dom.sessionsRegion);
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
      void refresh(false, false);
    } else {
      schedulePoll();
    }
  });

  window.addEventListener('pagehide', destroy, {once: true});
  void initialize();
})();
