# A1 Two-Column UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the temporary sharing site's home page as a file-first two-column interface with a left administrator sidebar while preserving every v0.8.2 upload, aria2, preview, download, sharing, renewal, theme, and mobile capability.

**Architecture:** Keep the existing standard-library single-file server and server-rendered HTML. Restructure `render_home()` and `render_file_rows()`, extend the shared embedded CSS and vanilla JavaScript, and lock the behavior down with focused `unittest` rendering tests before each implementation step. No route, API contract, persistence format, database, or database field changes are allowed.

**Tech Stack:** Python 3 standard library, `ThreadingHTTPServer`, server-rendered HTML/CSS, minimal vanilla JavaScript, `unittest`, Node.js syntax checks, browser viewport inspection.

---

## File Map

- Modify `download_server.py`: file-row markup, action menu, A1 home-page structure, shared CSS, responsive behavior, and small menu/sidebar/filter scripts.
- Modify `tests/test_download_server.py`: focused rendering and regression tests for every preserved feature and responsive hook.
- Modify `README.md`: update the UI overview and retain the v0.8.2 operational notes already present in the worktree.
- Modify `TESTING.md`: add desktop/mobile visual acceptance checks while retaining the v0.8.2 test guidance already present in the worktree.

The current worktree already contains the completed but uncommitted v0.8.2 implementation in all four files. Task 1 records that baseline before UI work so later UI commits remain reviewable.

### Task 1: Verify And Record The v0.8.2 Baseline

**Files:**
- Existing changes: `download_server.py`
- Existing changes: `tests/test_download_server.py`
- Existing changes: `README.md`
- Existing changes: `TESTING.md`

- [ ] **Step 1: Confirm only the known v0.8.2 files are dirty**

Run:

```text
git status --short
git diff --stat
```

Expected: exactly `README.md`, `TESTING.md`, `download_server.py`, and `tests/test_download_server.py` are modified; the UI plan/spec files may already be committed and must not appear as dirty.

- [ ] **Step 2: Re-run the complete v0.8.2 suite without bytecode writes**

Run:

```text
python -B -m unittest tests.test_download_server -v
```

Expected: 39 tests pass and the final line is `OK`.

- [ ] **Step 3: Check Python parsing, rendered JavaScript, and whitespace**

Run the Python AST parse:

```text
python -B -c "import ast, pathlib; ast.parse(pathlib.Path('download_server.py').read_text(encoding='utf-8')); print('AST OK')"
```

Expected: `AST OK`.

Pipe all rendered inline scripts directly into Node's syntax checker, then check whitespace:

```powershell
python -B -c "import re, download_server; page=download_server.render_home().decode('utf-8'); print('\n'.join(re.findall(r'<script>([\s\S]*?)</script>', page)))" | node --check -
git diff --check
```

Expected: every script check exits 0 and `git diff --check` prints nothing.

- [ ] **Step 4: Commit only the verified v0.8.2 baseline**

```text
git add download_server.py tests/test_download_server.py README.md TESTING.md
git commit -m "feat: complete v0.8.2 upload pipeline"
```

Expected: the commit contains only the four listed files and `git status --short` is empty.

### Task 2: Build File Rows And The Complete Action Menu

**Files:**
- Modify: `tests/test_download_server.py`
- Modify: `download_server.py:1302-1360`

- [ ] **Step 1: Write failing tests for file-first rows and preserved actions**

Add these tests to `DownloadServerBehaviorTests`:

