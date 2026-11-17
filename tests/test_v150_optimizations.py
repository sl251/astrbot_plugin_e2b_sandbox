"""v1.5.0 安全边界、跨消息附件、多图回传、性能优化、群成员隔离与新版可视化面板回归测试。"""

import asyncio
import base64
import json
import subprocess
import shutil
import socket
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from helpers import Event, FakeSDK, FakeSandbox, Query, State, TestDirectory, load_main

main = load_main()
from sandbox_plugin_test import file_transfer as transfer
from sandbox_plugin_test import media_handler
from sandbox_plugin_test.api_keys import ApiKeyPool

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "views" / "dashboard"


class SecurityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """验证宿主机 SSRF 拦截与本机敏感文件读取防护。"""

    def setUp(self):
        self.directory = TestDirectory()
        self.addCleanup(self.directory.cleanup)
        self.context = types.SimpleNamespace(add_llm_tools=lambda *args: None)
        self.data_patch = patch.object(main, "get_astrbot_data_path", lambda: self.directory.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.plugin = main.Main(self.context, {"e2b_api_key": "e2b_test_secret", "enable_user_whitelist": False})
        self.plugin._start_maintenance = lambda: None

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def test_ssrf_blocks_private_loopback_cgnat_metadata_and_credentials(self):
        blocked_urls = [
            "http://127.0.0.1:6185/api/config",
            "http://[::1]:8080/secret",
            "http://[::ffff:127.0.0.1]/admin",
            "http://10.0.0.5/internal",
            "http://172.16.10.2/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.100.100.200/latest/meta-data/",
            "http://localhost:11434/api/tags",
            "http://service.internal/token",
            "http://metadata.google.internal/computeMetadata/v1/",
            "https://user:pass@files.example/report.csv",
        ]
        for url in blocked_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                transfer.validate_http_url(url)

    def test_ssrf_blocks_302_redirect_to_loopback_and_dns_rebinding(self):
        handler = transfer.HTTPOnlyRedirectHandler()
        import urllib.request
        req = urllib.request.Request("https://public.example/file.csv")
        for target in ("http://127.0.0.1/secret", "http://169.254.169.254/meta", "http://192.168.0.1/"):
            with self.subTest(redirect=target), self.assertRaises(ValueError):
                handler.redirect_request(req, None, 302, "Found", {}, target)

        # 模拟恶意域名在连接阶段解析到环回或云元数据 IP（DNS Rebinding 防御）
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
        with patch.object(transfer.socket, "getaddrinfo", return_value=fake_addrinfo):
            with self.assertRaises(ValueError):
                transfer._connect_verified_socket("rebind.attacker.example", 80, 5)

    async def test_local_attachment_blocks_plugin_source_plugin_data_and_host_config(self):
        # 1. 禁止读取插件自身目录源码与配置
        with self.assertRaises(ValueError):
            self.plugin._read_local_file(str(ROOT / "main.py"))

        # 2. 禁止读取插件持久化目录（如 sandbox_sessions.json 或他人导出缓存）
        state_file = self.plugin._plugin_data_dir / "sandbox_sessions.json"
        state_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.plugin._read_local_file(str(state_file))

        # 3. 禁止读取敏感命名文件（如 cmd_config.json / .env）
        sensitive = Path(self.directory.name) / "cmd_config.json"
        sensitive.write_text('{"jwt_secret":"top-secret"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.plugin._read_local_file(str(sensitive))

        # 4. 原始未水合消息段（_untrusted_raw）伪造任意宿主机非缓存目录路径时拒绝读取
        outside_dir = ROOT.parent / f"untrusted_probe_{int(time.time() * 1000)}.txt"
        try:
            outside_dir.write_text("host-secret", encoding="utf-8")
            event = Event("attacker")
            event.message_obj.raw_message = {
                "message": [{"type": "file", "data": {"name": "a.txt", "path": str(outside_dir)}}]
            }
            files = self.plugin._extract_event_files(event)
            self.assertTrue(files[0].get("_untrusted_raw"))
            with self.assertRaises(ValueError):
                await self.plugin._resolve_file_payload(event, files[0])
        finally:
            outside_dir.unlink(missing_ok=True)


class WorkflowAndPerformanceTests(unittest.IsolatedAsyncioTestCase):
    """验证跨消息发附件、多图发送、子目录检测、按需下载、依赖缓存、按需字体与多 Key 并行。"""

    async def asyncSetUp(self):
        FakeSDK.reset()
        self.directory = TestDirectory()
        self.patches = [
            patch.object(main, "get_astrbot_data_path", lambda: self.directory.name),
            patch.object(main, "AsyncSandbox", FakeSDK),
            patch.object(main, "SandboxQuery", Query),
            patch.object(main, "SandboxState", State),
        ]
        for item in self.patches:
            item.start()
        self.context = types.SimpleNamespace(add_llm_tools=lambda *args: None)
        self.plugin = main.Main(self.context, {
            "e2b_api_keys": ["e2b_key_alpha # 主力号", "备用号 # e2b_key_beta"],
            "enable_user_whitelist": False,
        })
        self.plugin._start_maintenance = lambda: None

    async def asyncTearDown(self):
        await self.plugin.terminate()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def test_split_message_upload_then_text_prompt_consumes_once(self):
        # 模拟 QQ / OneBot 用户第 1 条消息仅上传文档（无文字），第 2 条消息发文字分析，第 3 条消息继续追问
        sample = Path(self.directory.name) / "sales.csv"
        sample.write_bytes(b"month,amount\n1,100\n")

        msg1 = Event("qq_user")
        msg1.message_obj.message_id = "m-101"
        comp = media_handler.Comp.File(name="sales.csv", file_id="fid-1")
        comp.get_file = AsyncMock(return_value=str(sample))
        msg1.message_obj.message = [comp]

        await self.plugin.remember_session_files(msg1)
        session_id = self.plugin._get_session_id(msg1)
        # 确保事件令牌未持有 Event 对象强引用
        self.assertEqual(self.plugin._file_event_ids[session_id], ("message", "m-101"))

        # 第 2 条消息只有纯文本提问
        msg2 = Event("qq_user")
        msg2.message_obj.message_id = "m-102"
        req2 = types.SimpleNamespace(
            func_tool=types.SimpleNamespace(
                tools=[types.SimpleNamespace(name="e2b_sandbox_run_python_code", active=True)],
                get_tool=lambda n: types.SimpleNamespace(name=n, active=True) if n == "e2b_sandbox_run_python_code" else None,
            ),
            system_prompt="",
        )
        await self.plugin.inject_file_hint(msg2, req2)
        self.assertIn("/home/user/uploads/sales.csv", req2.system_prompt)

        written = []
        orig_create = FakeSDK.create

        async def capture_create(**kwargs):
            box = await orig_create(**kwargs)
            box.files.write = AsyncMock(side_effect=lambda p, c: written.append((p, c)))
            return box

        with patch.object(FakeSDK, "create", capture_create):
            result = await self.plugin.run_python_code(msg2, "print('analyze')")
        self.assertIn("Uploaded files: /home/user/uploads/sales.csv", result)
        self.assertEqual(written, [("/home/user/uploads/sales.csv", b"month,amount\n1,100\n")])

        # 第 3 条纯文本追问：不再重复上传旧附件，但在提示词中告知沙箱已有该文件
        msg3 = Event("qq_user")
        msg3.message_obj.message_id = "m-103"
        self.assertEqual(self.plugin._get_pending_files(msg3), [])
        req3 = types.SimpleNamespace(func_tool=req2.func_tool, system_prompt="")
        await self.plugin.inject_file_hint(msg3, req3)
        self.assertIn("previously uploaded", req3.system_prompt)
        self.assertIn("/home/user/uploads/sales.csv", req3.system_prompt)

    async def test_empty_pending_files_issues_zero_mkdir_commands(self):
        sandbox = types.SimpleNamespace(commands=types.SimpleNamespace(run=AsyncMock()))
        result = await self.plugin._stage_pending_files(Event(), sandbox, [])
        self.assertEqual(result, [])
        sandbox.commands.run.assert_not_awaited()

    async def test_multiple_generated_images_are_all_delivered(self):
        evt = Event("chart_user")
        png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nmock").decode("ascii")
        execution = types.SimpleNamespace(
            results=[
                types.SimpleNamespace(png=png_b64),
                types.SimpleNamespace(png=png_b64),
                types.SimpleNamespace(png=png_b64),
            ]
        )
        with patch.object(media_handler.Image, "fromFileSystem", side_effect=lambda p: f"img:{Path(p).suffix}", create=True):
            count = await self.plugin._handle_images(evt, execution, [])
        self.assertEqual(count, 3)
        self.assertEqual(len(evt.sent), 3)

    async def test_deep_subdirectory_hint_detected_and_top_candidates_downloaded_lazily(self):
        # 5 个生成文件中仅下载得分最高的前 3 个，且深层子目录打印路径能够通过 stat 正常捕获
        after = {
            f"/home/user/temp_{idx}.txt": {"size": 10, "mtime": 2.0}
            for idx in range(4)
        }
        deep_path = "/home/user/a/b/c/d/final_report.csv"
        self.plugin._snapshot_sandbox_files = AsyncMock(return_value=after)
        read_calls = []

        async def fake_read(path, **kwargs):
            read_calls.append(path)
            return b"0123456789"

        sandbox = types.SimpleNamespace(
            commands=types.SimpleNamespace(run=AsyncMock(return_value=types.SimpleNamespace(stdout="10\n"))),
            files=types.SimpleNamespace(read=AsyncMock(side_effect=fake_read)),
        )
        collected = await self.plugin._collect_generated_files(
            sandbox, [], [f"Saved to {deep_path}"], "sess-lazy", {}
        )
        self.assertEqual(len(collected), 3)
        self.assertEqual(collected[0][2], deep_path)
        # 仅对不在快照中的深层路径调用了 1 次 stat，且总共只下载了前 3 个高分候选文件
        self.assertEqual(sandbox.commands.run.await_count, 1)
        self.assertEqual(len(read_calls), 3)

    async def test_lazy_font_bootstrap_and_per_sandbox_dependency_cache(self):
        # 纯算术代码不注入 matplotlib 引导；绘图代码自动注入；字符串中的 "plt" 不误触发安装
        self.assertEqual(self.plugin._build_execution_code("print(1 + 1)"), "print(1 + 1)")
        self.assertIn("matplotlib", self.plugin._build_execution_code("import matplotlib.pyplot as plt\nplt.plot([1, 2])"))
        self.assertEqual(self.plugin._detect_packages('text = "split_plt_token"'), [])
        self.assertEqual(self.plugin._detect_packages("plt.plot([1, 2])"), ["matplotlib"])

        # 同一沙箱连续两次检查相同依赖包时，第二次直接命中内存缓存不发远端命令
        box = FakeSandbox("sbx-cache-1", {})
        box.commands.run = AsyncMock(return_value=types.SimpleNamespace(exit_code=0, stdout="", stderr=""))
        await self.plugin._install_dependencies(box, ["pandas", "numpy"])
        await self.plugin._install_dependencies(box, ["pandas"])
        self.assertEqual(box.commands.run.await_count, 1)

    async def test_different_api_keys_create_in_parallel_without_blocking_on_slow_rpc(self):
        self.plugin.config["max_running_sandboxes"] = 4
        alpha_started = asyncio.Event()
        release_alpha = asyncio.Event()
        orig_create = FakeSDK.create

        async def slow_alpha_create(**kwargs):
            if kwargs.get("api_key") == "e2b_key_alpha":
                alpha_started.set()
                await release_alpha.wait()
            return await orig_create(**kwargs)

        with patch.object(FakeSDK, "create", slow_alpha_create), \
             patch.object(main.asyncio, "sleep", AsyncMock()):
            t1 = asyncio.create_task(self.plugin.create_session_sandbox(Event("u1")))
            await asyncio.wait_for(alpha_started.wait(), 1.0)
            # 当 Key #1 仍在等待远端慢响应时，Key #2 应能并行完成沙箱创建而不被阻塞
            res2 = await asyncio.wait_for(self.plugin.create_session_sandbox(Event("u2")), 1.0)
            self.assertIn("Created sandbox", res2)
            release_alpha.set()
            res1 = await asyncio.wait_for(t1, 1.0)
            self.assertIn("Created sandbox", res1)

    def test_group_member_sandbox_scope_isolates_group_users_only(self):
        g1, g2 = Event("alice"), Event("bob")
        g1.unified_msg_origin = g2.unified_msg_origin = "aiocqhttp:GroupMessage:99999"
        g1.message_obj.group_id = g2.message_obj.group_id = "99999"

        # 默认 session 模式：群成员共享会话 ID
        self.plugin.config["sandbox_scope"] = "session"
        self.assertEqual(self.plugin._get_session_id(g1), "aiocqhttp:GroupMessage:99999")
        self.assertEqual(self.plugin._get_session_id(g1), self.plugin._get_session_id(g2))

        # group_member 模式：群成员各自隔离，私聊不受影响
        self.plugin.config["sandbox_scope"] = "group_member"
        self.assertEqual(self.plugin._get_session_id(g1), "aiocqhttp:GroupMessage:99999#user:alice")
        self.assertEqual(self.plugin._get_session_id(g2), "aiocqhttp:GroupMessage:99999#user:bob")
        dm = Event("alice")
        self.assertEqual(self.plugin._get_session_id(dm), "test:friend:alice")


class ConfigAndPanelEnhancementTests(unittest.TestCase):
    """验证多 Key 别名脱敏、双配置文件一致性及新版前端输入法保护与可读性解析。"""

    def test_key_alias_parsing_and_secret_redaction(self):
        pool = ApiKeyPool({
            "e2b_api_keys": [
                "# 注释行应被忽略",
                "e2b_real_one # 主力生产号",
                "备用测试号 # e2b_real_two",
            ]
        })
        id_one = pool.identifier("e2b_real_one")
        id_two = pool.identifier("e2b_real_two")
        self.assertEqual(pool.resolve(id_one), "e2b_real_one")
        self.assertEqual(pool.resolve(id_two), "e2b_real_two")
        self.assertEqual(pool.alias(id_one), "主力生产号")
        self.assertEqual(pool.alias(id_two), "备用测试号")

    def test_conf_schema_and_metadata_keys_and_defaults_are_aligned(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        meta_text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        for key in schema:
            self.assertIn(f"  {key}:", meta_text, f"metadata.yaml 缺少配置键 {key}")
        self.assertIn('version: "1.5.0"', meta_text)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; IME & view mode check skipped")
    def test_frontend_ime_enter_guard_and_view_toggle(self):
        panel_js = (DASHBOARD / "panel.js").read_text(encoding="utf-8")
        self.assertIn("event.isComposing || event.keyCode === 229", panel_js)
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class FakeEl {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.attributes = {};
    this.listeners = new Map();
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = true;
    this.value = '';
    this.open = false;
    this.isConnected = true;
    this.style = {};
    this._text = '';
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  appendChild(c) { if (c && c._frag) this.children.push(...c.children); else this.children.push(c); return c; }
  replaceChildren(...nodes) { this.children = []; nodes.forEach((n) => this.appendChild(n)); }
  addEventListener(n, fn) { if (!this.listeners.has(n)) this.listeners.set(n, []); this.listeners.get(n).push(fn); }
  dispatch(n, ev = {}) { const e = {preventDefault: () => {}, ...ev}; (this.listeners.get(n) || []).forEach((fn) => fn(e)); }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const ids = [
  'refresh-button', 'auto-refresh', 'poll-state', 'notice', 'session-list', 'session-search',
  'status-filter', 'previous-page', 'next-page', 'page-label', 'key-list', 'confirm-dialog',
  'confirm-action', 'cancel-action', 'pause-ack', 'kill-id', 'fill-kill-id', 'kill-match-hint',
  'action-feedback', 'sessions-section', 'sessions-region', 'view-table-btn', 'view-card-btn',
  'confirm-title', 'confirm-description', 'confirm-session', 'confirm-sandbox', 'confirm-key',
  'pause-ack-label', 'kill-input-label', 'slot-matrix', 'scope-badge'
];
const els = new Map(ids.map((id) => [id, new FakeEl('div', id)]));
els.get('status-filter').value = 'all';
let actCount = 0;
const fakeDoc = {
  hidden: false,
  documentElement: new FakeEl('html'),
  activeElement: null,
  getElementById: (id) => els.get(id) || null,
  createElement: (t) => new FakeEl(t),
  createDocumentFragment: () => ({_frag: true, children: [], appendChild(c) { this.children.push(c); }}),
  addEventListener: () => {},
};
class PanelClientError extends Error {}
const fakeWin = {
  document: fakeDoc,
  HTMLElement: FakeEl,
  parent: {},
  AstrBotPluginView: { ready: async () => ({pluginName: 'astrbot_plugin_e2b_sandbox', isDark: true}) },
  E2BPanelBridge: {
    PanelClientError,
    createClient: () => ({
      getSnapshot: async () => ({
        source: 'local_tracking_only',
        generated_at: 1700000000,
        counts: {total: 1, running: 1, paused: 0, unknown: 0, busy: 0},
        capacity: {limit: 3, occupied: 1, available: 2, waiting: 0, max_queue: 10, sandbox_scope: 'group_member'},
        keys: [{label: 'Key #1', alias: '主力号', bound_sessions: 1, cooldown_seconds: 0, available: true}],
        sessions: [{
          session_ref: 'a'.repeat(64),
          session_label: 'aiocqhttp:GroupMessage:88888#user:1001',
          sandbox_id: 'sbx-target-1',
          version: 'b'.repeat(64),
          status: 'running',
          key_label: 'Key #1',
          key_alias: '主力号',
          owner_known: true,
          can_pause: true,
          can_kill: true,
        }],
      }),
      act: async (body) => { actCount += 1; return {action: body.action, sandbox_id: body.expected_sandbox_id, outcome: 'killed'}; },
      destroy: () => {},
    }),
  },
  setTimeout: () => 1,
  clearTimeout: () => {},
  setInterval: () => 2,
  clearInterval: () => {},
  addEventListener: () => {},
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  window: fakeWin, document: fakeDoc, HTMLElement: FakeEl, AbortController,
  Date, Math, Number, String, Array, Object, Promise, Error,
});
(async () => {
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(els.get('scope-badge').textContent, '群聊按成员独立隔离');
  assert.equal(els.get('slot-matrix').children.length, 3);

  // 测试表格/卡片视图切换
  els.get('view-card-btn').dispatch('click');
  assert.equal(els.get('sessions-region').getAttribute('data-view'), 'cards');

  // 打开销毁确认弹窗并测试中文输入法合成态 Enter 不会误触发提交
  const row = els.get('session-list').children[0];
  const killBtn = row.children[5].children[0].children[0].children[1];
  killBtn.dispatch('click');
  assert.equal(els.get('confirm-dialog').open, true);
  assert.ok(els.get('confirm-session').textContent.includes('群聊 88888 · 成员 1001'));

  els.get('fill-kill-id').dispatch('click');
  assert.equal(els.get('kill-id').value, 'sbx-target-1');
  assert.equal(els.get('confirm-action').disabled, false);

  // 输入法合成中（isComposing=true 或 keyCode=229）按回车严禁提交销毁
  els.get('kill-id').dispatch('keydown', {key: 'Enter', isComposing: true});
  els.get('kill-id').dispatch('keydown', {key: 'Enter', isComposing: false, keyCode: 229});
  assert.equal(actCount, 0);

  // 正常非合成态按回车成功提交
  els.get('kill-id').dispatch('keydown', {key: 'Enter', isComposing: false, keyCode: 13});
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(actCount, 1);
  console.log('v1.5.0 frontend assertions passed');
})().catch((err) => { console.error(err); process.exitCode = 1; });
"""
        node = shutil.which("node")
        result = subprocess.run([node, "-e", script, str(DASHBOARD / "panel.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("v1.5.0 frontend assertions passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
