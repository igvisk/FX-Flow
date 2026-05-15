from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path

import flet as ft

from fx_core import (
    APP_NAME,
    APP_VERSION,
    AUTHOR,
    DEFAULT_CURRENCIES,
    DEFAULT_LANGUAGE,
    GITHUB,
    LANGUAGE_CODES,
    LANGUAGE_SHORT_LABELS,
    AppPreferences,
    LocaleManager,
    PreferencesStore,
    RateSnapshot,
    RatesCache,
    RatesProvider,
    convert_amount,
    currency_label,
    currency_name,
    format_amount_grouped,
    format_input_amount_grouped,
    parse_amount,
    rate_for,
)

PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"

BG_TOP = "#0A121A"
BG_BOTTOM = "#16232E"
SURFACE = "#13202B"
SURFACE_ALT = "#1A2B39"
BORDER = "#2F4355"
ACCENT = "#E3C75F"
TEXT = "#F4F7FA"
MUTED = "#A6B7C6"
SUCCESS = "#79D2A6"
WARNING = "#F4C95D"
ERROR = "#F19A9A"


class FXFlowApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.provider = RatesProvider()
        self.cache: RatesCache | None = None
        self.preferences_store: PreferencesStore | None = None
        self.preferences = AppPreferences()
        self.current_snapshot: RateSnapshot | None = None
        self.currencies: list[str] = list(DEFAULT_CURRENCIES)
        self.active_section_index = 0
        self.i18n = LocaleManager(PROJECT_DIR / "locales")
        self.status_key = "status.initializing"
        self.status_level = "loading"
        self.status_params: dict[str, str] = {}
        self._formatting_amount_input = False

    async def start(self) -> None:
        self._configure_page()
        storage_dir = await self._resolve_storage_dir()
        self.cache = RatesCache(storage_dir / "rates-cache.json")
        self.preferences_store = PreferencesStore(storage_dir / "preferences.json")
        self.cache.seed_from_legacy_file()
        self.preferences = self.preferences_store.load()
        self.preferences.language = self.i18n.normalize_language(self.preferences.language)
        self.i18n.set_language(self.preferences.language)
        self.current_snapshot = self.cache.get_snapshot(self.preferences.last_from)
        self.currencies = self._build_currency_list()
        self._normalize_preferences()
        self._render_interface()
        await self._show_initial_window()
        self._apply_snapshot(self.current_snapshot)
        self._update_favorite_views()
        self._recalculate()
        self._refresh_status_display()
        self.page.update()
        self.page.run_task(self.refresh_rates, self.preferences.last_from, False)

    def t(self, key: str, **kwargs: object) -> str:
        return self.i18n.t(key, **kwargs)

    def _configure_page(self) -> None:
        self.page.title = APP_NAME
        self.page.padding = 0
        self.page.spacing = 0
        self.page.bgcolor = BG_TOP
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
        self.page.theme = ft.Theme(
            color_scheme_seed=ACCENT,
            scrollbar_theme=ft.ScrollbarTheme(thumb_visibility=True),
        )
        if self.page.platform == ft.PagePlatform.WINDOWS:
            # Avoid toggling window.visible during normal startup. Newer Flet
            # desktop builds can leave the process alive with the window hidden.
            self.page.window.width = 420
            self.page.window.height = 720
        windows_icon_path = Path(__file__).resolve().parent / "assets" / "icon_windows.ico"
        if self.page.platform == ft.PagePlatform.WINDOWS and windows_icon_path.exists():
            self.page.window.icon = str(windows_icon_path)

    async def _show_initial_window(self) -> None:
        if self.page.platform != ft.PagePlatform.WINDOWS:
            return
        try:
            await self.page.window.wait_until_ready_to_show()
        except Exception:
            pass
        try:
            await self.page.window.center()
        except Exception:
            pass
        self.page.update()

    async def _resolve_storage_dir(self) -> Path:
        slug = APP_NAME.lower().replace(" ", "-")
        candidates: list[Path] = []
        if self.page.platform == ft.PagePlatform.WINDOWS:
            local_appdata = os.getenv("LOCALAPPDATA")
            if local_appdata:
                candidates.append(Path(local_appdata) / APP_NAME)
        elif self.page.platform == ft.PagePlatform.ANDROID:
            candidates.append(Path.home() / APP_NAME)
        else:
            candidates.append(Path.home() / f".{slug}")
        candidates.extend([Path.home() / ".fx-flow-data", Path.cwd() / ".fx-flow-data"])
        return self._ensure_writable_storage_dir(candidates)

    def _ensure_writable_storage_dir(self, candidates: list[Path]) -> Path:
        unique_candidates: list[Path] = []
        for candidate in candidates:
            if candidate not in unique_candidates:
                unique_candidates.append(candidate)
        for candidate in unique_candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return candidate
            except OSError:
                continue
        return Path.home()

    def _build_currency_list(self) -> list[str]:
        assert self.cache is not None
        currencies = self.cache.available_currencies()
        if self.current_snapshot:
            currencies = sorted(set(currencies) | set(self.current_snapshot.rates.keys()))
        return currencies or list(DEFAULT_CURRENCIES)

    def _normalize_preferences(self) -> None:
        self.preferences.language = self.i18n.normalize_language(
            self.preferences.language or DEFAULT_LANGUAGE
        )
        available = set(self.currencies)
        favorites = [code for code in self.preferences.favorites if code in available]
        if not favorites:
            favorites = [code for code in DEFAULT_CURRENCIES if code in available]
        if not favorites and self.currencies:
            favorites = [self.currencies[0]]
        self.preferences.favorites = list(dict.fromkeys(favorites))
        converter_currencies = self._converter_currencies()
        if self.preferences.last_from not in converter_currencies:
            self.preferences.last_from = converter_currencies[0]
        if self.preferences.last_to not in converter_currencies:
            targets = [code for code in converter_currencies if code != self.preferences.last_from]
            self.preferences.last_to = targets[0] if targets else converter_currencies[0]
        if (
            self.preferences.last_to == self.preferences.last_from
            and len(converter_currencies) > 1
        ):
            self.preferences.last_to = next(
                code for code in converter_currencies if code != self.preferences.last_from
            )
        self._save_preferences()

    def _capture_ui_state(self) -> None:
        if hasattr(self, "amount_input"):
            self.preferences.last_amount = self.amount_input.value or self.preferences.last_amount
        if hasattr(self, "from_dropdown"):
            self.preferences.last_from = self.from_dropdown.value or self.preferences.last_from
        if hasattr(self, "to_dropdown"):
            self.preferences.last_to = self.to_dropdown.value or self.preferences.last_to

    def _render_interface(self) -> None:
        self.page.clean()
        self._build_controls()
        self.page.navigation_bar = self.navigation_bar
        self.page.add(self._build_shell())

    def _build_controls(self) -> None:
        converter_options = [
            self._currency_code_option(code) for code in self._converter_currencies()
        ]
        amount_value = self._grouped_amount_value(self.preferences.last_amount)
        self.clear_amount_button = ft.IconButton(
            icon=ft.Icons.CLOSE_ROUNDED,
            icon_color=MUTED,
            icon_size=18,
            tooltip=self.t("converter.clear_amount"),
            width=36,
            height=36,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=999),
                padding=ft.Padding.all(4),
            ),
            visible=bool(amount_value.strip()),
            on_click=self._on_clear_amount,
        )
        self.amount_input = ft.TextField(
            label=self.t("converter.amount"),
            value=amount_value,
            hint_text=self.t("converter.amount_hint"),
            keyboard_type=ft.KeyboardType.NUMBER,
            border_radius=16,
            filled=True,
            fill_color=SURFACE_ALT,
            text_style=ft.TextStyle(size=18, weight=ft.FontWeight.W_600),
            content_padding=ft.Padding.symmetric(horizontal=18, vertical=18),
            suffix=self.clear_amount_button,
            on_change=self._on_amount_change,
            on_submit=self._on_calculate,
            on_blur=self._on_amount_blur,
        )
        self.from_dropdown = ft.Dropdown(
            label=self.t("converter.from"),
            value=self.preferences.last_from,
            options=converter_options,
            expand=True,
            filled=True,
            fill_color=SURFACE_ALT,
            border_radius=16,
            content_padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            on_select=self._on_from_currency_change,
        )
        self.to_dropdown = ft.Dropdown(
            label=self.t("converter.to"),
            value=self.preferences.last_to,
            options=[self._currency_code_option(code) for code in self._converter_currencies()],
            expand=True,
            filled=True,
            fill_color=SURFACE_ALT,
            border_radius=16,
            content_padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            on_select=self._on_to_currency_change,
        )
        self.favorite_dropdown = ft.Dropdown(
            label=self.t("favorites.add_label"),
            options=[self._currency_option(code) for code in self.currencies],
            value=self._favorite_dropdown_value(),
            filled=True,
            fill_color=SURFACE_ALT,
            border_radius=16,
            content_padding=ft.Padding.symmetric(horizontal=16, vertical=14),
        )
        self.result_value = ft.Text("0", size=34, weight=ft.FontWeight.W_700, color=TEXT)
        self.result_rate_text = ft.Text(
            self.t("result.rate_pending"),
            size=14,
            color=ACCENT,
            weight=ft.FontWeight.W_500,
        )
        self.rate_value = ft.Text(self.t("detail.rate_unavailable"), size=15, color=MUTED)
        self.rate_source_text = ft.Text(self.t("detail.source_unavailable"), size=14, color=MUTED)
        self.rate_pair_text = ft.Text(self.t("detail.selection_pending"), size=14, color=MUTED)
        self.detail_text = ft.Text(self.t("detail.pick_and_calculate"), size=14, color=MUTED)
        self.status_icon = ft.Icon(ft.Icons.SYNC_ROUNDED, color=ACCENT, size=16)
        self.status_value = ft.Text(self.t("status.initializing"), color=TEXT, size=13, no_wrap=False)
        self.status_chip = ft.Container(
            expand=True,
            bgcolor="#2A3139",
            border_radius=20,
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    self.status_icon,
                    ft.Container(expand=True, content=self.status_value),
                ],
            ),
        )
        self.loading_ring = ft.ProgressRing(visible=False, width=16, height=16, stroke_width=2)
        self.favorite_row = ft.Row(wrap=True, spacing=10, run_spacing=10)
        self.favorite_list = ft.Column(spacing=12)
        self.section_views = [
            self._build_converter_tab(),
            self._build_favorites_tab(),
            self._build_about_tab(),
        ]
        self.section_content = ft.Container(
            expand=True,
            content=self.section_views[self.active_section_index],
        )
        self.navigation_bar = ft.NavigationBar(
            selected_index=self.active_section_index,
            bgcolor=SURFACE,
            indicator_color="#263849",
            label_behavior=ft.NavigationBarLabelBehavior.ALWAYS_SHOW,
            elevation=0,
            border=ft.Border.only(top=ft.BorderSide(1, BORDER)),
            on_change=self._on_navigation_change,
            destinations=[
                ft.NavigationBarDestination(
                    icon=ft.Icons.CURRENCY_EXCHANGE_OUTLINED,
                    selected_icon=ft.Icons.CURRENCY_EXCHANGE,
                    label=self.t("nav.converter"),
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.STAR_OUTLINE_ROUNDED,
                    selected_icon=ft.Icons.STAR_ROUNDED,
                    label=self.t("nav.favorites"),
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.INFO_OUTLINE,
                    selected_icon=ft.Icons.INFO,
                    label=self.t("nav.about"),
                ),
            ],
        )

    def _build_shell(self) -> ft.Control:
        hero = ft.Container(
            border_radius=24,
            padding=ft.Padding.all(18),
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, 1),
                colors=["#213445", "#15222D", "#0E171F"],
            ),
            border=ft.Border.all(1, BORDER),
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
                controls=[
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=2, vertical=4),
                        content=self.result_value,
                    ),
                    self.result_rate_text,
                ],
            ),
        )
        return ft.Container(
            expand=True,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(0, -1),
                end=ft.Alignment(0, 1),
                colors=[BG_TOP, BG_BOTTOM],
            ),
            content=ft.SafeArea(
                ft.Container(
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=18, vertical=18),
                    content=ft.Column(
                        expand=True,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        spacing=20,
                        controls=[hero, self.section_content],
                    ),
                )
            ),
        )

    def _build_converter_tab(self) -> ft.Control:
        return ft.Column(
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=18,
            controls=[
                self._card(
                    self.t("converter.title"),
                    "",
                    [
                        self.amount_input,
                        ft.Row(
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                self.from_dropdown,
                                ft.IconButton(
                                    icon=ft.Icons.SWAP_HORIZ_ROUNDED,
                                    icon_color=ACCENT,
                                    icon_size=24,
                                    tooltip=self.t("converter.swap_tooltip"),
                                    style=ft.ButtonStyle(
                                        bgcolor=SURFACE_ALT,
                                        shape=ft.RoundedRectangleBorder(radius=16),
                                        side=ft.border.BorderSide(1, BORDER),
                                        padding=ft.Padding.all(14),
                                    ),
                                    on_click=self._on_swap,
                                ),
                                self.to_dropdown,
                            ],
                        ),
                        ft.Row(
                            spacing=12,
                            controls=[
                                ft.Container(
                                    expand=1,
                                    content=self._secondary_button(
                                        self.t("converter.refresh"),
                                        ft.Icons.REFRESH_ROUNDED,
                                        self._on_refresh,
                                    ),
                                ),
                                ft.Container(
                                    expand=1,
                                    content=self._primary_button(
                                        self.t("converter.calculate"),
                                        ft.Icons.CALCULATE_ROUNDED,
                                        self._on_calculate,
                                    ),
                                ),
                            ],
                        ),
                        ft.Row(
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.START,
                            controls=[
                                ft.Container(expand=1, content=self.status_chip),
                                self.loading_ring,
                            ],
                        ),
                    ],
                ),
                self._card(
                    self.t("converter.favorites_title"),
                    "",
                    [
                        self.favorite_row,
                        ft.TextButton(
                            self.t("converter.manage_favorites"),
                            icon=ft.Icons.ARROW_FORWARD_ROUNDED,
                            style=ft.ButtonStyle(color=ACCENT),
                            on_click=self._go_to_favorites_tab,
                        ),
                    ],
                ),
                self._card(
                    self.t("converter.details_title"),
                    self.t("converter.details_subtitle"),
                    [self.rate_value, self.rate_source_text, self.rate_pair_text, self.detail_text],
                ),
            ],
        )

    def _build_favorites_tab(self) -> ft.Control:
        return ft.Column(
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=18,
            controls=[
                self._card(
                    self.t("favorites.manage_title"),
                    self.t("favorites.manage_subtitle"),
                    [
                        self.favorite_dropdown,
                        ft.ResponsiveRow(
                            spacing=0,
                            run_spacing=12,
                            controls=[
                                self._primary_button(
                                    self.t("favorites.add_button"),
                                    ft.Icons.STAR_ROUNDED,
                                    self._on_add_favorite,
                                ),
                            ],
                        ),
                    ],
                ),
                self._card(
                    self.t("favorites.list_title"),
                    self.t("favorites.list_subtitle"),
                    [self.favorite_list],
                ),
                self._card(
                    self.t("favorites.language_title"),
                    self.t("favorites.language_subtitle"),
                    [self._language_selector()],
                ),
            ],
        )

    def _build_about_tab(self) -> ft.Control:
        return ft.Column(
            scroll=ft.ScrollMode.AUTO,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            spacing=18,
            controls=[
                self._card(
                    self.t("about.title"),
                    self.t("about.subtitle"),
                    [
                        self._info_row(self.t("about.app_label"), APP_NAME),
                        self._info_row(self.t("about.version_label"), APP_VERSION),
                        self._info_row(self.t("about.author_label"), AUTHOR),
                        self._info_row(self.t("about.github_label"), GITHUB),
                    ],
                ),
                self._card(
                    self.t("about.how_title"),
                    self.t("about.how_subtitle"),
                    [
                        ft.Text(
                            "\n".join(
                                [
                                    self.t("about.source_line"),
                                    self.t("about.mode_line"),
                                    self.t("about.converter_line"),
                                    self.t("about.storage_line"),
                                ]
                            ),
                            color=TEXT,
                            size=15,
                        )
                    ],
                ),
            ],
        )

    def _card(self, title: str, subtitle: str, controls: list[ft.Control]) -> ft.Control:
        return ft.Container(
            bgcolor=SURFACE,
            border_radius=24,
            border=ft.Border.all(1, BORDER),
            padding=ft.Padding.all(20),
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                spacing=16,
                controls=[
                    ft.Column(
                        spacing=6,
                        controls=[
                            ft.Text(title, size=22, weight=ft.FontWeight.W_700, color=TEXT),
                            *([ft.Text(subtitle, size=14, color=MUTED)] if subtitle else []),
                        ],
                    ),
                    *controls,
                ],
            ),
        )

    def _primary_button(self, label: str, icon: ft.IconData, handler) -> ft.FilledButton:
        return ft.FilledButton(
            label,
            icon=icon,
            style=ft.ButtonStyle(
                bgcolor=ACCENT,
                color="#1A1A1A",
                shape=ft.RoundedRectangleBorder(radius=16),
                padding=ft.Padding.symmetric(horizontal=20, vertical=18),
            ),
            on_click=handler,
        )

    def _secondary_button(self, label: str, icon: ft.IconData, handler) -> ft.OutlinedButton:
        return ft.OutlinedButton(
            label,
            icon=icon,
            style=ft.ButtonStyle(
                side=ft.border.BorderSide(1, BORDER),
                shape=ft.RoundedRectangleBorder(radius=16),
                padding=ft.Padding.symmetric(horizontal=20, vertical=18),
                color=TEXT,
            ),
            on_click=handler,
        )

    def _info_row(self, label: str, value: str) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=14),
            border_radius=18,
            bgcolor=SURFACE_ALT,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(label, color=MUTED, size=14),
                    ft.Container(
                        expand=True,
                        alignment=ft.Alignment.CENTER_RIGHT,
                        content=ft.Text(
                            value,
                            color=TEXT,
                            size=15,
                            weight=ft.FontWeight.W_600,
                            text_align=ft.TextAlign.RIGHT,
                        ),
                    ),
                ],
            ),
        )

    def _language_selector(self) -> ft.Control:
        rows: list[ft.Row] = []
        buttons = [self._language_button(code) for code in LANGUAGE_CODES]
        for index in range(0, len(buttons), 3):
            rows.append(
                ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=12,
                    controls=buttons[index : index + 3],
                )
            )
        return ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
            controls=rows,
        )

    def _language_button(self, code: str) -> ft.Control:
        label = LANGUAGE_SHORT_LABELS[code]
        tooltip = self.t(f"common.language_name.{code}")
        if self.preferences.language == code:
            return ft.FilledButton(
                label,
                tooltip=tooltip,
                width=96,
                height=56,
                style=ft.ButtonStyle(
                    bgcolor=ACCENT,
                    color="#1A1A1A",
                    shape=ft.RoundedRectangleBorder(radius=14),
                    padding=ft.Padding.symmetric(horizontal=10, vertical=14),
                ),
                data=code,
                on_click=self._on_language_change,
            )
        return ft.OutlinedButton(
            label,
            tooltip=tooltip,
            width=96,
            height=56,
            style=ft.ButtonStyle(
                side=ft.border.BorderSide(1, BORDER),
                color=TEXT,
                shape=ft.RoundedRectangleBorder(radius=14),
                padding=ft.Padding.symmetric(horizontal=10, vertical=14),
            ),
            data=code,
            on_click=self._on_language_change,
        )

    def _favorite_currencies(self) -> list[str]:
        return [code for code in self.preferences.favorites if code in self.currencies]

    def _currency_option(self, code: str) -> ft.dropdown.Option:
        return ft.dropdown.Option(code, text=currency_label(code))

    def _currency_code_option(self, code: str) -> ft.dropdown.Option:
        return ft.dropdown.Option(code, text=code)

    def _currency_display(self, code: str) -> str:
        return currency_label(code)

    def _converter_currencies(self) -> list[str]:
        favorites = self._favorite_currencies()
        if favorites:
            return favorites
        if self.currencies:
            return [self.currencies[0]]
        return list(DEFAULT_CURRENCIES[:1])

    def _sync_converter_currency_options(self) -> None:
        converter_currencies = self._converter_currencies()
        self.from_dropdown.options = [
            self._currency_code_option(code) for code in converter_currencies
        ]
        self.to_dropdown.options = [
            self._currency_code_option(code) for code in converter_currencies
        ]
        if self.from_dropdown.value not in converter_currencies:
            self.from_dropdown.value = converter_currencies[0]
        if self.to_dropdown.value not in converter_currencies:
            targets = [code for code in converter_currencies if code != self.from_dropdown.value]
            self.to_dropdown.value = targets[0] if targets else converter_currencies[0]

    async def refresh_rates(self, base_currency: str | None = None, announce: bool = True) -> None:
        base = (base_currency or self.from_dropdown.value or self.preferences.last_from).upper()
        self._set_loading(True, "status.loading_fresh", level="loading")
        try:
            snapshot = await asyncio.to_thread(self.provider.fetch_rates, base)
        except Exception:
            snapshot = self.cache.get_snapshot(base) if self.cache else None
            if snapshot is None:
                self.current_snapshot = None
                self._set_status("status.fetch_failed", "error")
                self._recalculate()
                self.page.update()
                return
            self.current_snapshot = snapshot
            self._apply_snapshot(snapshot)
            status_key = (
                "status.offline_cache_announce" if announce else "status.offline_cache_ready"
            )
            self._set_status(status_key, "warning", base=base, date=snapshot.date)
        else:
            self.current_snapshot = snapshot
            if self.cache:
                self.cache.save_snapshot(snapshot)
            self._apply_snapshot(snapshot)
            status_key = "status.online_refreshed" if announce else "status.online_ready"
            self._set_status(status_key, "success", base=base)
        finally:
            self._set_loading(False)
            self._update_currency_options()
            self._update_favorite_views()
            self._recalculate()
            self.page.update()

    def _apply_snapshot(self, snapshot: RateSnapshot | None) -> None:
        if snapshot is None:
            self.rate_value.value = self.t("detail.rate_unavailable")
            self.rate_source_text.value = self.t("detail.source_unavailable")
            self.rate_pair_text.value = self._selected_pair_text()
            self.detail_text.value = self.t("detail.refresh_or_check_cache")
            self.result_rate_text.value = self.t("result.rate_pending")
            return
        self.rate_value.value = self.t(
            "detail.meta_line",
            base=self._currency_display(snapshot.base),
            date=snapshot.date,
            source=self._data_source_label(snapshot.source),
        )
        self.rate_source_text.value = self.t(
            "common.rate_source",
            source=(
                f"{self.t('common.european_central_bank')} | "
                f"{self.t('common.api_provider', api=self.provider.api_provider_label)}"
            ),
        )
        self.rate_pair_text.value = self._selected_pair_text()
        if snapshot.cached_at:
            cached_at = snapshot.cached_at[:19].replace("T", " ")
            self.detail_text.value = self.t(
                "detail.available_count_with_cache",
                count=len(snapshot.rates),
                cached_at=cached_at,
            )
        else:
            self.detail_text.value = self.t("detail.available_count", count=len(snapshot.rates))

    def _data_source_label(self, source: str) -> str:
        lookup_key = f"detail.data_source.{source}"
        translated = self.t(lookup_key)
        return translated if translated != lookup_key else source

    def _update_currency_options(self) -> None:
        combined = set(self.currencies)
        if self.cache:
            combined.update(self.cache.available_currencies())
        if self.current_snapshot:
            combined.update(self.current_snapshot.rates.keys())
        self.currencies = sorted(combined) if combined else list(DEFAULT_CURRENCIES)
        self.favorite_dropdown.options = [self._currency_option(code) for code in self.currencies]
        self._sync_converter_currency_options()
        if self.favorite_dropdown.value not in self.currencies:
            self.favorite_dropdown.value = self._favorite_dropdown_value()

    def _update_favorite_views(self) -> None:
        favorites = self._favorite_currencies()
        self.preferences.favorites = favorites
        self._sync_converter_currency_options()
        self.favorite_row.controls = [
            ft.OutlinedButton(
                code,
                icon=ft.Icons.STAR_ROUNDED,
                style=ft.ButtonStyle(
                    side=ft.border.BorderSide(1, BORDER),
                    shape=ft.RoundedRectangleBorder(radius=999),
                    color=ACCENT if code == self.from_dropdown.value else TEXT,
                ),
                data=code,
                on_click=self._on_favorite_quick_select,
            )
            for code in favorites
        ] or [ft.Text(self.t("favorites.empty_quick"), color=MUTED, size=14)]
        self.favorite_list.controls = [self._favorite_item(code) for code in favorites] or [
            ft.Text(self.t("favorites.empty_list"), color=MUTED, size=14)
        ]
        self.favorite_dropdown.value = self._favorite_dropdown_value()
        self._save_preferences()

    def _favorite_item(self, code: str) -> ft.Control:
        return ft.Container(
            bgcolor=SURFACE_ALT,
            border_radius=18,
            padding=ft.Padding.all(16),
            content=ft.Column(
                spacing=12,
                controls=[
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Text(
                                currency_label(code),
                                size=18,
                                weight=ft.FontWeight.W_700,
                                color=TEXT,
                            ),
                            ft.Container(
                                expand=True,
                                padding=ft.Padding.only(top=3),
                                content=ft.Text(
                                    self._favorite_rate_preview(code),
                                    size=13,
                                    color=MUTED,
                                    no_wrap=False,
                                    text_align=ft.TextAlign.RIGHT,
                                ),
                            ),
                        ],
                    ),
                    ft.Row(
                        wrap=True,
                        spacing=10,
                        run_spacing=10,
                        controls=[
                            ft.TextButton(
                                self.t("favorites.use_as_source"),
                                icon=ft.Icons.CALL_MADE_ROUNDED,
                                style=ft.ButtonStyle(color=ACCENT),
                                data=("from", code),
                                on_click=self._on_favorite_assign,
                            ),
                            ft.TextButton(
                                self.t("favorites.use_as_target"),
                                icon=ft.Icons.CALL_RECEIVED_ROUNDED,
                                style=ft.ButtonStyle(color=TEXT),
                                data=("to", code),
                                on_click=self._on_favorite_assign,
                            ),
                            ft.TextButton(
                                self.t("favorites.remove"),
                                icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                                style=ft.ButtonStyle(color=ERROR),
                                data=code,
                                on_click=self._on_remove_favorite,
                            ),
                        ],
                    ),
                ],
            ),
        )

    def _favorite_rate_preview(self, code: str) -> str:
        if not self.current_snapshot or code == self.current_snapshot.base:
            return self.t("favorites.rate_pending")
        rate = rate_for(self.current_snapshot, code)
        if rate is None:
            return self.t("favorites.rate_missing")
        converted = convert_amount(parse_amount("1"), rate)
        return (
            f"1 {self.current_snapshot.base} = "
            f"{format_amount_grouped(converted, '0.0001')} {code}"
        )

    def _selected_pair_text(self) -> str:
        from_currency = self.from_dropdown.value or self.preferences.last_from
        to_currency = self.to_dropdown.value or self.preferences.last_to
        if not from_currency or not to_currency:
            return self.t("detail.selection_pending")
        return self.t(
            "detail.selection",
            from_currency=self._currency_display(from_currency),
            to_currency=self._currency_display(to_currency),
        )

    def _favorite_dropdown_value(self) -> str | None:
        for code in self.currencies:
            if code not in self.preferences.favorites:
                return code
        return self.currencies[0] if self.currencies else None

    def _grouped_amount_value(self, raw_value: str) -> str:
        if not raw_value.strip():
            return raw_value
        try:
            return format_input_amount_grouped(raw_value)
        except ValueError:
            return raw_value

    def _format_amount_input(self) -> None:
        self.amount_input.value = self._grouped_amount_value(self.amount_input.value or "")

    def _format_amount_input_live(self) -> None:
        raw_value = self.amount_input.value or ""
        formatted_value = self._grouped_amount_value(raw_value)
        self.preferences.last_amount = formatted_value
        self._save_preferences()
        clear_button_changed = self._update_clear_amount_button()
        if formatted_value == raw_value:
            if clear_button_changed:
                self.clear_amount_button.update()
            return
        caret = self._mapped_amount_caret(raw_value, formatted_value)
        self._formatting_amount_input = True
        try:
            self.amount_input.value = formatted_value
            if caret is not None:
                self.amount_input.selection = ft.TextSelection(caret, caret)
            self.amount_input.update()
        finally:
            self._formatting_amount_input = False

    def _update_clear_amount_button(self) -> bool:
        if not hasattr(self, "clear_amount_button"):
            return False
        is_visible = bool((self.amount_input.value or "").strip())
        if self.clear_amount_button.visible == is_visible:
            return False
        self.clear_amount_button.visible = is_visible
        return True

    def _mapped_amount_caret(self, raw_value: str, formatted_value: str) -> int | None:
        selection = self.amount_input.selection
        if selection is None:
            return len(formatted_value)
        if selection.base_offset != selection.extent_offset:
            return len(formatted_value)
        raw_offset = max(0, min(selection.extent_offset, len(raw_value)))
        significant_before_caret = sum(
            1 for char in raw_value[:raw_offset] if char != " "
        )
        if significant_before_caret <= 0:
            return 0
        seen = 0
        for index, char in enumerate(formatted_value):
            if char == " ":
                continue
            seen += 1
            if seen == significant_before_caret:
                return index + 1
        return len(formatted_value)

    def _save_preferences(self) -> None:
        if self.preferences_store:
            self.preferences_store.save(self.preferences)

    def _recalculate(self) -> None:
        self.preferences.last_amount = self.amount_input.value or ""
        self.preferences.last_from = self.from_dropdown.value or self.preferences.last_from
        self.preferences.last_to = self.to_dropdown.value or self.preferences.last_to
        self._save_preferences()
        self.rate_pair_text.value = self._selected_pair_text()
        rate = rate_for(self.current_snapshot, self.to_dropdown.value or "")
        self._update_result_rate_line(rate)
        try:
            amount = parse_amount(self.amount_input.value or "")
        except ValueError as exc:
            self.result_value.value = self.t("common.na")
            self.detail_text.value = self._validation_message(exc)
            return
        if rate is None:
            self.result_value.value = self.t("common.na")
            self.detail_text.value = self.t("detail.missing_rate")
            return
        converted = convert_amount(amount, rate)
        from_currency = self.from_dropdown.value or self.preferences.last_from
        to_currency = self.to_dropdown.value or self.preferences.last_to
        self.result_value.value = f"{format_amount_grouped(converted)} {to_currency}"
        self.detail_text.value = self.t(
            "detail.calculation_line",
            amount=format_amount_grouped(amount),
            from_currency=from_currency,
            converted=format_amount_grouped(converted),
            to_currency=to_currency,
        )

    def _validation_message(self, exc: ValueError) -> str:
        if exc.args and isinstance(exc.args[0], str):
            key = f"validation.{exc.args[0]}"
            translated = self.t(key)
            if translated != key:
                return translated
            return str(exc.args[0])
        return self.t("validation.invalid_number")

    def _update_result_rate_line(self, rate: float | None) -> None:
        from_currency = self.from_dropdown.value or self.preferences.last_from
        to_currency = self.to_dropdown.value or self.preferences.last_to
        if rate is None or not from_currency or not to_currency:
            self.result_rate_text.value = self.t("result.rate_pending")
            return
        rate_amount = format_amount_grouped(
            convert_amount(parse_amount("1"), rate),
            "0.0001",
        )
        self.result_rate_text.value = self.t(
            "result.one_unit_rate",
            source=currency_name(from_currency),
            amount=rate_amount,
            target=currency_name(to_currency),
        )

    def _set_loading(
        self,
        is_loading: bool,
        status_key: str | None = None,
        level: str = "loading",
        **params: str,
    ) -> None:
        self.loading_ring.visible = is_loading
        if status_key:
            self._set_status(status_key, level, **params)

    def _set_status(self, key: str, level: str, **params: str) -> None:
        self.status_key = key
        self.status_level = level
        self.status_params = params
        self._refresh_status_display()

    def _refresh_status_display(self) -> None:
        palette = {
            "success": (SUCCESS, ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED, "#20332A"),
            "warning": (WARNING, ft.Icons.WARNING_AMBER_ROUNDED, "#3B3020"),
            "error": (ERROR, ft.Icons.ERROR_OUTLINE_ROUNDED, "#3B2424"),
            "loading": (ACCENT, ft.Icons.SYNC_ROUNDED, "#2F2A1E"),
        }
        color, icon, background = palette.get(
            self.status_level,
            (TEXT, ft.Icons.INFO_OUTLINE, SURFACE_ALT),
        )
        self.status_icon.icon = icon
        self.status_icon.color = color
        self.status_value.value = self.t(self.status_key, **self.status_params)
        self.status_chip.bgcolor = background

    def _on_calculate(self, _: ft.ControlEvent) -> None:
        self._format_amount_input()
        self._recalculate()
        self.page.update()

    def _on_amount_change(self, _: ft.ControlEvent) -> None:
        if self._formatting_amount_input:
            return
        self._format_amount_input_live()

    def _on_amount_blur(self, _: ft.ControlEvent) -> None:
        self._format_amount_input()
        self._recalculate()
        self.page.update()

    def _on_clear_amount(self, _: ft.ControlEvent) -> None:
        self.amount_input.value = ""
        self.preferences.last_amount = ""
        self._update_clear_amount_button()
        self._recalculate()
        self.page.update()

    def _on_to_currency_change(self, _: ft.ControlEvent) -> None:
        self.preferences.last_to = self.to_dropdown.value or self.preferences.last_to
        self._save_preferences()
        self._recalculate()
        self._update_favorite_views()
        self.page.update()

    def _on_from_currency_change(self, _: ft.ControlEvent) -> None:
        self.preferences.last_from = self.from_dropdown.value or self.preferences.last_from
        self._save_preferences()
        cached = self.cache.get_snapshot(self.preferences.last_from) if self.cache else None
        if cached:
            self.current_snapshot = cached
            self._apply_snapshot(cached)
            self._set_status("status.cached_refreshing", "warning", base=cached.base)
        self._recalculate()
        self._update_favorite_views()
        self.page.update()
        self.page.run_task(self.refresh_rates, self.preferences.last_from, False)

    def _on_swap(self, _: ft.ControlEvent) -> None:
        from_currency = self.from_dropdown.value
        to_currency = self.to_dropdown.value
        if not from_currency or not to_currency:
            return
        self.from_dropdown.value, self.to_dropdown.value = to_currency, from_currency
        self.preferences.last_from = self.from_dropdown.value
        self.preferences.last_to = self.to_dropdown.value
        cached = self.cache.get_snapshot(self.preferences.last_from) if self.cache else None
        if cached:
            self.current_snapshot = cached
            self._apply_snapshot(cached)
        self._recalculate()
        self._update_favorite_views()
        self.page.update()
        self.page.run_task(self.refresh_rates, self.preferences.last_from, False)

    def _on_refresh(self, _: ft.ControlEvent) -> None:
        self.page.run_task(self.refresh_rates, self.from_dropdown.value, True)

    def _on_add_favorite(self, _: ft.ControlEvent) -> None:
        code = self.favorite_dropdown.value
        if not code or code in self.preferences.favorites:
            self._set_status("status.favorite_exists", "warning")
            self.page.update()
            return
        self.preferences.favorites.append(code)
        self._set_status("status.favorite_added", "success", code=code)
        self._update_favorite_views()
        self.page.update()

    def _on_remove_favorite(self, e: ft.ControlEvent) -> None:
        code = str(e.control.data)
        if code in self.preferences.favorites and len(self._favorite_currencies()) <= 1:
            self._set_status("status.favorite_keep_one", "warning")
            self.page.update()
            return
        previous_from = self.from_dropdown.value
        self.preferences.favorites = [item for item in self.preferences.favorites if item != code]
        self._set_status("status.favorite_removed", "success", code=code)
        self._update_favorite_views()
        if self.from_dropdown.value != previous_from:
            self._on_from_currency_change(e)
            return
        self._recalculate()
        self.page.update()

    def _on_favorite_quick_select(self, e: ft.ControlEvent) -> None:
        self.from_dropdown.value = str(e.control.data)
        self._on_from_currency_change(e)

    def _on_favorite_assign(self, e: ft.ControlEvent) -> None:
        target, code = e.control.data
        if target == "from":
            self.from_dropdown.value = code
            self._on_from_currency_change(e)
            return
        self.to_dropdown.value = code
        self._on_to_currency_change(e)
        self._set_active_section(0)

    def _on_language_change(self, e: ft.ControlEvent) -> None:
        language = self.i18n.normalize_language(str(e.control.data))
        if language == self.preferences.language:
            return
        self._capture_ui_state()
        self.preferences.language = language
        self.i18n.set_language(language)
        self._normalize_preferences()
        self._render_interface()
        self._apply_snapshot(self.current_snapshot)
        self._update_favorite_views()
        self._recalculate()
        self._refresh_status_display()
        self.page.update()

    def _go_to_favorites_tab(self, _: ft.ControlEvent) -> None:
        self._set_active_section(1)

    def _on_navigation_change(self, e: ft.ControlEvent) -> None:
        self._set_active_section(int(e.data))

    def _set_active_section(self, index: int) -> None:
        self.active_section_index = index
        self.navigation_bar.selected_index = index
        self.section_content.content = self.section_views[index]
        self.page.update()