```python
def test_file_rows_render_icons_primary_actions_and_more_menu(self):
    files = [
        {
            "name": "clip.mp4",
            "url_name": "clip.mp4",
            "file_type": "video",
            "size_human": "12 MiB",
            "created_at_text": "2026-07-16 01:00",
            "expires_at_text": "2026-07-17 01:00",
            "remaining_text": "23h",
            "retention_label": "24h",
        }
    ]

    body = self.ds.render_file_rows(files, compact=True)

    self.assertIn('class="file-row"', body)
    self.assertIn('class="file-type-icon"', body)
    self.assertIn('href="/view/clip.mp4"', body)
    self.assertIn('href="/file/clip.mp4"', body)
    self.assertIn('class="file-menu-toggle"', body)
    self.assertIn('data-url="/file/clip.mp4"', body)
    self.assertIn('href="/once/clip.mp4"', body)
    self.assertIn('action="/api/renew"', body)
    self.assertIn('aria-expanded="false"', body)

def test_file_rows_escape_names_in_menu_and_metadata(self):
    files = [{
        "name": '<bad&".txt',
        "url_name": "%3Cbad%26%22.txt",
        "file_type": "text",
        "size_human": "1 B",
        "created_at_text": "now",
        "expires_at_text": "later",
        "remaining_text": "1h",
        "retention_label": "1h",
    }]

    body = self.ds.render_file_rows(files, compact=True)

    self.assertNotIn('<bad&".txt', body)
    self.assertIn('&lt;bad&amp;&quot;.txt', body)
    self.assertIn('value="&lt;bad&amp;&quot;.txt"', body)
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```text
python -B -m unittest tests.test_download_server.DownloadServerBehaviorTests.test_file_rows_render_icons_primary_actions_and_more_menu tests.test_download_server.DownloadServerBehaviorTests.test_file_rows_escape_names_in_menu_and_metadata -v
```

Expected: failures because compact rows currently omit renewal and one-time download and have no icon/menu markup.

- [ ] **Step 3: Add a deterministic file-type icon helper**

Add immediately above `render_file_rows()`:

```python
FILE_TYPE_ICONS = {
    "image": "🖼",
    "video": "▶",
    "text": "≡",
    "document": "▤",
    "archive": "▣",
    "other": "•",
}


def file_type_icon(file_type_name: str) -> str:
    return FILE_TYPE_ICONS.get(file_type_name, FILE_TYPE_ICONS["other"])
