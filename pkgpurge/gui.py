"""GTK4 图形界面：软件包列表 + 多选卸载 + 依赖级联模拟预览 + pkexec 提权执行.

线程模型：所有包管理器调用（list/info/simulate/elevated_run）都在 worker
线程执行，结果通过 GLib.idle_add 回主线程刷新 UI，界面不冻结。
"""

from __future__ import annotations

import json
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .backends import detect_backend, distro_pretty  # noqa: E402
from .backends.base import Package, human_size  # noqa: E402
from .i18n import _  # noqa: E402
from .priv import elevated_run, find_helper  # noqa: E402

MAX_PREVIEW_ROWS = 400


def format_removal_preview(will_remove: list[str], requested: list[str]) -> str:
    """确认对话框正文：区分「你勾选的」和「被依赖级联带走的」."""
    extra = [n for n in will_remove if n not in requested]
    lines = []
    for n in will_remove[:MAX_PREVIEW_ROWS]:
        lines.append(f"  • {n}" + ("   (dependency)" if n in extra else ""))
    if len(will_remove) > MAX_PREVIEW_ROWS:
        lines.append(f"  ... +{len(will_remove) - MAX_PREVIEW_ROWS} more")
    return "\n".join(lines)


class ProgressWindow(Gtk.Window):
    """卸载执行中的输出窗口：ProgressBar 脉冲 + 实时日志."""

    def __init__(self, parent: Gtk.Window):
        super().__init__(title=_("正在执行卸载…"), transient_for=parent,
                         modal=True, default_width=760, default_height=460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        self.set_child(box)
        self.bar = Gtk.ProgressBar()
        box.append(self.bar)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_monospace(True)
        self.view.set_vexpand(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.view)
        scroll.set_vexpand(True)
        box.append(scroll)
        self._pulse = GLib.timeout_add(120, self._on_pulse)
        self._closed = False

    def _on_pulse(self) -> bool:
        if self._closed:
            return False
        self.bar.pulse()
        return True

    def append_line(self, text: str) -> None:
        if self._closed:
            return
        buf = self.view.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.view.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)

    def finish(self) -> None:
        self._closed = True
        self.bar.set_fraction(1.0)
        self.close()


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, selftest: bool = False):
        super().__init__(application=app, title=_("软件包卸载器"),
                         default_width=1120, default_height=720)
        self.selftest = selftest
        self.backend = detect_backend()
        self.distro = distro_pretty()
        self.all_pkgs: list[Package] = []
        self.checked: dict[str, Package] = {}   # removal target -> Package
        self._search_text = ""
        self._rows_by_target: dict[str, Gtk.ListBoxRow] = {}
        self._detail_thread_guard = 0

        self._build_ui()
        if self.backend is None:
            GLib.idle_add(self._fatal_no_backend)
        else:
            self.reload_packages()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        self.subtitle = Gtk.Label(
            label=f"{self.backend.label if self.backend else '—'} · {self.distro}")
        hb.set_title_widget(self.subtitle)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("搜索软件包…"))
        self.search.set_width_chars(28)
        self.search.connect("search-changed", self._on_search_changed)
        hb.pack_start(self.search)

        btn_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        btn_refresh.set_tooltip_text(_("刷新"))
        btn_refresh.connect("clicked", lambda *_: self.reload_packages())
        hb.pack_end(btn_refresh)

        btn_about = Gtk.Button.new_from_icon_name("dialog-information-symbolic")
        btn_about.set_tooltip_text(_("关于"))
        btn_about.connect("clicked", self._on_about)
        hb.pack_end(btn_about)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        paned.set_position(680)
        root.append(paned)

        # 左：软件包列表
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_filter_func(self._filter_row)
        self.listbox.connect("row-selected", self._on_row_selected)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_child(self.listbox)
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        paned.set_start_child(self.scroll)

        # 右：详情 + 操作
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        paned.set_end_child(right)

        detail_label = Gtk.Label(label=_("详细信息"))
        detail_label.set_xalign(0.0)
        detail_label.add_css_class("heading")
        right.append(detail_label)

        self.detail_view = Gtk.TextView()
        self.detail_view.set_editable(False)
        self.detail_view.set_monospace(True)
        self.detail_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        dscroll = Gtk.ScrolledWindow()
        dscroll.set_child(self.detail_view)
        dscroll.set_vexpand(True)
        right.append(dscroll)
        self._set_detail(_("选中左侧软件包查看详情"))

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right.append(actions)

        self.btn_remove = Gtk.Button.new_with_label(_("卸载所选"))
        self.btn_remove.add_css_class("destructive-action")
        self.btn_remove.connect("clicked", lambda *_: self._start_removal(purge=False))
        actions.append(self.btn_remove)

        if self.backend and self.backend.supports_purge:
            self.btn_purge = Gtk.Button.new_with_label(_("清除配置并卸载"))
            self.btn_purge.connect("clicked", lambda *_: self._start_removal(purge=True))
            actions.append(self.btn_purge)

        if self.backend and self.backend.supports_autoremove:
            self.btn_auto = Gtk.Button.new_with_label(_("清理孤立依赖"))
            self.btn_auto.connect("clicked", self._start_autoremove)
            actions.append(self.btn_auto)

        # 底部状态栏
        self.status = Gtk.Label(label=_("正在加载软件包列表…"))
        self.status.set_xalign(0.0)
        self.status.add_css_class("dim-label")
        root.append(self.status)

    def _fatal_no_backend(self) -> bool:
        dlg = Gtk.AlertDialog.new(_("本机未检测到受支持的包管理器"))
        dlg.set_detail("需要 apt-get / dnf / zypper / pacman / apk 之一。")
        dlg.show(self)
        return False

    # ---------- 数据加载 ----------

    def reload_packages(self) -> None:
        if self.backend is None:
            return
        self.status.set_label(_("正在加载软件包列表…"))
        self.listbox.set_sensitive(False)

        def worker():
            try:
                pkgs = self.backend.list_installed()
                GLib.idle_add(self._on_loaded, pkgs, None)
            except Exception as e:  # noqa: BLE001 — 后端异常必须显式呈现给用户
                GLib.idle_add(self._on_loaded, [], e)

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, pkgs: list[Package], error) -> bool:
        self.listbox.set_sensitive(True)
        if error is not None:
            self.status.set_label(f"加载失败: {error}")
            dlg = Gtk.AlertDialog.new("加载软件包列表失败")
            dlg.set_detail(str(error))
            dlg.show(self)
            if self.selftest:
                self._selftest_done(ok=False, error=str(error))
            return False
        self.all_pkgs = pkgs
        # 已勾选项里被卸载掉的自动清除
        alive = {self._target(p) for p in pkgs}
        for t in [t for t in self.checked if t not in alive]:
            del self.checked[t]
        self._rebuild_rows()
        self._update_status()
        if self.selftest:
            self._run_selftest_checks()
        return False

    def _target(self, p: Package) -> str:
        return self.backend.removal_target(p) if self.backend else p.name

    def _rebuild_rows(self) -> None:
        for row in list(self._rows_by_target.values()):
            self.listbox.remove(row)
        self._rows_by_target.clear()
        for p in self.all_pkgs:
            row = self._make_row(p)
            self.listbox.append(row)
            self._rows_by_target[self._target(p)] = row
        self.listbox.invalidate_filter()

    def _make_row(self, p: Package) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        row.set_child(box)

        cb = Gtk.CheckButton()
        cb.set_active(self._target(p) in self.checked)
        cb.connect("toggled", self._on_check_toggled, p)
        box.append(cb)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        texts.set_hexpand(True)
        box.append(texts)

        name = f"<b>{GLib.markup_escape_text(p.name)}</b>"
        if p.version:
            name += f'  <span size="small" alpha="60%">{GLib.markup_escape_text(p.version)}</span>'
        if p.auto_installed:
            name += '  <span size="small" foreground="#e5a50a">[auto]</span>'
        name_lbl = Gtk.Label(label=name, use_markup=True, xalign=0.0)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(name_lbl)

        if p.summary:
            sum_lbl = Gtk.Label(
                label=f'<span size="small" alpha="70%">{GLib.markup_escape_text(p.summary)}</span>',
                use_markup=True, xalign=0.0)
            sum_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            texts.append(sum_lbl)

        size_lbl = Gtk.Label(label=human_size(p.size_bytes))
        size_lbl.add_css_class("dim-label")
        size_lbl.set_valign(Gtk.Align.CENTER)
        box.append(size_lbl)

        row.pkg = p  # type: ignore[attr-defined]
        row.search_text = f"{p.name} {p.summary}".lower()  # type: ignore[attr-defined]
        return row

    def _on_check_toggled(self, cb: Gtk.CheckButton, p: Package) -> None:
        t = self._target(p)
        if cb.get_active():
            self.checked[t] = p
        else:
            self.checked.pop(t, None)
        self._update_status()

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        if not self._search_text:
            return True
        return self._search_text in getattr(row, "search_text", "")

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().lower()
        self.listbox.invalidate_filter()

    def _update_status(self) -> None:
        backend = self.backend.label if self.backend else "—"
        self.status.set_label(_("共 {n} 个软件包 · 已选 {m} 个 · {backend}")
                              .format(n=len(self.all_pkgs), m=len(self.checked),
                                      backend=backend))

    def _on_row_selected(self, lb: Gtk.ListBox, row) -> None:
        if row is None or not hasattr(row, "pkg"):
            return
        self._set_detail(_("加载中…"))
        p: Package = row.pkg
        self._detail_thread_guard += 1
        guard = self._detail_thread_guard

        def worker():
            try:
                text = self.backend.info(self._target(p)) if self.backend else ""
            except Exception as e:  # noqa: BLE001
                text = f"info failed: {e}"
            GLib.idle_add(self._apply_detail, guard, text)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_detail(self, guard: int, text: str) -> bool:
        if guard == self._detail_thread_guard:  # 丢弃过期线程结果，防串台
            self._set_detail(text)
        return False

    def _set_detail(self, text: str) -> None:
        self.detail_view.get_buffer().set_text(text)

    def _on_about(self, *_a) -> None:
        dlg = Gtk.AlertDialog.new(f"PkgPurge {__version__}")
        helper = "已安装" if find_helper() else "未找到"
        dlg.set_detail(
            "跨发行版 Linux 软件包卸载器\n"
            f"后端: {self.backend.label if self.backend else '—'}   系统: {self.distro}\n"
            f"pkexec helper: {helper}\n\n"
            "GPL-3.0-or-later · https://github.com/huanxiang666/pkgpurge")
        dlg.show(self)

    # ---------- 卸载流程 ----------

    def _start_removal(self, purge: bool) -> None:
        if not self.checked:
            dlg = Gtk.AlertDialog.new("请先勾选要卸载的软件包")
            dlg.show(self)
            return
        targets = sorted(self.checked.keys())
        self.btn_remove.set_sensitive(False)
        self.status.set_label(_("模拟预览…"))

        def worker():
            will_remove = targets
            err = None
            if self.backend and self.backend.supports_simulate:
                try:
                    will_remove = self.backend.simulate_removal(targets, purge=purge)
                except Exception as e:  # noqa: BLE001
                    err = str(e)
            GLib.idle_add(self._confirm_removal, targets, will_remove, purge, err)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_removal(self, targets, will_remove, purge, sim_error) -> bool:
        self.btn_remove.set_sensitive(True)
        self._update_status()
        if not will_remove and not sim_error:
            dlg = Gtk.AlertDialog.new("没有需要卸载的软件包")
            dlg.set_detail("所选软件包未安装，或仅剩配置残留（apt 可用「清除配置并卸载」清理）。")
            dlg.show(self)
            return False
        head = _("将卸载 {n} 个软件包（含依赖级联）：").format(n=len(will_remove))
        detail = format_removal_preview(will_remove, targets)
        if sim_error:
            detail = f"(模拟失败: {sim_error}，按勾选列表执行)\n" + detail
        dlg = Gtk.AlertDialog.new(head)
        dlg.set_detail(detail[:12000])
        verb = _("清除配置并卸载") if purge else _("卸载")
        dlg.set_buttons([_("取消"), verb])
        dlg.set_default_button(0)
        dlg.choose(self, None, self._on_confirm_choice, targets, purge)
        return False

    def _on_confirm_choice(self, dlg: Gtk.AlertDialog, result, targets, purge) -> None:
        try:
            choice = dlg.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        ops = self.backend.removal_ops(targets, purge=purge)
        self._execute_ops(ops)

    def _start_autoremove(self) -> None:
        self.btn_auto.set_sensitive(False)

        def worker():
            preview = []
            try:
                preview = self.backend.autoremove_preview()
            except Exception:  # noqa: BLE001 — 预览失败不阻断，交给正式命令自己报告
                preview = []
            GLib.idle_add(self._confirm_autoremove, preview)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_autoremove(self, preview) -> bool:
        self.btn_auto.set_sensitive(True)
        detail = "\n".join(f"  • {n}" for n in preview[:MAX_PREVIEW_ROWS]) or \
            "(后端未提供预览列表，将执行其原生 autoremove)"
        dlg = Gtk.AlertDialog.new("清理不再被需要的依赖软件包？")
        dlg.set_detail(detail[:12000])
        dlg.set_buttons([_("取消"), _("清理")])
        dlg.set_default_button(0)
        dlg.choose(self, None, self._on_autoremove_choice)
        return False

    def _on_autoremove_choice(self, dlg, result) -> None:
        try:
            choice = dlg.choose_finish(result)
        except GLib.Error:
            return
        if choice == 1:
            self._execute_ops(self.backend.autoremove_ops())

    def _execute_ops(self, ops) -> None:
        prog = ProgressWindow(self)
        prog.present()
        prog.append_line(_("需要 root 权限，请在弹出的对话框中输入密码。"))

        def worker():
            res = elevated_run(
                ops, line_cb=lambda l: GLib.idle_add(prog.append_line, l))
            GLib.idle_add(self._on_ops_done, prog, res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ops_done(self, prog: ProgressWindow, res) -> bool:
        prog.finish()
        if res.ok:
            dlg = Gtk.AlertDialog.new(_("卸载完成"))
            dlg.set_detail(res.message)
            dlg.show(self)
            self.checked.clear()
            self.reload_packages()
        elif res.cancelled:
            dlg = Gtk.AlertDialog.new(_("卸载被取消（未通过身份验证）"))
            dlg.show(self)
        else:
            dlg = Gtk.AlertDialog.new(_("卸载失败"))
            dlg.set_detail(str(res.data or res.message)[-4000:])
            dlg.show(self)
            self.reload_packages()
        return False

    # ---------- selftest（headless 验收） ----------

    def _run_selftest_checks(self) -> None:
        try:
            n_rows = len(self._rows_by_target)
            assert n_rows > 0, "no package rows rendered"
            first = self.all_pkgs[0]
            target = self._target(first)
            assert target in self._rows_by_target, "row lookup broken"
            # 勾选/取消勾选一致性
            self.checked[target] = first
            self._update_status()
            assert len(self.checked) >= 1
            del self.checked[target]
            # 预览文本生成
            preview = format_removal_preview([first.name], [first.name])
            assert first.name in preview
            # 命令生成（不执行）
            ops = self.backend.removal_ops([target])
            assert ops and isinstance(ops[0], list)
            # helper 白名单同源校验
            from .helper import validate_ops
            assert validate_ops(ops) is None, f"own ops rejected by helper: {ops}"
            if self.backend.supports_autoremove:
                aops = self.backend.autoremove_ops()
                assert validate_ops(aops) is None, f"autoremove ops rejected: {aops}"
            self._selftest_done(ok=True, rows=n_rows, backend=self.backend.id,
                                distro=self.distro, sample=target,
                                ops=ops, version=__version__)
        except Exception as e:  # noqa: BLE001 — selftest 任何失败都要显式报出
            self._selftest_done(ok=False, error=str(e))

    def _selftest_done(self, ok: bool, **info) -> None:
        print(json.dumps({"selftest": ok, **info}, ensure_ascii=False), flush=True)
        app = self.get_application()
        if app is not None:
            app.selftest_ok = ok  # type: ignore[attr-defined]
        self.close()
        if app:
            app.quit()


class PkgPurgeApp(Gtk.Application):
    def __init__(self, selftest: bool = False):
        super().__init__(application_id=APP_ID)
        self.selftest = selftest
        self.selftest_ok: bool | None = None
        self._watchdog_fired = False

    def do_activate(self) -> None:
        win = MainWindow(self, selftest=self.selftest)
        win.present()
        if self.selftest:
            # 看门狗：60 秒没完成即判失败，防挂死 CI/验收
            GLib.timeout_add_seconds(60, self._watchdog)

    def _watchdog(self) -> bool:
        if self._watchdog_fired:
            return False
        self._watchdog_fired = True
        if self.selftest_ok is None:
            self.selftest_ok = False
            print(json.dumps({"selftest": False, "error": "watchdog timeout (60s)"}),
                  flush=True)
        self.quit()
        return False


def run_gui() -> int:
    return PkgPurgeApp(selftest=False).run(None)


def run_selftest() -> int:
    app = PkgPurgeApp(selftest=True)
    app.run(None)
    return 0 if app.selftest_ok else 1
