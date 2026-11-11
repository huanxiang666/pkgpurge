"""GTK4 图形界面：软件包列表 + 多选卸载 + 依赖级联模拟预览 + pkexec 提权执行.

线程模型：所有包管理器调用（list/info/simulate/elevated_run）都在 worker
线程执行，结果通过 GLib.idle_add 回主线程刷新 UI，界面不冻结。

对话框兼容层：Gtk.AlertDialog 需要 GTK ≥ 4.10；对更老的 GTK4（如
Ubuntu 22.04 的 4.6）自动降级到 Gtk.MessageDialog（B4）。
"""

from __future__ import annotations

import json
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gio, Gtk, Pango  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .backends import detect_backend, distro_pretty  # noqa: E402
from .backends.base import Package, human_size  # noqa: E402
from .i18n import _  # noqa: E402
from .priv import elevated_run, find_helper  # noqa: E402

MAX_PREVIEW_ROWS = 400

_HAS_ALERT = hasattr(Gtk, "AlertDialog")


class Dialogs:
    """AlertDialog(≥4.10) / MessageDialog(4.6+) 双路径封装.

    choose 的回调统一收到 int：按钮索引；窗口被关闭/取消时为 -1。
    """

    @staticmethod
    def show(parent: Gtk.Window, message: str, detail: str | None = None) -> None:
        if _HAS_ALERT:
            dlg = Gtk.AlertDialog(message=message)
            if detail:
                dlg.set_detail(detail[:12000])
            dlg.show(parent)
        else:
            dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                    message_type=Gtk.MessageType.INFO,
                                    buttons=Gtk.ButtonsType.OK, text=message)
            if detail:
                dlg.set_property("secondary-text", detail[:4000])
            dlg.connect("response", lambda w, _r: w.destroy())
            dlg.present()

    @staticmethod
    def choose(parent: Gtk.Window, message: str, detail: str | None,
               buttons: list[str], default_index: int, cb) -> None:
        if _HAS_ALERT:
            dlg = Gtk.AlertDialog(message=message)
            if detail:
                dlg.set_detail(detail[:12000])
            dlg.set_buttons(buttons)
            dlg.set_default_button(default_index)

            def finish(dialog, result):
                try:
                    choice = dialog.choose_finish(result)
                except GLib.Error:
                    choice = -1
                cb(choice)

            dlg.choose(parent, None, finish)
        else:
            dlg = Gtk.MessageDialog(transient_for=parent, modal=True,
                                    message_type=Gtk.MessageType.WARNING,
                                    text=message)
            if detail:
                dlg.set_property("secondary-text", detail[:4000])
            for i, label in enumerate(buttons):
                dlg.add_button(label, i)
            dlg.set_default_response(default_index)

            def response(w, r):
                w.destroy()
                cb(r if 0 <= r < len(buttons) else -1)

            dlg.connect("response", response)
            dlg.present()