```

The visible glyph must be paired with `aria-hidden="true"`; the readable type remains available through the filename and metadata.

- [ ] **Step 4: Replace file row actions with primary actions plus a menu**

Keep the existing search/filter bar and table wrapper, but generate every row with this structure. Use the loop index for a unique menu ID and use escaped values exactly as shown:

```python
safe_name = html.escape(name)
safe_name_attr = html.escape(name, quote=True)
menu_id = f"file-menu-{index}"
preview = (
    f'<a class="button secondary file-action" href="/view/{url_name}">预览</a>'
    if kind else ""
)
renew_form = (
    '<form class="renew-form" method="post" action="/api/renew">'
    f'<input type="hidden" name="filename" value="{safe_name_attr}">'
    '<label class="sr-only" for="renew-password-' + str(index) + '">管理密码</label>'
    '<input id="renew-password-' + str(index) + '" type="password" '
    'name="password" placeholder="管理密码" required>'
    '<button class="secondary" type="submit">续期</button></form>'
)
more_menu = (
    '<div class="file-menu">'
    f'<button class="file-menu-toggle secondary" type="button" aria-label="更多操作：{safe_name_attr}" '
    f'aria-expanded="false" aria-controls="{menu_id}">⋯</button>'
    f'<div id="{menu_id}" class="file-menu-panel" hidden>'
    f'<button class="share-btn menu-command" type="button" data-url="/file/{url_name}" '
    f'data-name="{safe_name_attr}">二维码分享</button>'
    f'<a class="menu-command danger-text" href="/once/{url_name}">一次性下载</a>'
    f'{renew_form}</div></div>'
)
```

Render the primary action region in this fixed order:

```python
actions = (
    f'{preview}'
    f'<a class="button secondary file-action" href="/file/{url_name}">下载</a>'
    f'{more_menu}'
)
```

Render `.file-type-icon` before the filename and place size, created time, remaining time, and retention in a `.file-meta` line. Keep the existing table columns in `/downloads/` if needed, but use the same complete actions for both compact and full variants so no route loses QR sharing, once download, or renewal.

- [ ] **Step 5: Run focused and full tests**

Run the focused command from Step 2, then:

```text
python -B -m unittest tests.test_download_server -v
```

Expected: both new tests and all existing tests pass.

- [ ] **Step 6: Commit file rows**

```text
git add download_server.py tests/test_download_server.py
git commit -m "feat: organize complete file actions"
```

### Task 3: Restructure The Home Page Into A1 Desktop Layout

**Files:**
- Modify: `tests/test_download_server.py`
- Modify: `download_server.py:1403-1497`

- [ ] **Step 1: Write a failing home-layout and feature-preservation test**

Add:

```python
def test_home_renders_a1_layout_without_losing_admin_features(self):
    tasks = {"ok": True, "tasks": [{
        "gid": "abc123",
        "name": "queued.bin",
        "hint": "",
        "status": "active",
        "progress": 25.0,
        "speed_human": "1 MiB/s",
        "completed_human": "1 MiB",
        "total_human": "4 MiB",
    }]}
    with mock.patch.object(self.ds, "get_aria2_tasks", return_value=tasks):
        body = self.ds.render_home("hello <world>").decode("utf-8")

    for marker in (
        'class="app-shell"',
        'id="administrator-tools"',
        'class="file-workspace"',
        'class="status-strip"',
        'id="administrator-toggle"',
        'action="/api/add-task"',
        'id="upload-form"',
        'action="/api/remove-task"',
        'action="/api/clear-stopped"',
        'id="upload-cancel"',
    ):
        self.assertIn(marker, body)

    for field in (
        'id="url"',
        'id="filename"',
        'id="task-retention"',
        'id="password"',
        'id="upload-file"',
        'id="upload-filename"',
        'id="upload-retention"',
        'id="upload-password"',
    ):
        self.assertIn(field, body)

    self.assertIn("hello &lt;world&gt;", body)
    self.assertNotIn("hello <world>", body)
```

Retain `test_home_renders_upload_v2_client`; together the two tests protect all four v0.8.2 endpoints, worker concurrency, retry delays, active XHR tracking, and cancellation.

- [ ] **Step 2: Run the focused tests and confirm the new test fails**

```text
python -B -m unittest tests.test_download_server.DownloadServerBehaviorTests.test_home_renders_a1_layout_without_losing_admin_features tests.test_download_server.DownloadServerBehaviorTests.test_home_renders_upload_v2_client -v
```

Expected: the A1 layout test fails on `.app-shell`; the existing v0.8.2 rendering test passes.

- [ ] **Step 3: Calculate a safe aria2 task count for the sidebar summary**

Immediately after `tasks = get_aria2_tasks()` in `render_home()`, add:

```python
task_items = tasks.get("tasks") if isinstance(tasks, dict) else []
task_count = len(task_items) if isinstance(task_items, list) else 0
tasks_ok = bool(tasks.get("ok")) if isinstance(tasks, dict) else False
task_summary = f"{task_count} 个任务" if tasks_ok else "状态不可用"
```

- [ ] **Step 4: Replace the home body with file-first DOM order and desktop grid areas**

Keep the existing form field names, IDs, actions, upload progress IDs, and retention helpers unchanged. Use this top-level structure:

```html
<header class="site-header">
  <div><h1>临时下载站</h1><p>给朋友分享临时文件</p></div>
  <div class="header-actions">
    <a class="button secondary" href="/downloads/">下载目录</a>
    <button class="icon-button" type="button" onclick="location.reload()" aria-label="刷新" title="刷新">↻</button>
    <button class="icon-button theme-toggle" type="button" onclick="toggleTheme()" aria-label="切换主题" title="切换主题">◐</button>
  </div>
</header>
{message_html}
<button id="administrator-toggle" class="administrator-toggle secondary" type="button"
        aria-expanded="false" aria-controls="administrator-tools">管理员工具</button>