async def _run_app(page: ft.Page) -> None:
    await FXFlowApp(page).start()


def _show_startup_error(page: ft.Page, details: str) -> None:
    page.clean()
    page.title = APP_NAME
    page.padding = 0
    page.spacing = 0
    page.bgcolor = BG_TOP
    page.add(
        ft.SafeArea(
            ft.Container(
                expand=True,
                padding=ft.Padding.all(20),
                content=ft.Column(
                    scroll=ft.ScrollMode.AUTO,
                    spacing=14,
                    controls=[
                        ft.Text(
                            "Aplikaciu sa nepodarilo spustit",
                            size=28,
                            weight=ft.FontWeight.W_700,
                            color=TEXT,
                        ),
                        ft.Text(
                            "Spustenie zlyhalo este pocas inicializacie. Skopiruj prosim chybu nizsie.",
                            size=15,
                            color=MUTED,
                        ),
                        ft.Container(
                            bgcolor=SURFACE,
                            border=ft.Border.all(1, BORDER),
                            border_radius=16,
                            padding=ft.Padding.all(16),
                            content=ft.Text(
                                details,
                                selectable=True,
                                color=ERROR,
                                size=13,
                                font_family="monospace",
                            ),
                        ),
                    ],
                ),
            )
        )
    )
    page.update()


async def main(page: ft.Page) -> None:
    try:
        await _run_app(page)
    except Exception:
        details = traceback.format_exc()
        print(details)
        _show_startup_error(page, details)


if __name__ == "__main__":
    ft.run(main=main, name=APP_NAME, assets_dir=str(ASSETS_DIR))
