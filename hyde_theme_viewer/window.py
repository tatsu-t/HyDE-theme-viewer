from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from . import catalog  # noqa: E402
from .catalog import GitHubError, ThemeEntry  # noqa: E402

THUMB_SIZE = 220
PREVIEW_MAX = 900
DOWNLOAD_WORKERS = 6


def _texture_from_file(path, size) -> Gdk.Texture | None:
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size, size, True)
        return Gdk.Texture.new_for_pixbuf(pixbuf)
    except GLib.Error:
        return None


def _fetch_and_cache_images(entry: ThemeEntry, remote_items: list[dict], on_image) -> int:
    """Download (or reuse cached copies of) a list of {'name','download_url'}
    entries in parallel, calling on_image(path) as soon as each one finishes
    (in completion order, not submission order). Returns how many succeeded."""
    if not remote_items:
        return 0

    def _one(item):
        dest = catalog.cached_image_path(entry.name, item["download_url"])
        return catalog.download_image(item["download_url"], dest)

    ok = 0
    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(_one, item) for item in remote_items]
        for future in as_completed(futures):
            path = future.result()
            if path is not None:
                ok += 1
                on_image(path)
    return ok


class LogDialog(Adw.Window):
    """A scrollable, copyable full-text log viewer -- used for anything too
    long or too technical for a toast (subprocess output, tracebacks)."""

    def __init__(self, parent: Gtk.Window, title: str, text: str) -> None:
        super().__init__(transient_for=parent, modal=True)
        self.set_default_size(760, 520)

        buffer = Gtk.TextBuffer()
        buffer.set_text(text or "(no output)")
        text_view = Gtk.TextView(buffer=buffer, editable=False, monospace=True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_top_margin(10)
        text_view.set_bottom_margin(10)
        text_view.set_left_margin(10)
        text_view.set_right_margin(10)
        scroller = Gtk.ScrolledWindow(child=text_view)

        copy_button = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy to clipboard")
        copy_button.connect("clicked", lambda _b: self.get_clipboard().set(text))

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=title))
        header.pack_end(copy_button)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(scroller)
        self.set_content(toolbar_view)


class ImageDialog(Adw.Window):
    def __init__(self, parent: Gtk.Window, path) -> None:
        super().__init__(transient_for=parent, modal=True)
        self.set_default_size(1000, 750)
        picture = Gtk.Picture()
        texture = _texture_from_file(path, PREVIEW_MAX)
        if texture is not None:
            picture.set_paintable(texture)
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())
        toolbar_view.set_content(picture)
        self.set_content(toolbar_view)


class ImageFlow(Gtk.FlowBox):
    def __init__(self, on_activate) -> None:
        super().__init__()
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.set_max_children_per_line(6)
        self.set_row_spacing(8)
        self.set_column_spacing(8)
        self._on_activate = on_activate

    def clear(self) -> None:
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt

    def add_image(self, path) -> None:
        texture = _texture_from_file(path, THUMB_SIZE)
        if texture is None:
            return
        picture = Gtk.Picture(paintable=texture)
        picture.set_size_request(THUMB_SIZE, THUMB_SIZE)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        button = Gtk.Button(child=picture)
        button.add_css_class("flat")
        button.connect("clicked", lambda _b, p=path: self._on_activate(p))
        self.append(button)

    def set_images(self, paths) -> None:
        self.clear()
        for path in paths:
            self.add_image(path)


class ThemeRow(Gtk.Box):
    def __init__(self, entry: ThemeEntry) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.entry = entry
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(10)
        self.set_margin_end(10)
        label = Gtk.Label(label=entry.name, xalign=0)
        label.add_css_class("heading")
        self.append(label)
        if entry.description:
            desc = Gtk.Label(label=entry.description, xalign=0)
            desc.add_css_class("dim-label")
            desc.add_css_class("caption")
            desc.set_wrap(True)
            desc.set_lines(2)
            desc.set_ellipsize(3)  # Pango.EllipsizeMode.END
            self.append(desc)


class HydeThemeWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="HyDE Theme Viewer")
        self.set_default_size(1200, 800)

        self.entries: list[ThemeEntry] = []
        self.selected_entry: ThemeEntry | None = None
        self._load_generation = 0

        split = Adw.OverlaySplitView()
        split.set_sidebar_width_fraction(0.3)

        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload catalog")
        refresh_button.connect("clicked", lambda _b: self._load_catalog())
        sidebar_header.pack_end(refresh_button)
        sidebar_toolbar.add_top_bar(sidebar_header)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_row_selected)

        self.sidebar_stack = Gtk.Stack()
        sidebar_spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, spacing=8)
        sidebar_spinner_box.append(Adw.Spinner())
        sidebar_spinner_box.append(Gtk.Label(label="Loading theme catalog…"))
        self.sidebar_stack.add_named(sidebar_spinner_box, "loading")
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.listbox)
        self.sidebar_stack.add_named(scroller, "list")
        self.sidebar_error = Adw.StatusPage(
            title="Couldn't load catalog",
            icon_name="dialog-warning-symbolic",
        )
        self._catalog_error_text = ""
        show_log_button = Gtk.Button(label="Show details", halign=Gtk.Align.CENTER)
        show_log_button.connect(
            "clicked", lambda _b: LogDialog(self, "Couldn't load catalog", self._catalog_error_text).present()
        )
        self.sidebar_error.set_child(show_log_button)
        self.sidebar_stack.add_named(self.sidebar_error, "error")

        sidebar_toolbar.set_content(self.sidebar_stack)
        split.set_sidebar(sidebar_toolbar)

        content_toolbar = Adw.ToolbarView()
        content_header = Adw.HeaderBar()
        self.content_title = Adw.WindowTitle(title="HyDE Theme Viewer")
        content_header.set_title_widget(self.content_title)
        self.apply_button = Gtk.Button(label="Apply this theme")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.set_sensitive(False)
        self.apply_button.connect("clicked", self._on_apply_clicked)
        content_header.pack_end(self.apply_button)
        content_toolbar.add_top_bar(content_header)

        self.hyde_missing = not catalog.THEME_PATCH_SH.is_file()
        if self.hyde_missing:
            banner = Adw.Banner(
                title=f"HyDE not found ({catalog.THEME_PATCH_SH}) — applying a theme is disabled",
                revealed=True,
            )
            content_toolbar.add_top_bar(banner)

        self.toast_overlay = Adw.ToastOverlay()
        self.stack = Gtk.Stack()

        placeholder = Adw.StatusPage(
            title="Select a theme",
            description="Pick a theme on the left to fetch and preview its images",
            icon_name="applications-graphics-symbolic",
        )
        self.stack.add_named(placeholder, "placeholder")

        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        detail_box.set_margin_top(16)
        detail_box.set_margin_bottom(16)
        detail_box.set_margin_start(16)
        detail_box.set_margin_end(16)

        self.info_label = Gtk.Label(xalign=0, wrap=True)
        self.info_label.add_css_class("dim-label")
        detail_box.append(self.info_label)

        self.screenshot_group = Adw.PreferencesGroup(title="Screenshots")
        self.screenshot_flow = ImageFlow(self._open_image)
        self.screenshot_group.add(self.screenshot_flow)

        self.wallpaper_group = Adw.PreferencesGroup(title="Wallpapers")
        self.wallpaper_flow = ImageFlow(self._open_image)
        self.wallpaper_group.add(self.wallpaper_flow)

        detail_scroller = Gtk.ScrolledWindow()
        detail_box.append(self.screenshot_group)
        detail_box.append(self.wallpaper_group)
        detail_scroller.set_child(detail_box)
        self.stack.add_named(detail_scroller, "detail")

        self.stack.set_visible_child_name("placeholder")
        self.toast_overlay.set_child(self.stack)
        content_toolbar.set_content(self.toast_overlay)
        split.set_content(content_toolbar)

        self.set_content(split)

        self._load_catalog()

    # -- catalog loading -------------------------------------------------

    def _load_catalog(self) -> None:
        self._load_generation += 1
        generation = self._load_generation
        self.sidebar_stack.set_visible_child_name("loading")
        threading.Thread(target=self._load_catalog_thread, args=(generation,), daemon=True).start()

    def _load_catalog_thread(self, generation: int) -> None:
        try:
            entries = catalog.fetch_catalog()
            GLib.idle_add(self._on_catalog_loaded, generation, entries, None)
        except GitHubError as exc:
            GLib.idle_add(self._on_catalog_loaded, generation, None, str(exc))
        except Exception:
            GLib.idle_add(self._on_catalog_loaded, generation, None, traceback.format_exc())

    def _on_catalog_loaded(self, generation: int, entries, error: str | None) -> bool:
        if generation != self._load_generation:
            return False
        if error is not None:
            self._catalog_error_text = error
            self.sidebar_error.set_description(error.splitlines()[-1] if error else error)
            self.sidebar_stack.set_visible_child_name("error")
            return False
        self.entries = entries
        row = self.listbox.get_row_at_index(0)
        while row is not None:
            self.listbox.remove(row)
            row = self.listbox.get_row_at_index(0)
        for entry in entries:
            list_row = Gtk.ListBoxRow()
            list_row.set_child(ThemeRow(entry))
            self.listbox.append(list_row)
        self.sidebar_stack.set_visible_child_name("list")
        return False

    # -- selection & loading -------------------------------------------------

    def _on_row_selected(self, _listbox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self.apply_button.set_sensitive(False)
            self.content_title.set_title("HyDE Theme Viewer")
            self.stack.set_visible_child_name("placeholder")
            return
        theme_row: ThemeRow = row.get_child()
        entry = theme_row.entry
        self.selected_entry = entry
        self.apply_button.set_sensitive(not self.hyde_missing)
        self.content_title.set_title(entry.name)
        self.screenshot_group.set_title(f"Screenshots  ·  {catalog.GALLERY_REPO}/{entry.name}")
        theme_path = catalog.theme_repo_path(entry.name)
        self.wallpaper_group.set_title(f"Wallpapers  ·  …/{theme_path.split('hyde/', 1)[1]}")

        info_bits = []
        if entry.description:
            info_bits.append(entry.description)
        if entry.owner:
            info_bits.append(f"by {entry.owner}")
        self.info_label.set_label("  •  ".join(info_bits))

        self.screenshot_flow.clear()
        self.wallpaper_flow.clear()
        self.screenshot_group.set_description("Fetching screenshots…")
        self.wallpaper_group.set_description("Fetching wallpapers…")
        self.stack.set_visible_child_name("detail")

        threading.Thread(
            target=self._fetch_image_group,
            args=(entry, catalog.fetch_screenshots, self.screenshot_flow, self.screenshot_group, "screenshots"),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._fetch_image_group,
            args=(entry, catalog.fetch_wallpapers, self.wallpaper_flow, self.wallpaper_group, "wallpapers"),
            daemon=True,
        ).start()

    def _fetch_image_group(
        self, entry: ThemeEntry, fetch_listing, flow: "ImageFlow", group: Adw.PreferencesGroup, noun: str
    ) -> None:
        """List a theme's images from GitHub, then download them in parallel,
        showing each one in `flow` the moment it's ready rather than waiting
        for the whole set."""
        error = None
        count = 0
        try:
            items = fetch_listing(entry)
            count = _fetch_and_cache_images(
                entry, items, lambda path: GLib.idle_add(self._on_image_ready, entry, flow, path)
            )
        except GitHubError as exc:
            error = str(exc)
        except Exception:
            error = traceback.format_exc()
        GLib.idle_add(self._on_image_group_done, entry, group, noun, count, error)

    def _on_image_ready(self, entry: ThemeEntry, flow: "ImageFlow", path) -> bool:
        if self.selected_entry is entry:
            flow.add_image(path)
        return False

    def _on_image_group_done(self, entry: ThemeEntry, group: Adw.PreferencesGroup, noun: str, count: int, error: str | None) -> bool:
        if self.selected_entry is not entry:
            return False
        if error:
            group.set_description(f"Couldn't fetch {noun}: {error}")
        else:
            group.set_description(None if count else f"No {noun} found for this theme")
        return False

    def _open_image(self, path) -> None:
        ImageDialog(self, path).present()

    # -- apply ----------------------------------------------------------------

    def _on_apply_clicked(self, _button) -> None:
        entry = self.selected_entry
        if entry is None:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Apply this theme?",
            body=f"This will apply “{entry.name}” to HyDE right now, replacing your current desktop theme.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_apply_dialog_response, entry)
        dialog.present()

    def _on_apply_dialog_response(self, _dialog, response: str, entry: ThemeEntry) -> None:
        if response != "apply":
            return
        self.apply_button.set_sensitive(False)
        self.toast_overlay.add_toast(Adw.Toast(title=f"Applying “{entry.name}”…"))
        threading.Thread(target=self._apply_theme_thread, args=(entry,), daemon=True).start()

    def _apply_theme_thread(self, entry: ThemeEntry) -> None:
        try:
            ok, output = catalog.apply_theme(entry)
        except Exception:
            GLib.idle_add(self._on_apply_crashed, traceback.format_exc(), entry)
            return
        GLib.idle_add(self._on_apply_done, ok, output, entry)

    def _on_apply_done(self, ok: bool, output: str, entry: ThemeEntry) -> bool:
        self.apply_button.set_sensitive(not self.hyde_missing)
        if ok:
            self.toast_overlay.add_toast(Adw.Toast(title=f"Applied “{entry.name}”"))
        else:
            self.toast_overlay.add_toast(Adw.Toast(title=f"Failed to apply “{entry.name}”"))
            LogDialog(self, f"Apply failed — {entry.name}", output).present()
        return False

    def _on_apply_crashed(self, tb: str, entry: ThemeEntry) -> bool:
        self.apply_button.set_sensitive(not self.hyde_missing)
        self.toast_overlay.add_toast(Adw.Toast(title=f"Failed to apply “{entry.name}”"))
        LogDialog(self, f"Apply crashed — {entry.name}", tb).present()
        return False


class HydeThemeApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="jp.tatsut.hyde-theme-viewer")

    def do_activate(self) -> None:
        win = self.props.active_window
        if win is None:
            win = HydeThemeWindow(self)
        win.present()