<div class="app-shell">
  <div class="file-workspace">
    <div class="status-strip">
      <div class="status-item"><span>文件</span><strong>{len(files)}</strong></div>
      <div class="status-item"><span>目录占用</span><strong>{html.escape(str(stats["downloads_used_human"]))}</strong></div>
      <div class="status-item"><span>剩余空间</span><strong>{html.escape(str(stats["free_human"]))}</strong></div>
      <div class="status-item"><span>默认保留</span><strong>{RETENTION_HOURS:g}h</strong></div>
    </div>
    <section class="file-section"><div class="section-heading"><h2>可用文件</h2></div>{render_file_rows(files, compact=True)}</section>
  </div>
  <aside id="administrator-tools" class="administrator-tools" aria-label="管理员工具">
    <section class="admin-section">
      <h2>添加链接</h2>
      <form method="post" action="/api/add-task">
        <label for="url">下载链接</label>
        <input id="url" name="url" type="text" inputmode="url" required>
        <label for="filename">自定义文件名（可选）</label>
        <input id="filename" name="filename" type="text" maxlength="180">
        {retention_select_html('task-retention')}
        <label for="password">管理密码</label>
        <input id="password" name="password" type="password" required>
        <input type="submit" value="添加任务">
      </form>
    </section>
    <section class="admin-section">
      <h2>上传文件</h2>
      <form id="upload-form" method="post" action="/api/upload" enctype="multipart/form-data" onsubmit="return handleUpload(this)">
        <div id="drop-zone" class="drop-zone">
          <span class="drop-icon" aria-hidden="true">▣</span>
          <p>拖拽文件到这里，或点击选择文件</p>
          <input id="upload-file" name="file" type="file" required>
        </div>
        <div id="drop-file-name" class="muted"></div>
        <label for="upload-filename">自定义文件名（可选）</label>
        <input id="upload-filename" name="filename" type="text" maxlength="180">
        {retention_select_html('upload-retention')}
        <label for="upload-password">管理密码</label>
        <input id="upload-password" name="password" type="password" required>
        <div class="form-submit"><input type="submit" value="上传"></div>
        <div id="upload-progress" class="upload-progress">
          <div class="upload-bar-outer"><div id="upload-bar" class="upload-bar-inner"></div></div>
          <div id="upload-status" class="upload-status"></div>
          <button id="upload-cancel" class="danger" type="button" hidden>取消上传</button>
        </div>
      </form>
    </section>
    <details class="admin-section task-section">
      <summary>下载任务 <span>{html.escape(task_summary)}</span></summary>
      {render_task_rows(tasks)}
    </details>
  </aside>
</div>
```

Inside `.status-strip`, output exactly four labeled values from the existing `stats` and configuration: file count, download directory usage, remaining space, and default retention. Keep `.disk-bar-outer` and the existing warn/danger class calculation under the remaining-space value.

Inside the aside, place three sibling `.admin-section` blocks in this order:

1. “添加链接” containing the complete `/api/add-task` form.
2. “上传文件” containing the complete `#upload-form`, drop zone, v0.8.2 progress/status, and cancel control.
3. A `<details class="admin-section task-section">` whose summary includes `task_summary` and whose body is `render_task_rows(tasks)`.

Use single-column form markup in the sidebar; remove `.form-grid` wrappers there without changing any input ID/name. Keep the legal-use and expiry notes as a compact `.site-note` below the file list rather than another card.

- [ ] **Step 5: Run focused and full tests**

Run the command from Step 2, then the full suite.

Expected: all tests pass; the v0.8.2 test still finds `/api/upload_init`, `/api/upload_chunk`, `/api/upload_finish`, `/api/upload_cancel`, retry delays, worker state, and `#upload-cancel`.

- [ ] **Step 6: Commit the server-rendered A1 structure**

```text
git add download_server.py tests/test_download_server.py
git commit -m "feat: render file-first A1 home layout"
```