def format_removal_preview(will_remove: list[str], requested: list[str],
                           parsed_ok: bool = True) -> str:
    """确认对话框正文：区分「你勾选的」和「被依赖级联带走的」.

    parsed_ok=False 时首行给出显著警示（B2：回退名单不得冒充级联结果）。
    """
    lines = []
    if not parsed_ok:
        lines.append("⚠ " + _("依赖级联解析失败：以下仅为你的勾选清单，实际移除可能包含更多依赖"))
        lines.append("")
    extra = [n for n in will_remove if n not in requested]
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
        self._loading = False

        self._build_ui()
        if self.backend is None:
            GLib.idle_add(self._fatal_no_backend)
        else:
            self.reload_packages()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        title = Gtk.Label(
            label=f"{self.backend.label if self.backend else '—'} · {self.distro}")
        hb.set_title_widget(title)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("搜索软件包…"))
        self.search.set_width_chars(28)
        self.search.connect("search-changed", self._on_search_changed)
        hb.pack_start(self.search)

        self.btn_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.btn_refresh.set_tooltip_text(_("刷新"))
        self.btn_refresh.connect("clicked", lambda *_: self.reload_packages())
        hb.pack_end(self.btn_refresh)

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

        if self.backend is not None:  # N19：无后端时不渲染任何动作按钮
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            right.append(actions)

            self.btn_remove = Gtk.Button.new_with_label(_("卸载所选"))
            self.btn_remove.add_css_class("destructive-action")
            self.btn_remove.connect("clicked", lambda *_: self._start_removal(purge=False))
            actions.append(self.btn_remove)

            if self.backend.supports_purge:
                btn_purge = Gtk.Button.new_with_label(_("清除配置并卸载"))
                btn_purge.connect("clicked", lambda *_: self._start_removal(purge=True))
                actions.append(btn_purge)

            if self.backend.supports_autoremove:
                self.btn_auto = Gtk.Button.new_with_label(_("清理孤立依赖"))
                self.btn_auto.connect("clicked", self._start_autoremove)
                actions.append(self.btn_auto)

        # 底部状态栏
        self.status = Gtk.Label(label=_("正在加载软件包列表…"))
        self.status.set_xalign(0.0)
        self.status.add_css_class("dim-label")
        root.append(self.status)

    def _fatal_no_backend(self) -> bool:
        Dialogs.show(self, _("本机未检测到受支持的包管理器"),
                     "需要 apt-get / dnf / zypper / pacman / apk 之一。")
        return False

    # ---------- 数据加载 ----------

    def reload_packages(self) -> None:
        if self.backend is None or self._loading:
            return  # N14：加载期间防连点起多个 worker
        self._loading = True
        self.status.set_label(_("正在加载软件包列表…"))
        self.listbox.set_sensitive(False)
        self.btn_refresh.set_sensitive(False)

        def worker():
            try:
                pkgs = self.backend.list_installed()
                GLib.idle_add(self._on_loaded, pkgs, None)
            except Exception as e:  # noqa: BLE001 — 后端异常必须显式呈现给用户
                GLib.idle_add(self._on_loaded, [], e)

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, pkgs: list[Package], error) -> bool:
        self._loading = False
        self.listbox.set_sensitive(True)
        self.btn_refresh.set_sensitive(True)
        if error is not None:
            self.status.set_label(f"加载失败: {error}")
            Dialogs.show(self, _("加载软件包列表失败"), str(error))
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
        # LIMIT: 几千包一次性建 row，首屏约数百 ms；如成为瓶颈改为分批 idle_add
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
        Dialogs.show(
            self, f"PkgPurge {__version__}",
            _("跨发行版 Linux 软件包卸载器") + "\n"
            + f"{_('后端')}: {self.backend.label if self.backend else '—'}   "
            + f"{_('系统')}: {self.distro}\n"
            + f"pkexec helper: {'已安装' if find_helper() else '未找到'}\n\n"
            + "GPL-3.0-or-later · https://github.com/huanxiang666/pkgpurge")

    # ---------- 卸载流程 ----------

    def _start_removal(self, purge: bool) -> None:
        if not self.checked:
            Dialogs.show(self, _("请先勾选要卸载的软件包"))
            return
        targets = sorted(self.checked.keys())
        self.btn_remove.set_sensitive(False)
        self.status.set_label(_("模拟预览…"))

        def worker():
            will_remove, parsed_ok = targets, True
            err = None
            if self.backend and self.backend.supports_simulate:
                try:
                    will_remove, parsed_ok = self.backend.simulate_removal(
                        targets, purge=purge)
                except Exception as e:  # noqa: BLE001
                    err = str(e)
            GLib.idle_add(self._confirm_removal, targets, will_remove, purge,
                          err, parsed_ok)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_removal(self, targets, will_remove, purge, sim_error,
                         parsed_ok) -> bool:
        self.btn_remove.set_sensitive(True)
        self._update_status()
        if not will_remove and parsed_ok and not sim_error:
            Dialogs.show(self, _("没有需要卸载的软件包"),
                         _("所选软件包未安装，或仅剩配置残留（apt 可用「清除配置并卸载」清理）。"))
            return False
        head = _("将卸载 {n} 个软件包（含依赖级联）：").format(n=len(will_remove))
        detail = format_removal_preview(will_remove, targets, parsed_ok)
        if sim_error:
            detail = f"(模拟失败: {sim_error}，按勾选列表执行)\n\n" + detail
        verb = _("清除配置并卸载") if purge else _("卸载")
        Dialogs.choose(self, head, detail, [_("取消"), verb], 0,
                       lambda choice: self._on_confirm_choice(choice, targets, purge))
        return False

    def _on_confirm_choice(self, choice: int, targets, purge) -> None:
        if choice != 1:
            return
        try:
            ops = self.backend.removal_ops(targets, purge=purge)
        except Exception as e:  # noqa: BLE001 — B3：破坏性动作的失败必须可见
            Dialogs.show(self, _("无法构造卸载命令"), str(e))
            return
        self._execute_ops(ops)

    def _start_autoremove(self) -> None:
        self.btn_auto.set_sensitive(False)

        def worker():
            try:
                preview = self.backend.autoremove_preview()
            except Exception:  # noqa: BLE001 — 预览失败不阻断，正式命令自己会报告
                preview = []
            GLib.idle_add(self._confirm_autoremove, preview)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_autoremove(self, preview) -> bool:
        self.btn_auto.set_sensitive(True)
        detail = "\n".join(f"  • {n}" for n in preview[:MAX_PREVIEW_ROWS]) or \
            _("(后端未提供预览列表，将执行其原生 autoremove)")
        Dialogs.choose(self, _("清理不再被需要的依赖软件包？"), detail,
                       [_("取消"), _("清理")], 0, self._on_autoremove_choice)
        return False

    def _on_autoremove_choice(self, choice: int) -> None:
        if choice != 1:
            return
        try:
            ops = self.backend.autoremove_ops()
        except Exception as e:  # noqa: BLE001 — B3：含 NotImplementedError
            Dialogs.show(self, _("无法构造卸载命令"), str(e))
            return
        self._execute_ops(ops)

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
            Dialogs.show(self, _("卸载完成"), res.message)
            self.checked.clear()
            self.reload_packages()
        elif res.cancelled:
            Dialogs.show(self, _("卸载被取消（未通过身份验证）"))
        else:
            Dialogs.show(self, _("卸载失败"), str(res.data or res.message)[-4000:])
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
            # 预览文本生成（含 parsed_ok=False 警示路径）
            preview = format_removal_preview([first.name], [first.name])
            assert first.name in preview
            warn = format_removal_preview([first.name], [first.name], parsed_ok=False)
            assert "⚠" in warn
            # 命令生成（不执行）
            ops = self.backend.removal_ops([target])
            assert ops and isinstance(ops[0], list)
            # helper 白名单同源校验
            from .helper import validate_ops
            assert validate_ops(ops) is None, f"own ops rejected by helper: {ops}"
            if self.backend.supports_autoremove:
                aops = self.backend.autoremove_ops()
                assert validate_ops(aops) is None, f"autoremove ops rejected: {aops}"
            # 模拟接口契约：必须返回 (list, bool)
            if self.backend.supports_simulate:
                sim = self.backend.simulate_removal([target])
                assert isinstance(sim, tuple) and len(sim) == 2 \
                    and isinstance(sim[0], list) and isinstance(sim[1], bool), \
                    f"simulate_removal contract broken: {sim!r}"
            # 对话框兼容层：两条路径的构造器参数都必须成立（只建不弹）
            md = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.WARNING,
                                   text="selftest")
            md.set_property("secondary-text", "detail")
            md.add_button(_("取消"), 0)
            md.add_button(_("卸载"), 1)
            md.set_default_response(0)
            md.destroy()
            if _HAS_ALERT:
                ad = Gtk.AlertDialog(message="selftest")
                ad.set_detail("detail")
                ad.set_buttons([_("取消"), _("卸载")])
                ad.set_default_button(0)
            self._selftest_done(ok=True, rows=n_rows, backend=self.backend.id,
                                distro=self.distro, sample=target,
                                dialogs="AlertDialog" if _HAS_ALERT else "MessageDialog",
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
        # NON_UNIQUE：selftest/CI 环境无 D-Bus 会话总线时不做总线注册，
        # 避免 g_application_register 在残缺环境下阻塞（22.04 runner 实测挂起点）
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
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
    # 双保险看门狗：GLib 定时器在主循环里；faulthandler 在解释器层，
    # 主循环卡死（如 D-Bus 注册阻塞）时 90s 后打 traceback 并强制退出
    import faulthandler
    faulthandler.dump_traceback_later(90, exit=True)
    app = PkgPurgeApp(selftest=True)
    app.run(None)
    faulthandler.cancel_dump_traceback_later()
    return 0 if app.selftest_ok else 1
