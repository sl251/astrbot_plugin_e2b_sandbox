/* 面向 AstrBot 页面 postMessage 桥接的可取消通信客户端。
 * 宿主自带 SDK 的 apiGet/apiPost 不支持取消，且未响应请求会一直滞留在私有 Map 中。
 * 本模块复用宿主标准通信频道与消息动作，维护有界请求表；就绪握手与主题监听仍调用宿主 SDK。
 * 取消请求会立即清理监听器与定时器并忽略迟到响应；不访问凭据、本地存储、外部网络或父窗口 DOM。
 */
(function attachE2BBridgeClient(root) {
  'use strict';
  const CHANNEL = 'astrbot-plugin-page';
  const ENDPOINTS = Object.freeze({snapshot: 'dashboard/snapshot', action: 'dashboard/action'});
  const PUBLIC_CODES = Object.freeze({
    unauthorized: '登录状态已失效，请重新登录 AstrBot WebUI。',
    stopping: '插件正在停止，请稍后刷新。',
    stale_target: '目标沙箱或版本已变化，请刷新后重新确认。',
    target_missing: '目标会话已不存在，请刷新面板。',
    session_busy: '该会话正在执行或清理，操作已拒绝且不会排队。',
    owner_missing: '原所属 Key 未记录或已移除，不能换 Key 操作。',
    ambiguous_target: '目标有重复会话记录，操作已拒绝。',
    invalid_target: '目标标识无效，请刷新后重新确认。',
    confirmation_required: '请明确确认目标沙箱后再操作。',
    unsupported_action: '此面板不支持该操作。',
    pause_unconfirmed: '暂停未确认，容量与会话记录仍保留，请刷新检查。',
    kill_unconfirmed: '销毁未确认，会话记录仍保留，请刷新检查。',
    operation_failed: '云端操作未确认，请刷新检查，不要重复提交。',
    snapshot_failed: '暂时无法读取本地快照，请手动重试。',
    invalid_body: '操作请求格式无效，请刷新面板。',
  });

  // 宿主 PluginViewFrame 对 4xx/5xx 响应会将后端公开文案折叠进 ok:false 的纯字符串 error。
  // 仅精确匹配后端 web_panel.py 预设的常量级公开文案，绝不原样回显任意未知错误字符串，防止泄露路径、令牌或调用栈。
  const EXACT_PUBLIC_MESSAGES = Object.freeze({
    '请先登录 AstrBot WebUI。': 'unauthorized',
    '插件正在停止，请稍后刷新。': 'stopping',
    '暂时无法读取本地快照。': 'snapshot_failed',
    '操作请求必须使用 JSON。': 'invalid_body',
    '操作请求体过大或无效。': 'invalid_body',
    '操作请求格式无效。': 'invalid_body',
    '请明确确认目标沙箱后再操作。': 'confirmation_required',
    '面板仅支持暂停或销毁已有沙箱。': 'unsupported_action',
    '目标版本无效，请刷新面板。': 'invalid_target',
    '目标沙箱 ID 无效。': 'invalid_target',
    '该会话的沙箱或版本已变化，请刷新后重新确认。': 'stale_target',
    '目标会话已变化，请刷新面板。': 'stale_target',
    '目标标识无法安全展示，已拒绝操作。': 'invalid_target',
    '原所属 API Key 未记录或已移除，不能换 Key 操作。': 'owner_missing',
    '目标沙箱存在重复会话记录，已拒绝操作。': 'ambiguous_target',
    '目标会话已不存在，请刷新面板。': 'target_missing',
    '暂停尚未确认；会话记录与容量保留，请刷新后检查。': 'pause_unconfirmed',
    '暂停状态尚未确认，请刷新后检查。': 'pause_unconfirmed',
    '销毁状态尚未确认；记录保留，请刷新后检查。': 'kill_unconfirmed',
    '该会话正在执行或清理，请稍后刷新；操作不会排队。': 'session_busy',
    '该会话正在执行或清理，操作未排队。': 'session_busy',
    '云端操作未确认；请刷新面板检查，勿重复提交。': 'operation_failed',
  });

  const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

  class PanelClientError extends Error {
    constructor(code, message) {
      super(message);
      this.name = 'PanelClientError';
      this.code = code;
    }
  }

  const abortError = () => {
    const error = new Error('本次读取已取消。');
    error.name = 'AbortError';
    return error;
  };

  const makeInstanceId = (cryptoObj) => {
    if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
      return cryptoObj.randomUUID();
    }
    if (cryptoObj && typeof cryptoObj.getRandomValues === 'function') {
      const bytes = new Uint8Array(16);
      cryptoObj.getRandomValues(bytes);
      return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    }
    return `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
  };

  function resolveParentOrigin(windowObject) {
    if (!windowObject || !windowObject.location || !windowObject.URL) {
      throw new PanelClientError('invalid_origin', '无法确认页面来源，已拒绝连接宿主桥接。');
    }
    if (!windowObject.parent || windowObject.parent === windowObject) {
      throw new PanelClientError('invalid_origin', '页面未嵌入在 AstrBot WebUI 中，已拒绝连接。');
    }
    let parsed;
    try {
      parsed = new windowObject.URL(windowObject.location.href);
    } catch {
      throw new PanelClientError('invalid_origin', '页面地址无效，已拒绝连接宿主桥接。');
    }
    if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
        || !parsed.origin || parsed.origin === 'null' || parsed.origin === '*') {
      throw new PanelClientError('invalid_origin', '当前页面 origin 无效（null），已拒绝向未知目标发送消息。');
    }
    const runtimeOrigin = windowObject.location.origin;
    if (typeof runtimeOrigin === 'string' && runtimeOrigin && runtimeOrigin !== 'null'
        && runtimeOrigin !== parsed.origin) {
      throw new PanelClientError('invalid_origin', '当前页面运行 origin 与地址不一致，已拒绝连接宿主桥接。');
    }
    return parsed.origin;
  }

  function mapBridgeFailure(rawError) {
    const text = typeof rawError === 'string'
      ? rawError.trim()
      : (rawError && typeof rawError.message === 'string' ? rawError.message.trim() : '');
    if (text && hasOwn(EXACT_PUBLIC_MESSAGES, text)) {
      const code = EXACT_PUBLIC_MESSAGES[text];
      return new PanelClientError(code, PUBLIC_CODES[code]);
    }
    return new PanelClientError('bridge_error', '请求被拒绝或未完成，请检查登录、目标状态并刷新。');
  }

  function createClient(windowObject, timeoutMs = 30000) {
    const expectedParentOrigin = resolveParentOrigin(windowObject);
    const pending = new Map();
    const instance = makeInstanceId(windowObject.crypto);
    const safeTimeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 30000;
    let bridgeSession = '';
    let sequence = 0;
    let destroyed = false;

    const onMessage = (event) => {
      if (event.source !== windowObject.parent || event.origin !== expectedParentOrigin) return;
      const message = event.data;
      if (!message || typeof message !== 'object' || message.channel !== CHANNEL) return;
      if (typeof message.session === 'string' && message.session && !bridgeSession) {
        bridgeSession = message.session;
      }
      if (message.kind !== 'response') return;
      const entry = pending.get(message.requestId);
      if (!entry) return;
      if (message.ok !== true) {
        entry.finish(mapBridgeFailure(message.error));
        return;
      }
      const payload = message.data;
      if (payload && payload.status === 'error') {
        const code = payload.data && typeof payload.data.code === 'string' ? payload.data.code : '';
        const knownCode = hasOwn(PUBLIC_CODES, code);
        entry.finish(new PanelClientError(knownCode ? code : 'request_failed', knownCode ? PUBLIC_CODES[code] : '请求未完成，请刷新检查。'));
        return;
      }
      entry.finish(null, payload && payload.status === 'ok' ? payload.data : payload);
    };
    windowObject.addEventListener('message', onMessage);

    function request(action, endpoint, body, signal) {
      if (destroyed) return Promise.reject(abortError());
      if (action === 'api:get' && endpoint !== ENDPOINTS.snapshot) {
        return Promise.reject(new PanelClientError('invalid_endpoint', '仅允许读取沙箱快照。'));
      }
      if (action === 'api:post' && endpoint !== ENDPOINTS.action) {
        return Promise.reject(new PanelClientError('invalid_endpoint', '仅允许管理已有会话。'));
      }
      if (action !== 'api:get' && action !== 'api:post') {
        return Promise.reject(new PanelClientError('invalid_endpoint', '不支持此桥接操作。'));
      }
      if (signal && signal.aborted) return Promise.reject(abortError());
      return new Promise((resolve, reject) => {
        sequence += 1;
        const requestId = `e2b_panel_${instance}_${sequence}`;
        let timer;
        const onAbort = () => finish(abortError());
        function finish(error, value) {
          if (!pending.has(requestId)) return;
          pending.delete(requestId);
          windowObject.clearTimeout(timer);
          if (signal) signal.removeEventListener('abort', onAbort);
          if (error) reject(error);
          else resolve(value);
        }
        pending.set(requestId, {finish});
        if (signal) signal.addEventListener('abort', onAbort, {once: true});
        timer = windowObject.setTimeout(() => {
          finish(new PanelClientError('timeout', '宿主桥接响应超时；操作可能仍在执行，请刷新检查。'));
        }, safeTimeoutMs);
        const payload = {channel: CHANNEL, kind: 'request', requestId, action, endpoint};
        if (bridgeSession) payload.session = bridgeSession;
        if (action === 'api:post') payload.body = body;
        else payload.params = {};
        try {
          // 从页面地址解析真实服务源，并确保目标源绝不为 null 或通配符
          windowObject.parent.postMessage(payload, expectedParentOrigin);
        } catch {
          finish(new PanelClientError('bridge_error', '无法连接宿主桥接，请从插件详情页重新打开。'));
        }
      });
    }

    return Object.freeze({
      getSnapshot: (signal) => request('api:get', ENDPOINTS.snapshot, null, signal),
      act: (body) => request('api:post', ENDPOINTS.action, body, null),
      cancelAll: () => Array.from(pending.values()).forEach((entry) => entry.finish(abortError())),
      destroy: () => {
        destroyed = true;
        Array.from(pending.values()).forEach((entry) => entry.finish(abortError()));
        windowObject.removeEventListener('message', onMessage);
      },
      pendingCount: () => pending.size,
    });
  }

  root.E2BPanelBridge = Object.freeze({createClient, PanelClientError});
})(window);