### Task 4: Add Responsive Styling And Accessible Interactions

**Files:**
- Modify: `tests/test_download_server.py`
- Modify: `download_server.py:893-1270`

- [ ] **Step 1: Write failing style and script hook tests**

Add:

```python
def test_page_includes_responsive_a1_and_accessible_menu_behavior(self):
    body = self.ds.render_home().decode("utf-8")

    for css_or_script in (
        "grid-template-areas",
        "300px minmax(0, 1fr)",
        "@media (max-width: 900px)",
        ".file-type-icon",
        "font-size: 28px",
        ".administrator-tools.mobile-open",
        "function toggleFileMenu",
        "function closeFileMenus",
        "function toggleAdministratorTools",
        "aria-expanded",
        "filter-empty",
        "Escape",
    ):
        self.assertIn(css_or_script, body)
```

- [ ] **Step 2: Run the focused test and confirm it fails**

```text
python -B -m unittest tests.test_download_server.DownloadServerBehaviorTests.test_page_includes_responsive_a1_and_accessible_menu_behavior -v
```

Expected: failure on the first new CSS or script marker.

- [ ] **Step 3: Replace the home-specific CSS with stable A1 layout rules**

Retain shared preview, form, modal, toast, progress, and dark-theme rules. Add these exact structural rules, then style their child elements with existing CSS variables:

```css
main { width: min(1440px, calc(100% - 32px)); margin: 0 auto; padding: 20px 0 40px; }
.site-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; }
.header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.icon-button { width: 40px; height: 40px; padding: 0; display: inline-grid; place-items: center; font-size: 20px; }
.app-shell { display: grid; grid-template-columns: 300px minmax(0, 1fr); grid-template-areas: "admin files"; gap: 18px; align-items: start; }
.file-workspace { grid-area: files; min-width: 0; padding: 0; }
.administrator-tools { grid-area: admin; position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow-y: auto; }
.administrator-toggle { display: none; width: 100%; margin-bottom: 12px; }
.admin-section { padding: 14px 0; border-bottom: 1px solid var(--line); }
.admin-section:first-child { padding-top: 0; }
.status-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); }
.status-item { min-width: 0; padding: 12px; border-right: 1px solid var(--line); }
.status-item:last-child { border-right: 0; }
.file-section { margin-top: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); padding: 16px; }
.file-row-main { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
.file-type-icon { width: 32px; flex: 0 0 32px; font-size: 28px; line-height: 1; text-align: center; }
.file-name { overflow-wrap: anywhere; }
.file-meta { display: flex; flex-wrap: wrap; gap: 4px 10px; color: var(--muted); font-size: 12px; }
.file-actions { display: flex; justify-content: flex-end; align-items: center; gap: 6px; min-width: 184px; }
.file-menu { position: relative; }
.file-menu-toggle { width: 40px; height: 40px; padding: 0; font-size: 20px; }
.file-menu-panel { position: absolute; z-index: 20; right: 0; top: calc(100% + 6px); width: min(260px, calc(100vw - 32px)); padding: 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--card-bg); box-shadow: 0 10px 28px rgba(15, 23, 42, .16); }
.file-menu-panel[hidden] { display: none; }
.menu-command { display: block; width: 100%; padding: 9px 10px; text-align: left; background: transparent; color: var(--text); }
.renew-form { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px; padding-top: 6px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.filter-empty { display: none; padding: 24px 16px; color: var(--muted); text-align: center; }
.filter-empty.visible { display: block; }
```

Use neutral light/dark variables and replace purple gradient progress fills with solid semantic colors. Keep every principal radius at `8px` or below.

Add the responsive rules:

