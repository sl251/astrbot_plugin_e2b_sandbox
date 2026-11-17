/* E2B 云沙箱可视化控制台交互逻辑。所有服务端返回文本统一通过 textContent 写入以防御 XSS。 */
(function startE2BPanel() {
  'use strict';
  const POLL_MS = 10000;
  const TICK_MS = 1000;
  const FAILURE_LIMIT = 3;
  const BRIDGE_READY_MS = 8000;
  const VALID_PAGE_SIZES = Object.freeze([10, 20, 50]);
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
    busy: '会话正在执行或收尾中，操作不会排队。',
    owner_missing: '原所属 Key 未记录或已移除，禁止跨 Key 操作。',
    identity_unconfirmed: '尚未确认目标沙箱 ID，暂不支持管理操作。',
    stopping: '插件正在停止，暂不接受管理操作。',
    session_busy: '该会话正在执行或收尾，操作未排队。',
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
    scopeBadge: $('scope-badge'),
    noticeBar: $('notice-bar'),
    notice: $('notice'),
    version: $('plugin-version'),
    uptime: $('plugin-uptime'),
    lastUpdated: $('last-updated'),
    slotMatrix: $('slot-matrix'),
    sessionsRegion: $('sessions-region'),
    viewTableBtn: $('view-table-btn'),
    viewCardBtn: $('view-card-btn'),
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
    confirmEyebrow: $('confirm-eyebrow'),
    confirm: $('confirm-action'),
    cancel: $('cancel-action'),
    pauseAck: $('pause-ack'),
    killId: $('kill-id'),
    fillKillId: $('fill-kill-id'),
    killMatchHint: $('kill-match-hint'),
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
  let viewMode = 'table';
  let pendingTarget = null;
  let actionBusy = false;
  let isLoading = false;
  let bridgeUnavailable = false;
  let disposed = false;

  const keyCountdowns = [];
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

  /* 将生硬的 AstrBot 会话标识（如 aiocqhttp:GroupMessage:12345#user:888）解析为人类易读标题与渠道标签 */
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
    if (diffSec < 5) return '刚刚活跃';
    if (diffSec < 60) return `${diffSec} 秒前活跃`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前活跃`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前活跃`;
    return `${Math.floor(diffSec / 86400)} 天前活跃`;
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
    text(dom.refreshLabel || dom.refresh, isLoading ? '同步中…' : '刷新状态');
    updatePollLabel();
  }

  function stopPolling() {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function updatePollLabel() {
    let label = '实时同步中';
    let pillClass = 'poll-pill poll-pill--active';
    let detail = '每 10 秒同步本地状态快照';
    if (bridgeUnavailable) {
      label = '桥接不可用';
      pillClass = 'poll-pill poll-pill--error';
      detail = '未连接到宿主页面桥接';
    } else if (!client) {
      label = '等待宿主桥接';
      pillClass = 'poll-pill';
      detail = '正在等待宿主页面就绪';
    } else if (isLoading) {
      label = '读取中…';
      pillClass = 'poll-pill poll-pill--active';
      detail = '正在读取本地追踪快照';
    } else if (document.hidden) {
      label = '后台静默';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '页面在后台，已暂停轮询';
    } else if (actionBusy) {
      label = '执行管控中';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '正在执行管理操作，已暂停轮询';
    } else if (dom.dialog && dom.dialog.open) {
      label = '等待确认';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '等待二次确认，已暂停轮询';
    } else if (dom.auto && !dom.auto.checked) {
      label = '手动刷新模式';
      pillClass = 'poll-pill poll-pill--paused';
      detail = '已关闭自动同步';
    } else if (failures >= FAILURE_LIMIT) {
      label = '同步中断';
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
    setNotice('正在读取本地追踪状态（不连接或唤醒云沙箱，零额外计费）…', '', false);
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
      setNotice('状态已更新。休眠中的沙箱不产生云端计费；管理操作均需二次确认。', 'success', false);
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
    text(dom.lastUpdated, formatLocalTime(snapshot.generated_at, '—'));

    const counts = snapshot.counts;
    const cap = snapshot.capacity;

    if (dom.scopeBadge) {
      const isolated = cap.sandbox_scope === 'group_member';
      dom.scopeBadge.className = isolated ? 'scope-pill scope-pill--isolated' : 'scope-pill';
      text(dom.scopeBadge, isolated ? '群聊按成员独立隔离' : '群聊共享协作模式');
    }

    // 更新生命周期流转全景条实时数字与阈值说明
    text($('flow-queue-badge'), `${displayNumber(cap.waiting)} 排队`);
    text($('flow-running-badge'), `${displayNumber(counts.running)} 运行`);
    text($('flow-paused-badge'), `${displayNumber(counts.paused)} 休眠`);
    if (isFiniteNum(cap.idle_pause_seconds)) {
      text($('flow-idle-desc'), `执行完默认立即休眠；保持运行空闲满 ${cap.idle_pause_seconds} 秒自动休眠`);
    }
    if (isFiniteNum(cap.paused_retention_hours)) {
      text($('flow-retention-desc'), `休眠满 ${cap.paused_retention_hours} 小时后台自动销毁释放，重要产物请及时发回`);
    }

    setKpiValue('count-running', displayNumber(counts.running));
    const busyCount = isFiniteNum(counts.busy) && counts.busy > 0 ? counts.busy : 0;
    text($('running-sub'), busyCount > 0 ? `正在执行代码 · ${busyCount} 台忙碌中` : '占用并发运行槽位');
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
    text($('capacity-brief'), `空闲 ${displayNumber(cap.available)} · 排队 ${displayNumber(cap.waiting)}`);
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
    const parsed = parseSessionIdentity(labelText);
    const sandboxText = typeof session.sandbox_id === 'string' ? session.sandbox_id : '';
    const aliasText = typeof session.key_alias === 'string' ? session.key_alias : '';
    const haystack = `${labelText}\n${parsed.title}\n${parsed.platform}\n${sandboxText}\n${aliasText}`.toLocaleLowerCase('zh-CN');
    if (query && !haystack.includes(query)) return false;
    if (filter === 'busy') return session.busy === true;
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
        text(dom.emptyTitle, hasFilters ? '没有符合筛选条件的沙箱会话' : '还没有活跃或休眠的云沙箱');
        text(dom.emptyDesc, hasFilters
          ? '可以尝试清空搜索词，或将状态筛选切回「全部状态」。'
          : '当用户在对话中让机器人执行 Python 计算、处理表格或绘制图表时，云沙箱会自动出现在这里。');
        if (dom.resetFilters) dom.resetFilters.hidden = !hasFilters;
      }
    }

    text($('session-summary'), `匹配 ${sorted.length} / 共 ${allSessions.length} 台`);
    const rangeStart = sorted.length ? start + 1 : 0;
    const rangeEnd = start + visible.length;
    text(dom.pageSummary, sorted.length ? `当前展示第 ${rangeStart}–${rangeEnd} 台（共 ${sorted.length} 台）` : '共 0 台');
    text(dom.page, `${pageIndex + 1} / ${pageCount}`);
    if (dom.previous) dom.previous.disabled = pageIndex === 0;
    if (dom.next) dom.next.disabled = pageIndex >= pageCount - 1;
    updateRelativeTimes();
  }

  function sessionRow(session, retentionSeconds) {
    const tr = element('tr', 'session-row');

    // 第 1 列：形象化对话来源（群聊/私聊图标 + 渠道徽章 + 易读名称）与沙箱 ID
    const tdIdentity = element('td', 'col-identity');
    const idBox = element('div', 'cell-identity');
    const parsed = parseSessionIdentity(session.session_label);

    const avatar = element('span', parsed.isGroup ? 'origin-avatar origin-avatar--group' : 'origin-avatar');
    avatar.appendChild(cloneIcon(parsed.isGroup ? 'tpl-icon-group' : 'tpl-icon-user'));
    idBox.appendChild(avatar);

    const idStack = element('div', 'identity-stack');
    const headline = element('div', 'identity-headline');
    const labelNode = element('span', 'session-label', parsed.title);
    if (parsed.raw) labelNode.setAttribute('title', `原始会话标识: ${parsed.raw}`);
    headline.appendChild(labelNode);
    if (parsed.platform) {
      headline.appendChild(element('span', 'platform-tag', parsed.platform));
    }
    idStack.appendChild(headline);

    const rawSandbox = typeof session.sandbox_id === 'string' ? session.sandbox_id.trim() : '';
    const sandboxText = rawSandbox || (typeof session.sandbox_id === 'string' ? '云端 ID 待确认' : '—');
    const sandboxLine = element('div', 'sandbox-line');
    sandboxLine.appendChild(cloneIcon('tpl-icon-vm'));
    const sandboxNode = element('span', 'sandbox-id mono', sandboxText);
    if (rawSandbox) sandboxNode.setAttribute('title', `云沙箱 ID: ${rawSandbox}`);
    sandboxLine.appendChild(sandboxNode);
    idStack.appendChild(sandboxLine);

    idBox.appendChild(idStack);
    tdIdentity.appendChild(idBox);
    tr.appendChild(tdIdentity);

    // 第 2 列：状态胶囊
    const tdStatus = element('td', 'col-status');
    const statusWrap = element('div', 'cell-status');
    const status = typeof session.status === 'string' && hasOwn(STATUS_LABELS, session.status)
      ? session.status : 'unknown';
    const badge = element('span', `badge badge--${status}`);
    badge.appendChild(element('i', `dot dot--${status}`));
    badge.appendChild(element('span', '', STATUS_LABELS[status]));
    statusWrap.appendChild(badge);
    if (session.busy === true) {
      statusWrap.appendChild(element('span', 'badge badge--busy', '执行中'));
    }
    if (session.owner_known === false) {
      statusWrap.appendChild(element('span', 'badge badge--danger', 'Key 缺失'));
    }
    tdStatus.appendChild(statusWrap);
    tr.appendChild(tdStatus);

    // 第 3 列：绑定 API Key（含备注名）与环境模板
    const tdKey = element('td', 'col-key');
    const keyStack = element('div', 'cell-meta-stack');
    const keyDisplay = formatKeyDisplay(session.key_label, session.key_alias);
    const keyNode = element('span', 'cell-primary cell-key-name', keyDisplay);
    if (keyDisplay !== '—') keyNode.setAttribute('title', keyDisplay);
    keyStack.appendChild(keyNode);
    const rawTemplate = typeof session.template === 'string' ? session.template.trim() : '';
    const tplText = rawTemplate ? `模板 ${rawTemplate}` : '官方默认环境';
    const tplNode = element('span', 'cell-secondary cell-template mono', tplText);
    if (rawTemplate) tplNode.setAttribute('title', `自定义模板 ID: ${rawTemplate}`);
    keyStack.appendChild(tplNode);
    tdKey.appendChild(keyStack);
    tr.appendChild(tdKey);

    // 第 4 列：活跃度、创建时间与动态保留寿命进度条
    const nowMs = Date.now();
    const tdTime = element('td', 'col-time');
    const timeStack = element('div', 'cell-meta-stack');
    const activeRel = element('span', 'cell-primary cell-time-primary mono', formatRelativeTime(session.last_active, nowMs));
    const activeExact = formatLocalTime(session.last_active);
    if (activeExact !== '—') activeRel.setAttribute('title', `最后活动时间: ${activeExact}`);
    relativeTimeNodes.push({node: activeRel, ts: session.last_active});
    timeStack.appendChild(activeRel);

    const createdCompact = formatCompactTime(session.created_at, nowMs);
    const createdExact = formatLocalTime(session.created_at);
    const createdNode = element('span', 'cell-secondary cell-time-secondary mono', `创建于 ${createdCompact}`);
    if (createdExact !== '—') createdNode.setAttribute('title', `创建时间: ${createdExact}`);
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
      if (pausedExact !== '—') lifeBox.setAttribute('title', `休眠开始于: ${pausedExact}`);
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

    // 第 5 列：语义化传入附件 / 产出文件胶囊
    const tdFiles = element('td', 'col-files');
    const filesWrap = element('div', 'cell-files mono');
    const inCount = (isFiniteNum(session.pending_files) ? session.pending_files : 0)
      + (isFiniteNum(session.uploaded_files) ? session.uploaded_files : 0);
    const outCount = isFiniteNum(session.generated_files) ? session.generated_files : 0;

    const inPill = element(
      'span',
      inCount > 0 ? 'file-pill file-pill--in-active' : 'file-pill',
      `↑ 附件 ${isFiniteNum(session.pending_files) ? inCount : '—'}`
    );
    inPill.setAttribute('title', `待传缓存附件 ${displayNumber(session.pending_files)} · 已入沙箱文件 ${displayNumber(session.uploaded_files)}`);

    const outPill = element(
      'span',
      outCount > 0 ? 'file-pill file-pill--out-active' : 'file-pill',
      `↓ 产物 ${displayNumber(session.generated_files)}`
    );
    outPill.setAttribute('title', '最近一次代码执行生成并缓存在本地可供发送的文件数');

    filesWrap.appendChild(inPill);
    filesWrap.appendChild(outPill);
    tdFiles.appendChild(filesWrap);
    tr.appendChild(tdFiles);

    // 第 6 列：暂停与销毁管理按钮（保持契约结构：actionBox 首个子元素为 .action-row）
    const tdActions = element('td', 'col-actions');
    const actionBox = element('div', 'cell-actions');
    const actionRow = element('div', 'action-row');
    const ownerMissing = session.owner_known === false;
    const canPause = session.can_pause === true && !ownerMissing;
    const canKill = session.can_kill === true && !ownerMissing;

    const pauseBtn = element('button', 'btn btn--ghost btn--sm', '休眠');
    pauseBtn.type = 'button';
    pauseBtn.disabled = !canPause || actionBusy;
    pauseBtn.setAttribute('title', '暂停此云沙箱以释放并发槽位并停止计费');
    pauseBtn.addEventListener('click', () => openConfirmation(session, 'pause'));
    actionRow.appendChild(pauseBtn);

    const killBtn = element('button', 'btn btn--danger-ghost btn--sm', '销毁');
    killBtn.type = 'button';
    killBtn.disabled = !canKill || actionBusy;
    killBtn.setAttribute('title', '永久销毁此云沙箱并清除其变量与未保存文件');
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
    text($('key-count'), validKeys.length ? `${availableCount} / ${validKeys.length} 可轮询` : '0 把');

    const fragment = document.createDocumentFragment();
    validKeys.forEach((key) => {
      const validCooldown = isFiniteNum(key.cooldown_seconds);
      const initialRemaining = number(key.cooldown_seconds);
      const item = element('div', initialRemaining > 0 ? 'key-item key-item--cooldown' : 'key-item key-item--ready');

      const top = element('div', 'key-item-top');
      const titleWrap = element('div', 'key-title-wrap');
      const iconWrap = element('span', 'key-icon');
      iconWrap.appendChild(cloneIcon('tpl-icon-key'));
      titleWrap.appendChild(iconWrap);
      titleWrap.appendChild(element('span', 'key-name', safeLabel(key.label)));
      if (typeof key.alias === 'string' && key.alias.trim()) {
        const aliasBadge = element('span', 'key-alias', key.alias.trim());
        aliasBadge.setAttribute('title', `配置备注: ${key.alias.trim()}`);
        titleWrap.appendChild(aliasBadge);
      }
      top.appendChild(titleWrap);

      const stateBadge = element('span', 'badge');
      top.appendChild(stateBadge);
      item.appendChild(top);

      const bottom = element('div', 'key-item-bottom');
      bottom.appendChild(element('span', '', `已绑定沙箱 ${displayNumber(key.bound_sessions)} 台`));
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
        initialTotal: Math.max(60, initialRemaining),
        remaining: initialRemaining,
        availableFlag: key.available !== false,
      });
      fragment.appendChild(item);
    });

    if (!validKeys.length) {
      fragment.appendChild(element('p', 'muted small', '尚未配置任何 E2B API Key，请前往插件配置页填写。'));
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
        text(item.label, '已到期 · 等待后台回收');
      } else if (remainSec < 3600) {
        item.label.className = 'lifespan-label lifespan-label--warn mono';
        item.fill.className = 'lifespan-fill lifespan-fill--warn';
        text(item.label, `即将回收 · 剩 ${formatDuration(remainSec)}`);
      } else {
        item.label.className = 'lifespan-label mono';
        item.fill.className = 'lifespan-fill';
        text(item.label, `保留剩余 ${formatDuration(remainSec)}`);
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
        item.badge.className = 'badge badge--unknown';
        text(item.badge, item.availableFlag ? '状态未知' : '不可用');
        item.countdown.className = 'mono key-unavailable';
        text(item.countdown, '—');
        return;
      }
      const remaining = Math.max(0, Math.ceil(item.remaining - elapsedSec));
      if (remaining > 0) {
        item.card.className = 'key-item key-item--cooldown';
        item.badge.className = 'badge badge--paused';
        text(item.badge, '冷却保护中');
        item.countdown.className = 'mono key-cooldown';
        const mm = Math.floor(remaining / 60);
        const ss = String(remaining % 60).padStart(2, '0');
        text(item.countdown, `冷却剩余 ${mm}:${ss}`);
        if (item.fill && item.fill.style) {
          const pct = Math.max(0, Math.min(100, Math.round((remaining / item.initialTotal) * 100)));
          item.fill.style.width = `${pct}%`;
        }
      } else if (item.remaining > 0) {
        item.card.className = 'key-item';
        item.badge.className = 'badge badge--unknown';
        text(item.badge, '待刷新');
        item.countdown.className = 'mono';
        text(item.countdown, '冷却结束 · 刷新确认');
        if (item.fill && item.fill.style) item.fill.style.width = '0%';
      } else if (!item.availableFlag) {
        item.card.className = 'key-item';
        item.badge.className = 'badge badge--unknown';
        text(item.badge, '不可用');
        item.countdown.className = 'mono key-unavailable';
        text(item.countdown, '暂不可分配');
      } else {
        item.card.className = 'key-item key-item--ready';
        item.badge.className = 'badge badge--running';
        text(item.badge, '可轮询');
        item.countdown.className = 'mono key-ready';
        text(item.countdown, '参与新沙箱分配');
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
      setNotice('当前浏览器不支持原生安全确认对话框，管理操作已禁用。', 'error', false);
      invalidateActions();
      return;
    }

    stopPolling();
    cancelRead();
    lastFocusedElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    pendingTarget = Object.freeze({session: {...session}, action});

    const parsed = parseSessionIdentity(session.session_label);
    const displayOrigin = parsed.platform ? `${parsed.title}（${parsed.platform}）` : parsed.title;

    if (dom.confirmEyebrow) {
      dom.confirmEyebrow.className = action === 'kill' ? 'dialog-eyebrow dialog-eyebrow--danger' : 'dialog-eyebrow';
      text(dom.confirmEyebrow, action === 'kill' ? '高危不可逆操作确认' : '实例休眠二次确认');
    }
    text($('confirm-title'), action === 'kill' ? '永久销毁此云沙箱？' : '休眠暂停此云沙箱？');
    text($('confirm-description'), action === 'kill'
      ? '销毁后不可恢复：该云沙箱内的内存变量、进程与未发回聊天窗口的文件将被彻底清除。'
      : '暂停后将立即停止云端计费并释放并发槽位；沙箱内变量与文件会保留至休眠到期。');
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

    dom.confirm.className = action === 'kill' ? 'btn btn--danger' : 'btn btn--primary';
    text(dom.confirm, action === 'kill' ? '确认永久销毁' : '确认暂停沙箱');
    dom.confirm.disabled = true;
    dom.cancel.disabled = false;
    updateConfirmation();

    dom.dialog.showModal();
    updatePollLabel();
    if (action === 'kill' && dom.cancel) {
      // 遵循破坏性弹窗焦点规范：初始焦点落在安全的取消按钮或核对框，防止连按回车误删
      dom.killId ? dom.killId.focus() : dom.cancel.focus();
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
          text(dom.killMatchHint, `需完整匹配 ${expected.length} 个字符`);
        } else if (typed === expected) {
          dom.killMatchHint.className = 'kill-match-hint kill-match-hint--ok mono';
          text(dom.killMatchHint, '✓ 沙箱 ID 核对一致，可执行销毁');
        } else if (expected.startsWith(typed)) {
          dom.killMatchHint.className = 'kill-match-hint mono';
          text(dom.killMatchHint, `已匹配 ${typed.length} / ${expected.length} 个字符…`);
        } else {
          dom.killMatchHint.className = 'kill-match-hint kill-match-hint--mismatch mono';
          text(dom.killMatchHint, '✕ 输入内容与目标沙箱 ID 不一致');
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

    text(dom.confirm, '正在提交…');
    text(dom.feedback, '正在校验目标乐观版本锁并请求宿主执行，请勿重复提交。');
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
        ? '云沙箱已永久销毁。'
        : result.outcome === 'paused'
          ? '云沙箱已休眠暂停。'
          : '云端目标已不存在，本地追踪记录已自动清理。';
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
      text(dom.confirm, action === 'kill' ? '确认永久销毁' : '确认暂停沙箱');
      if (dom.dialog && dom.dialog.open && typeof dom.dialog.close === 'function') {
        dom.dialog.close();
        dom.dialog.returnValue = '';
      }
      if (!disposed) {
        invalidateActions();
        setNotice(`${message} 正在同步最新状态…`, success ? 'success' : 'error', !success);
        restoreFocus();
        if (!document.hidden) {
          const refreshed = await refresh(true);
          if (!disposed && refreshed) {
            setNotice(`${message} 已同步最新状态。`, success ? 'success' : 'error', !success);
          }
        } else {
          setNotice(`${message} 返回前台后将自动刷新。`, success ? 'success' : 'error', !success);
        }
        schedulePoll();
      }
    }
  }

  async function initialize() {
    const sdk = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!sdk || typeof sdk.ready !== 'function' || !window.E2BPanelBridge || window.parent === window) {
      setNotice('当前宿主不支持插件页面桥接，或未从插件详情页入口打开；已安全降级为只读说明视图。', 'error', false);
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
        setNotice('页面当前处于后台，切回前台后将自动读取快照。', '', false);
        schedulePoll();
      } else {
        await refresh(true);
      }
    } catch (error) {
      if (disposed) return;
      const detail = window.E2BPanelBridge && error instanceof window.E2BPanelBridge.PanelClientError
        ? error.message
        : '无法建立受信任的宿主桥接连接。请登录 WebUI 后从插件详情页重新打开。';
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
  if (dom.viewTableBtn) dom.viewTableBtn.addEventListener('click', () => setViewMode('table'));
  if (dom.viewCardBtn) dom.viewCardBtn.addEventListener('click', () => setViewMode('cards'));
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
