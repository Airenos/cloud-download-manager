# Icon Rail And Centered Modals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the wide administrator sidebar with a 64px icon rail and three centered modal tools without losing any v0.8.2 or file-sharing behavior.

**Architecture:** Keep the existing server-rendered forms and APIs. Change only their home-page containers, add accessible vanilla JavaScript modal state management, and replace sidebar responsive CSS with desktop icon-rail and mobile bottom-toolbar rules.

**Tech Stack:** Python 3 standard library, SSR HTML/CSS, vanilla JavaScript, `unittest`, Node.js syntax checks.

---

### Task 1: Render Icon Rail And Modal Containers

**Files:**
- Modify: `tests/test_download_server.py`
- Modify: `download_server.py`

- [ ] Add a failing render test asserting `admin-tool-rail`, buttons `open-add-task`, `open-upload`, `open-tasks`, their `aria-controls`, and modals `add-task-modal`, `upload-modal`, `tasks-modal`.
- [ ] Assert the existing form actions, field IDs, aria2 remove/clear forms, upload progress, and upload cancel control remain in the rendered page.
- [ ] Run the focused test and confirm it fails on `admin-tool-rail`.
- [ ] Move the three current administrator sections into modal containers and render the icon rail without changing backend-facing form fields or actions.
- [ ] Run focused tests and the complete suite.

### Task 2: Add Accessible Modal Behavior And Upload Busy Protection

**Files:**
- Modify: `tests/test_download_server.py`
- Modify: `download_server.py`

- [ ] Add a failing script test for `openAdminModal`, `closeAdminModal`, `closeActiveAdminModal`, `data-busy`, outside click, `Escape`, focus return, and `aria-expanded` updates.
- [ ] Run the focused test and confirm the functions are absent.
- [ ] Implement one-open-modal state, close buttons, overlay click, Escape handling, focus return, and the busy guard.
- [ ] In `handleUpload()`, set `upload-modal.dataset.busy = 'true'` before init and clear it in `finally`; retain existing retry, progress, worker, finish, and cancel behavior.
- [ ] Run focused tests, the Node syntax check, and the complete suite.

### Task 3: Replace Sidebar CSS With Icon Rail And Mobile Toolbar

**Files:**
- Modify: `tests/test_download_server.py`
- Modify: `download_server.py`
- Modify: `README.md`
- Modify: `TESTING.md`

- [ ] Add a failing CSS test for `64px minmax(0, 1fr)`, centered overlay/modal rules, `max-height: 88vh`, and the mobile fixed bottom toolbar.
- [ ] Run the focused test and confirm it fails on the old `300px` layout.
- [ ] Implement stable rail buttons, tooltips, overlay, modal width variants, internal scrolling, task-table overflow, and mobile toolbar rules.
- [ ] Update documentation from wide sidebar to icon rail plus centered modals.
- [ ] Run the full Python tests, AST parse, rendered JavaScript syntax check, HTTP SSR checks, and `git diff --check`.
- [ ] Restart the local 8082 verification server and generate desktop/tablet/mobile screenshots when browser approval is available.

No route, API, persistence, database, or database field changes are included.