```css
@media (max-width: 900px) {
  main { width: min(100% - 24px, 720px); padding-top: 12px; }
  .site-header { align-items: flex-start; }
  .app-shell { display: flex; flex-direction: column; }
  .file-workspace { order: 1; width: 100%; }
  .administrator-toggle { display: block; order: 2; }
  .administrator-tools { display: none; order: 3; position: static; max-height: none; width: 100%; overflow: visible; }
  .administrator-tools.mobile-open { display: block; }
  .status-strip { grid-template-columns: 1fr 1fr; }
  .status-item:nth-child(2) { border-right: 0; }
  .file-table thead { display: none; }
  .file-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 12px 0; }
  .file-row > td { display: block; border: 0; padding: 0; }
  .file-actions { min-width: 0; }
}

@media (max-width: 520px) {
  .site-header { flex-wrap: wrap; }
  .header-actions { width: 100%; justify-content: flex-start; }
  .status-strip { grid-template-columns: 1fr; }
  .status-item { border-right: 0; border-bottom: 1px solid var(--line); }
  .status-item:last-child { border-bottom: 0; }
  .file-row { grid-template-columns: 1fr; }
  .file-actions { justify-content: flex-start; padding-left: 42px; }
}
```

- [ ] **Step 4: Add menu, mobile sidebar, and empty-filter JavaScript**

Replace the implicit global `event` use in `filterFiles()` with a passed button: markup calls `filterFiles('video', this)`. Implement:

```javascript
function closeFileMenus(exceptMenu) {
  var menus = document.querySelectorAll('.file-menu');
  for (var i = 0; i < menus.length; i++) {
    if (menus[i] === exceptMenu) continue;
    var button = menus[i].querySelector('.file-menu-toggle');
    var panel = menus[i].querySelector('.file-menu-panel');
    if (button) button.setAttribute('aria-expanded', 'false');
    if (panel) panel.hidden = true;
  }
}

function toggleFileMenu(button) {
  var menu = button.closest('.file-menu');
  var panel = document.getElementById(button.getAttribute('aria-controls'));
  var opening = button.getAttribute('aria-expanded') !== 'true';
  closeFileMenus(opening ? menu : null);
  button.setAttribute('aria-expanded', opening ? 'true' : 'false');
  panel.hidden = !opening;
  if (opening) {
    var first = panel.querySelector('button, a, input');
    if (first) first.focus();
  }
}

function toggleAdministratorTools() {
  var button = document.getElementById('administrator-toggle');
  var tools = document.getElementById('administrator-tools');
  var opening = !tools.classList.contains('mobile-open');
  tools.classList.toggle('mobile-open', opening);
  button.setAttribute('aria-expanded', opening ? 'true' : 'false');
}
```

Bind `.file-menu-toggle` and `#administrator-toggle` through one document click listener. Close menus on an outside click and on `Escape`; after Escape, restore focus to the button that controlled the open panel.

Update `_applyFilters()` to count visible `.file-row` nodes and toggle `#filter-empty`:

```javascript
var visible = 0;
for (var i = 0; i < rows.length; i++) {
  var name = rows[i].getAttribute('data-name') || '';
  var matchType = _curFilter === 'all' || rows[i].getAttribute('data-type') === _curFilter;
  var matchSearch = !q || name.toLowerCase().indexOf(q) >= 0;
  rows[i].style.display = matchType && matchSearch ? '' : 'none';
  if (matchType && matchSearch) visible += 1;
}
var empty = document.getElementById('filter-empty');
if (empty) empty.classList.toggle('visible', rows.length > 0 && visible === 0);
```

Add `<div id="filter-empty" class="filter-empty">没有匹配的文件</div>` after the file table. Keep the existing server-rendered “暂无可下载文件” for the true empty state.

- [ ] **Step 5: Run tests and JavaScript syntax checks**

Run:

```text
python -B -m unittest tests.test_download_server -v
```

Then check the complete rendered JavaScript again:

```powershell
python -B -c "import re, download_server; page=download_server.render_home().decode('utf-8'); print('\n'.join(re.findall(r'<script>([\s\S]*?)</script>', page)))" | node --check -
```

Expected: the full Python suite passes and every JavaScript block parses.

- [ ] **Step 6: Commit responsive UI behavior**

```text
git add download_server.py tests/test_download_server.py
git commit -m "feat: add responsive A1 interactions"
```

### Task 5: Documentation, Visual QA, And Final Regression

**Files:**
- Modify: `README.md`
- Modify: `TESTING.md`
- Verify: `download_server.py`
- Verify: `tests/test_download_server.py`

- [ ] **Step 1: Update documentation without expanding product scope**

In `README.md`, replace the old page-layout description with these facts:

- Desktop uses a left administrator sidebar and a wider file workspace.
- Mobile shows files first and exposes the complete sidebar through “管理员工具”.
- Video rows retain both preview and normal download.
- QR sharing, one-time download, and renewal live in the per-file more menu.

Keep all existing v0.8.2 configuration, limits, upload diagnostics, and deployment instructions unchanged.

In `TESTING.md`, add the three viewport sizes (`1440x900`, `1024x768`, `390x844`) and the interaction matrix from the design spec: long names, empty state, no-match state, upload in progress, aria2 unavailable, menu outside click, Escape, video preview/download, light theme, and dark theme.

- [ ] **Step 2: Run the complete automated verification**

```text
python -B -m unittest tests.test_download_server -v
python -B -c "import ast, pathlib; ast.parse(pathlib.Path('download_server.py').read_text(encoding='utf-8')); print('AST OK')"
git diff --check
```

Expected: every test passes, output includes `AST OK`, and the diff check is silent.

- [ ] **Step 3: Start the server on an unused local port**

Use port 8081 when free; otherwise use 8082:

```text
$env:HOST='127.0.0.1'; $env:PORT='8081'; python -B download_server.py
```

Expected: the server reports `http://127.0.0.1:8081/` and stays running for browser verification.

- [ ] **Step 4: Perform desktop and mobile visual acceptance**

At `1440x900` and `1024x768`, verify the administrator sidebar is left of the wider file area, the sidebar can scroll independently when tall, the compact status strip does not wrap incoherently, and video rows expose both “预览” and “下载”.

At `390x844`, verify files appear before the collapsed administrator tools, expanding the entry reveals all three administrator sections, file names and actions do not overflow, menus align inside the viewport, and touch targets remain approximately 40px.

In both light and dark themes, inspect long Chinese/English filenames, the no-files state, a no-match search, the QR modal, once-download warning styling, upload progress, retry/worker text, cancel control, and aria2 error state. Record screenshots for each viewport and inspect them before completion.

- [ ] **Step 5: Exercise representative workflows**

Use a disposable small text file and a disposable small video file:

1. Upload the text file and confirm progress, worker, retry, and cancel UI remains stable.
2. Add a link task with custom filename and retention, then confirm aria2 task status and removal form remain reachable.
3. Preview and normally download the video from the same row.
4. Open QR sharing, copy the link, close by outside click, reopen, and close with Escape.
5. Open the more menu and verify renewal still requires the administrator password.

Do not trigger one-time download on a file that must be preserved; use a disposable file for that route.

- [ ] **Step 6: Review the final diff against the approved scope**

```text
git status --short
git diff --stat HEAD~3..HEAD
git diff HEAD~3..HEAD -- download_server.py tests/test_download_server.py README.md TESTING.md
```

Confirm there are no new routes, no persistence changes, no database fields, no frontend framework or build dependency, and no loss of `/downloads/`, `/view/`, `/file/`, `/once/`, upload v2, aria2 removal/cleanup, refresh, or theme behavior.

- [ ] **Step 7: Commit documentation and any visual-only fixes**

```text
git add README.md TESTING.md download_server.py tests/test_download_server.py
git commit -m "docs: document A1 UI workflows"
```

If visual QA required source fixes, first add a focused rendering regression test, run the full suite again, and use a separate `fix: address A1 visual QA findings` commit before the documentation commit.
