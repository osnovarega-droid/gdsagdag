import json
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import customtkinter

from Managers.AccountsManager import AccountManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager
from .accounts_list_frame import AccountsListFrame
from .accounts_tab import AccountsControl
from .config_tab import ConfigTab
from .control_frame import ControlFrame
from .main_menu import MainMenu

customtkinter.set_appearance_mode("Dark")
customtkinter.set_default_color_theme("blue")

BG_MAIN = "#0b1020"
BG_PANEL = "#121a30"
BG_CARD = "#151d34"
BG_CARD_ALT = "#10182d"
BG_BORDER = "#242d48"
TXT_MAIN = "#e9edf7"
TXT_MUTED = "#8f9bb8"
ACCENT_BLUE = "#2f6dff"
ACCENT_BLUE_DARK = "#214ebe"
ACCENT_GREEN = "#1f9d55"
ACCENT_RED = "#c83a4a"
ACCENT_PURPLE = "#252b4f"
ACCENT_ORANGE = "#ff9500"

REGION_PING_TARGETS = {}


class SteamRouteManager:
    """Manages Windows Firewall rules for Steam SDR regional routing."""

    PREFIX = "FSN_Route_"

    def __init__(self):
        pass

    def _run_netsh(self, cmd_args):
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall"] + cmd_args,
                capture_output=True,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True
        except Exception:
            return False

    def add_block_rule(self, region_name, ips):
        if ips:
            packed_ips = ",".join(ips)
            return self._run_netsh(
                ["add", "rule", f"name={self.PREFIX}{region_name}", "dir=out", "action=block", f"remoteip={packed_ips}"]
            )
        return False

    def remove_rule(self, region_name):
        return self._run_netsh(["delete", "rule", f"name={self.PREFIX}{region_name}"])

    def full_cleanup(self):
        try:
            cmd = f'Remove-NetFirewallRule -Name "{self.PREFIX}*" -ErrorAction SilentlyContinue'
            subprocess.run(["powershell", "-Command", cmd], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass


class App(customtkinter.CTk):
    def __init__(self, gsi_manager=None, startup_gpu_info=None):
        super().__init__()
        self.title("Goose Panel | v.4.0.0")
        self.gsi_manager = gsi_manager
        self.window_position_file = Path("window_position.txt")
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.runtime_poll_in_flight = False
        self.ping_refresh_in_flight = False
        self._ui_actions_queue = queue.SimpleQueue()
        
        self.geometry("1100x600")
        self.minsize(1100, 600)
        self.maxsize(1100, 600)
        self.configure(fg_color=BG_MAIN)
        self._load_window_position()

        base_path = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).parent.parent
        icon_path = Path(base_path) / "Icon1.ico"
        if icon_path.exists():
            self.iconbitmap(icon_path)

        self.account_manager = AccountManager()
        self.log_manager = LogManager()
        self.settings_manager = SettingsManager()
        self.account_row_items = []
        self.account_badges = {}
        self.sdr_regions = {}
        self._level_file_mtime = None
        
        self._build_srt_state()
        self._load_region_json_if_exists()
        self._create_hidden_legacy_controllers()
        self._build_layout()

        self._connect_gsi_to_ui()
        self._log_startup_gpu_info(startup_gpu_info)

        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.show_section("license")
        self._start_ui_actions_pump()
        self._start_runtime_status_tracking()

    def _start_ui_actions_pump(self):
        def pump():
            try:
                while True:
                    action = self._ui_actions_queue.get_nowait()
                    action()
            except queue.Empty:
                pass
            except Exception:
                pass
            finally:
                if self.winfo_exists():
                    self.after(50, pump)

        self.after(50, pump)

    def _queue_ui_action(self, action):
        try:
            self._ui_actions_queue.put(action)
        except Exception:
            pass
            
    def _run_action_async(self, fn, done_callback=None):
        future = self.executor.submit(fn)

        def on_done(done_future):
            if not self.winfo_exists():
                return
            if done_callback:
                self.after(0, lambda: done_callback(done_future))

        future.add_done_callback(on_done)

    def _safe_ui_refresh(self):
        if not self.winfo_exists():
            return
        self._sync_switches_with_selection()
        self._update_accounts_info()

    def _create_hidden_legacy_controllers(self):
        self.legacy_host = customtkinter.CTkFrame(self, fg_color="transparent")

        self.accounts_list = AccountsListFrame(self.legacy_host)
        self.accounts_control = AccountsControl(self.legacy_host, self.update_label, self.accounts_list)
        self.control_frame = ControlFrame(self.legacy_host)
        self.main_menu = MainMenu(self.legacy_host)
        self.config_tab = ConfigTab(self.legacy_host)

        for widget in [self.accounts_list, self.accounts_control, self.control_frame, self.main_menu, self.config_tab]:
            widget.grid_remove()

        self.control_frame.set_accounts_list_frame(self.accounts_list)
        self.accounts_list.set_control_frame(self.control_frame)

    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = customtkinter.CTkFrame(self, width=200, corner_radius=12, fg_color=BG_PANEL, border_width=1, border_color=BG_BORDER)
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(10, 6), pady=10)
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(7, weight=1)

        customtkinter.CTkLabel(self.sidebar, text="Goose Panel", font=customtkinter.CTkFont(size=20, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")

        self.nav_buttons = {}
        nav_items = [("functional", "Functionals"), ("config", "Configurations"), ("license", "License"), ("stats", "Accs Statistic")]
        for idx, (key, text) in enumerate(nav_items, start=1):
            btn = customtkinter.CTkButton(
                self.sidebar,
                text=text,
                width=150,
                height=34,
                corner_radius=9,
                font=customtkinter.CTkFont(size=12, weight="bold"),
                fg_color=BG_CARD_ALT,
                hover_color=BG_CARD,
                text_color=TXT_MAIN,
                border_width=1,
                border_color=ACCENT_RED,
                command=lambda k=key: self.show_section(k),
            )
            btn.grid(row=idx, column=0, padx=24, pady=4)
            self.nav_buttons[key] = btn

        logs_wrap = customtkinter.CTkFrame(self.sidebar, width=180, fg_color=BG_CARD_ALT, corner_radius=10, border_width=1, border_color=BG_BORDER)
        logs_wrap.grid(row=7, column=0, padx=10, pady=(4, 8), sticky="nsew")
        logs_wrap.grid_propagate(False)
        logs_wrap.grid_columnconfigure(0, weight=1)
        logs_wrap.grid_rowconfigure(1, weight=1)

        customtkinter.CTkLabel(logs_wrap, text="• Logs", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=8, pady=(6, 2), sticky="w")

        self.logs_box = customtkinter.CTkTextbox(logs_wrap, width=250, fg_color="#0e1428", text_color="#98a7cf", border_width=0, corner_radius=8, wrap="word", font=customtkinter.CTkFont(size=11))
        self.logs_box.grid(row=1, column=0, padx=2, pady=(0, 2), sticky="nsew")
        self.log_manager.textbox = self.logs_box

        self.content = customtkinter.CTkFrame(self, fg_color=BG_PANEL, corner_radius=12, border_width=1, border_color=BG_BORDER)
        self.content.grid(row=0, column=1, padx=(6, 10), pady=10, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.sections = {
            "functional": self._build_functional_section(self.content),
            "config": self._build_config_section(self.content),
            "license": self._build_license_section(self.content),
            "stats": self._build_stats_section(self.content),
        }

    def _run_hidden_cmd(self, cmd, check=False):
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _reset_windows_proxy(self):
        if not sys.platform.startswith("win"):
            self.log_manager.add_log("⚠️ Reset доступен только на Windows")
            return

        self.log_manager.add_log("🔄 Reset: сброс proxy...")

        commands = [
            # Удаляем SRT firewall-правила (по Name и DisplayName)
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", 'Remove-NetFirewallRule -Name "FSN_Route_*" -ErrorAction SilentlyContinue; Get-NetFirewallRule -DisplayName "FSN_Route_*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue'],
            ["netsh", "advfirewall", "firewall", "delete", "rule", "name=FSN_Route_*"],

            # WinHTTP proxy -> direct
            ["netsh", "winhttp", "reset", "proxy"],

            # WinINET proxy (текущий пользователь)
            ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyEnable", "/t", "REG_DWORD", "/d", "0", "/f"],
            ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyServer", "/t", "REG_SZ", "/d", "", "/f"],
            ["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "AutoConfigURL", "/f"],
            ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "AutoDetect", "/t", "REG_DWORD", "/d", "1", "/f"],

            # Машинные ключи (best effort, могут требовать admin)
            ["reg", "add", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyEnable", "/t", "REG_DWORD", "/d", "0", "/f"],
            ["reg", "add", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyServer", "/t", "REG_SZ", "/d", "", "/f"],
            ["reg", "delete", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "AutoConfigURL", "/f"],

            ["rundll32.exe", "inetcpl.cpl,ClearMyTracksByProcess", "8"],
            ["ipconfig", "/flushdns"],
        ]

        command_errors = []
        for cmd in commands:
            try:
                result = self._run_hidden_cmd(cmd, check=False)
                if result.returncode != 0:
                    command_errors.append(" ".join(cmd[:3]))
            except Exception:
                command_errors.append(" ".join(cmd[:3]))

        try:
            verify = self._run_hidden_cmd(["netsh", "winhttp", "show", "proxy"], check=False)
            verify_text = ((verify.stdout or "") + "\n" + (verify.stderr or "")).lower()
        except Exception:
            verify_text = ""

        direct_markers = (
            "direct access",
            "прямой доступ",
            "without proxy",
            "без прокси",
            "no proxy server",
            "нет прокси",
        )
        has_proxy_markers = (
            "proxy server",
            "прокси-сервер",
            "proxy-server",
        )

        is_direct = any(marker in verify_text for marker in direct_markers)
        if not is_direct and verify_text:
            is_direct = not any(marker in verify_text for marker in has_proxy_markers)

        if is_direct:
            self.log_manager.add_log("✅ Reset завершен: proxy очищен")
        elif command_errors:
            self.log_manager.add_log("⚠️ Reset частично выполнен: запустите от администратора для полного сброса")
        else:
            self.log_manager.add_log("⚠️ Reset выполнен, но WinHTTP не подтвердил direct mode")

    def _build_functional_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        top = customtkinter.CTkFrame(frame, fg_color="transparent")
        top.grid(row=0, column=0, padx=10, pady=(8, 6), sticky="ew")
        title_frame = customtkinter.CTkFrame(top, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="w")
        customtkinter.CTkLabel(title_frame, text="Accounts", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=24, weight="bold")).grid(row=0, column=0, padx=(0, 10))

        self.accounts_info = customtkinter.CTkLabel(title_frame, text="0 accounts • 0 selected • 0 launched", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=12))
        self.accounts_info.grid(row=0, column=1)

        search_wrap = customtkinter.CTkFrame(title_frame, fg_color="transparent")
        search_wrap.grid(row=0, column=2, padx=(14, 0), sticky="w")
        self.search_var = customtkinter.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_account_filter())

        customtkinter.CTkEntry(search_wrap, textvariable=self.search_var, placeholder_text="Search", width=220, height=32, fg_color=BG_CARD, border_color=BG_BORDER, text_color=TXT_MAIN).grid(row=0, column=0)

        actions = customtkinter.CTkFrame(frame, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        actions.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")
        for i in range(4):
            actions.grid_columnconfigure(i, weight=1)

        common_btn = {"height": 34, "font": customtkinter.CTkFont(size=12, weight="bold")}
        customtkinter.CTkButton(actions, text="Launch Selected", command=self._action_start_selected, fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_DARK, **common_btn).grid(row=0, column=0, padx=6, pady=8, sticky="ew")
        customtkinter.CTkButton(actions, text="Select 4 accs", command=self._action_select_first_4, fg_color=ACCENT_PURPLE, hover_color="#313866", **common_btn).grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        customtkinter.CTkButton(actions, text="Select all accs", command=self._action_select_all_toggle, fg_color=BG_CARD_ALT, hover_color=BG_BORDER, **common_btn).grid(row=0, column=2, padx=6, pady=8, sticky="ew")
        customtkinter.CTkButton(actions, text="Kill selected", command=self._action_kill_selected, fg_color=BG_CARD_ALT, hover_color=BG_BORDER, **common_btn).grid(row=0, column=3, padx=6, pady=8, sticky="ew")

        main = customtkinter.CTkFrame(frame, fg_color="transparent")
        main.grid(row=2, column=0, padx=10, pady=(0, 8), sticky="nsew")
        main.grid_columnconfigure(0, weight=2)
        main.grid_columnconfigure(1, weight=1)
        main.grid_columnconfigure(2, weight=1)
        main.grid_rowconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=0)

        accounts_block = customtkinter.CTkFrame(main, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        accounts_block.grid(row=0, column=0, rowspan=2, padx=(0, 6), pady=0, sticky="nsew")
        accounts_block.grid_rowconfigure(1, weight=1)
        accounts_block.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(accounts_block, text="Accounts", font=customtkinter.CTkFont(size=20, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=10, pady=8, sticky="w")

        self.accounts_scroll = customtkinter.CTkScrollableFrame(accounts_block, fg_color=BG_CARD_ALT)
        self.accounts_scroll.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.accounts_scroll.grid_columnconfigure(0, weight=1)
        self._create_account_rows()

        self.srt_placeholder = customtkinter.CTkFrame(main, width=260, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        self.srt_placeholder.grid(row=0, column=1, padx=6, pady=0, sticky="nsew")
        self.srt_placeholder.grid_propagate(False)
        self.srt_placeholder.grid_rowconfigure(2, weight=1)
        self.srt_placeholder.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(self.srt_placeholder, text="Steam Route Tool", text_color="#2ee66f", font=customtkinter.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=8, pady=(8, 3), sticky="w")

        actions_bar = customtkinter.CTkFrame(self.srt_placeholder, fg_color="transparent")
        actions_bar.grid(row=1, column=0, padx=8, pady=(0, 4), sticky="ew")
        actions_bar.grid_columnconfigure((0, 1), weight=1)

        customtkinter.CTkButton(actions_bar, text="Block all", fg_color=ACCENT_RED, hover_color="#962c38", height=28, command=self._srt_block_all, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        customtkinter.CTkButton(actions_bar, text="Reset", fg_color=BG_CARD_ALT, hover_color=BG_BORDER, height=28, command=self._srt_reset, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        self.srt_scroll = customtkinter.CTkScrollableFrame(self.srt_placeholder, fg_color=BG_CARD_ALT, corner_radius=8)
        self.srt_scroll.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.srt_scroll.grid_columnconfigure(0, weight=1)
        self._build_srt_rows()

        tools = customtkinter.CTkFrame(main, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        tools.grid(row=0, column=2, padx=(6, 0), pady=0, sticky="nsew")
        tools.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(tools, text="Extra Tools", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=16, weight="bold")).grid(row=0, column=0, padx=8, pady=(8, 6), sticky="w")
        extra_buttons = [
            ("Move all CS windows", self._action_move_all_cs_windows, BG_CARD_ALT),
            ("Kill ALL CS & Steam", self._action_kill_all_cs_and_steam, ACCENT_PURPLE),
            ("Send trade", self._action_send_trade_selected, ACCENT_GREEN),
            ("Settings trade", self._action_open_looter_settings, ACCENT_RED),
            ("Marked farmer", self._action_marked_farmer, ACCENT_ORANGE),
            ("Launch BES", self._action_launch_bes, BG_CARD_ALT),
        ]
        for idx, (text, cmd, color) in enumerate(extra_buttons, start=1):
            customtkinter.CTkButton(tools, text=text, command=cmd, fg_color=color, hover_color=BG_BORDER, height=34, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=idx, column=0, padx=8, pady=4, sticky="ew")

        lobby = customtkinter.CTkFrame(main, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        lobby.grid(row=1, column=1, columnspan=2, padx=(6, 0), pady=(0, 0), sticky="ew")
        customtkinter.CTkLabel(lobby, text="Lobby Management", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=13, weight="bold")).grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w")
        for i in range(2):
            lobby.grid_columnconfigure(i, weight=1)

        lobby_buttons = [
            ("Make Lobbies", self._action_make_lobbies, BG_CARD_ALT),
            ("Make Lobbes & Search Game", self._action_make_lobbies_and_search, ACCENT_BLUE),
            ("Disband lobbies", self._action_disband_lobbies, BG_CARD_ALT),
            ("Get level", self._action_try_get_level, BG_CARD_ALT),
            ("Shuffle Lobbies", self._action_shuffle_lobbies, BG_CARD_ALT),
            ("Support Developer", self._action_support_developer, BG_CARD_ALT),
        ]
        for idx, (text, cmd, color) in enumerate(lobby_buttons):
            r, c = divmod(idx, 2)
            customtkinter.CTkButton(lobby, text=text, command=cmd, fg_color=color, hover_color=BG_BORDER, height=32, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=r + 1, column=c, padx=6, pady=4, sticky="ew")

        self._update_accounts_info()
        return frame

    def _create_account_rows(self):
        self.account_row_items.clear()
        levels_cache = getattr(self.accounts_list, "levels_cache", {})

        for idx, account in enumerate(self.account_manager.accounts):
            row = customtkinter.CTkFrame(self.accounts_scroll, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BG_BORDER)
            row.grid(row=idx, column=0, padx=4, pady=3, sticky="ew")
            row.grid_columnconfigure(1, weight=1)

            sw = customtkinter.CTkSwitch(row, text="", width=24, command=lambda a=account: self._toggle_account(a), fg_color="#2d3b60", progress_color=ACCENT_BLUE)
            sw.grid(row=0, column=0, rowspan=2, padx=(6, 5), pady=6, sticky="w")
            if account in self.account_manager.selected_accounts:
                sw.select()

            lvl_data = levels_cache.get(account.login, {})
            level_text = lvl_data.get("level", "-")
            xp_text = lvl_data.get("xp", "-")

            level_label = customtkinter.CTkLabel(row, text=f"lvl: {level_text} | xp: {xp_text}", anchor="w", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=11))
            level_label.grid(row=1, column=1, padx=3, pady=(0, 5), sticky="w")

            badge = customtkinter.CTkLabel(
                row,
                text="idle",
                text_color="#dbe8ff",
                font=customtkinter.CTkFont(size=10),
                fg_color=ACCENT_BLUE,
                corner_radius=8,
                width=62,
                height=20,
            )
            badge.grid(row=0, column=2, rowspan=2, padx=6, pady=6)

            login_label = customtkinter.CTkLabel(row, text=account.login, anchor="w", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=12, weight="bold"))
            login_label.grid(row=0, column=1, padx=3, pady=(5, 0), sticky="w")

            account.setColorCallback(lambda color, a=account: self._handle_account_color_change(a, color))
            self.account_badges[account.login] = badge

            self.account_row_items.append({
                "row": row,
                "account": account,
                "login_lower": account.login.lower(),
                "switch": sw,
                "login_label": login_label,
                "level_label": level_label,
                "badge": badge,
            })

    def _refresh_level_labels(self):
        try:
            if hasattr(self.accounts_list, "_load_levels_from_json"):
                self.accounts_list.levels_cache = self.accounts_list._load_levels_from_json()
            levels_cache = getattr(self.accounts_list, "levels_cache", {}) or {}
            levels_cache_lower = {str(k).lower(): v for k, v in levels_cache.items()}
            for item in self.account_row_items:
                login = item["account"].login
                lvl_data = levels_cache.get(login, levels_cache_lower.get(str(login).lower(), {}))
                level_text = lvl_data.get("level", "-")
                xp_text = lvl_data.get("xp", "-")
                item["level_label"].configure(text=f"lvl: {level_text} | xp: {xp_text}")
        except Exception:
            pass
    def _refresh_level_labels_if_changed(self):
        """Обновляет level/xp в UI, если level.json изменился."""
        try:
            level_path = Path("level.json")
            mtime = level_path.stat().st_mtime if level_path.exists() else None
            if mtime != self._level_file_mtime:
                self._level_file_mtime = mtime
                self._refresh_level_labels()
        except Exception:
            pass
    def _normalize_account_color(self, color):
        color_map = {"green": ACCENT_GREEN, "yellow": "#f5c542", "white": "#DCE4EE"}
        return color_map.get(str(color).lower(), color)

    def _handle_account_color_change(self, account, color):
        normalized = self._normalize_account_color(color)

        def apply_change():
            for item in self.account_row_items:
                if item["account"] is account:
                    item["login_label"].configure(text_color=normalized)
                    break
            self._refresh_account_badge(account)
            self._update_accounts_info()

        self._queue_ui_action(apply_change)

    def _refresh_account_badge(self, account, is_running=None):
        for item in self.account_row_items:
            if item["account"] is not account:
                continue
            running = account.isCSValid() if is_running is None else is_running
            item["badge"].configure(text="Running" if running else "idle", fg_color=ACCENT_GREEN if running else ACCENT_BLUE)
            return

    def _refresh_all_runtime_states(self):
        for item in self.account_row_items:
            account = item["account"]
            current_color = self._normalize_account_color(getattr(account, "_color", TXT_MAIN))
            item["login_label"].configure(text_color=current_color)
        self._sync_switches_with_selection()
        self._update_accounts_info()

    def _poll_runtime_states(self):
        running_map = {}
        for item in self.account_row_items:
            account = item["account"]
            try:
                running_map[account] = account.isCSValid()
            except Exception:
                running_map[account] = False
        return running_map

    def _start_runtime_status_tracking(self):
        def poll():
            try:
                self._refresh_all_runtime_states()
                self._refresh_level_labels_if_changed()
                if not self.runtime_poll_in_flight:
                    self.runtime_poll_in_flight = True

                    def done_callback(future):
                        self.runtime_poll_in_flight = False
                        try:
                            running_map = future.result()
                            for item in self.account_row_items:
                                self._refresh_account_badge(item["account"], running_map.get(item["account"], False))
                        except Exception:
                            pass

                    self._run_action_async(self._poll_runtime_states, done_callback)
            except Exception:
                self.runtime_poll_in_flight = False
            finally:
                if self.winfo_exists():
                    self.after(1500, poll)

        self.after(500, poll)

    def _apply_account_filter(self):
        filter_text = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        render_idx = 0
        for item in self.account_row_items:
            show = not filter_text or filter_text in item["login_lower"]
            if show:
                item["row"].grid(row=render_idx, column=0, padx=4, pady=3, sticky="ew")
                render_idx += 1
            else:
                item["row"].grid_remove()

    def _toggle_account(self, account):
        if account in self.account_manager.selected_accounts:
            self.account_manager.selected_accounts.remove(account)
        else:
            self.account_manager.selected_accounts.append(account)
        self._safe_ui_refresh()

    def _sync_switches_with_selection(self):
        selected = set(self.account_manager.selected_accounts)
        for item in self.account_row_items:
            if item["account"] in selected:
                item["switch"].select()
            else:
                item["switch"].deselect()

    def _update_accounts_info(self):
        total = len(self.account_manager.accounts)
        selected = len(self.account_manager.selected_accounts)
        launched = self.account_manager.count_launched_accounts()
        if hasattr(self, "accounts_info"):
            self.accounts_info.configure(text=f"{total} accounts • {selected} selected • {launched} launched")

    def _build_config_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(frame, text="Configurations", font=customtkinter.CTkFont(size=28, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=16, pady=(14, 8), sticky="w")
        return frame

    def _build_license_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        frame.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(frame, text="License", font=customtkinter.CTkFont(size=30, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=16, pady=(20, 8), sticky="w")
        return frame

    def _build_stats_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        frame.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(frame, text="Accs Stats", font=customtkinter.CTkFont(size=30, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=16, pady=(20, 8), sticky="w")
        return frame

    def _action_start_selected(self):
        self._run_action_async(self.accounts_control.start_selected)

    def _action_select_first_4(self):
        non_farmed = [acc for acc in self.account_manager.accounts if not self.accounts_list.is_farmed_account(acc)]
        target = non_farmed[:4]
        current = self.account_manager.selected_accounts
        if len(current) == len(target) and all(a in current for a in target):
            self.account_manager.selected_accounts.clear()
        else:
            self.account_manager.selected_accounts.clear()
            self.account_manager.selected_accounts.extend(target)
        self._safe_ui_refresh()

    def _action_select_all_toggle(self):
        if len(self.account_manager.selected_accounts) == len(self.account_manager.accounts):
            self.account_manager.selected_accounts.clear()
        else:
            self.account_manager.selected_accounts.clear()
            self.account_manager.selected_accounts.extend(self.account_manager.accounts)
        self._safe_ui_refresh()

    def _action_kill_selected(self):
        self._run_action_async(self.accounts_control.kill_selected)

    def _action_try_get_level(self):
        self._run_action_async(self.accounts_control.try_get_level, lambda _: self.after(300, self._refresh_level_labels))

    def _action_kill_all_cs_and_steam(self):
        self._run_action_async(self.control_frame.kill_all_cs_and_steam)

    def _action_move_all_cs_windows(self):
        self._run_action_async(self.control_frame.move_all_cs_windows)

    def _action_launch_bes(self):
        self._run_action_async(self.control_frame.launch_bes)

    def _action_support_developer(self):
        self._run_action_async(self.control_frame.sendCasesMe)

    def _action_send_trade_selected(self):
        self._run_action_async(self.config_tab.send_trade_selected)

    def _action_open_looter_settings(self):
        self._run_action_async(self.config_tab.open_looter_settings)

    def _action_marked_farmer(self):
        self._run_action_async(self.accounts_control.mark_farmed, lambda _: self._safe_ui_refresh())

    def _action_make_lobbies_and_search(self):
        self._run_action_async(self.main_menu.make_lobbies_and_search_game)

    def _action_make_lobbies(self):
        self._run_action_async(self.main_menu.make_lobbies)

    def _action_shuffle_lobbies(self):
        self._run_action_async(self.main_menu.shuffle_lobbies)

    def _action_disband_lobbies(self):
        self._run_action_async(self.main_menu.disband_lobbies)

    def _load_region_json_if_exists(self):
        region_path = Path("region.json")
        if not region_path.exists():
            return
        try:
            data = json.loads(region_path.read_text(encoding="utf-8"))
            pops = data.get("pops", {})
            parsed_regions = {}
            parsed_ping_targets = {}

            for pop_key, pop_data in pops.items():
                relays = pop_data.get("relays", [])
                if not relays:
                    continue

                desc = pop_data.get("desc") or pop_key
                relay_ips = []
                for relay in relays:
                    ip = relay.get("ipv4")
                    if not ip:
                        continue
                    relay_ips.append(ip)

                if not relay_ips:
                    continue

                # Используем только точные IP-адреса релэев, без расширения до /24,
                # чтобы блокировка одной-двух зон не "задевала" соседние регионы.
                parsed_regions[desc] = sorted(set(relay_ips))
                parsed_ping_targets[desc] = relay_ips[0]

            if parsed_regions:
                self.sdr_regions = parsed_regions
                REGION_PING_TARGETS.clear()
                REGION_PING_TARGETS.update(parsed_ping_targets)
        except Exception:
            pass

    def _build_srt_state(self):
        self.route_manager = SteamRouteManager() if sys.platform.startswith("win") else None
        self.blocked_regions = set()
        self.srt_rows = {}

    def _build_srt_rows(self):
        if not self.sdr_regions:
            customtkinter.CTkLabel(
                self.srt_scroll,
                text="region.json не найден или пуст",
                text_color=TXT_MUTED,
                font=customtkinter.CTkFont(size=11),
            ).grid(row=0, column=0, padx=6, pady=8, sticky="w")
            return

        for idx, region in enumerate(self.sdr_regions.keys()):
            row = customtkinter.CTkFrame(self.srt_scroll, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BG_BORDER)
            row.grid(row=idx, column=0, padx=2, pady=2, sticky="ew")
            row.grid_columnconfigure(0, weight=1)

            name_label = customtkinter.CTkLabel(row, text=region, text_color=TXT_MAIN, font=customtkinter.CTkFont(size=11, weight="bold"))
            name_label.grid(row=0, column=0, padx=(6, 2), pady=4, sticky="w")

            ping_label = customtkinter.CTkLabel(row, text="-- ms", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=10))
            ping_label.grid(row=0, column=1, padx=2, pady=4)

            block_btn = customtkinter.CTkButton(
                row,
                text="✕",
                width=26,
                height=24,
                fg_color=BG_CARD_ALT,
                hover_color=ACCENT_RED,
                font=customtkinter.CTkFont(size=12, weight="bold"),
                command=lambda r=region: self._toggle_region_block(r),
            )
            block_btn.grid(row=0, column=2, padx=(2, 6), pady=3)
            self.srt_rows[region] = {"ping": ping_label, "button": block_btn}

        self._schedule_ping_refresh()

    def _set_region_visual(self, region):
        row = self.srt_rows.get(region)
        if not row:
            return
        is_blocked = region in self.blocked_regions
        row["button"].configure(
            fg_color=ACCENT_RED if is_blocked else BG_CARD_ALT,
            text="✓" if is_blocked else "✕",
            hover_color="#962c38" if is_blocked else ACCENT_RED,
        )

    def _toggle_region_block(self, region):
        def op():
            if region in self.blocked_regions:
                ok = True if self.route_manager is None else self.route_manager.remove_rule(region)
                if ok:
                    self.blocked_regions.discard(region)
            else:
                region_ips = self.sdr_regions.get(region, [])
                ok = True if self.route_manager is None else self.route_manager.add_block_rule(region, region_ips)
                if ok:
                    self.blocked_regions.add(region)
            return True

        self._run_action_async(op, lambda _: self._set_region_visual(region))

    def _srt_block_all(self):
        def op():
            for region, region_ips in self.sdr_regions.items():
                ok = True if self.route_manager is None else self.route_manager.add_block_rule(region, region_ips)
                if ok:
                    self.blocked_regions.add(region)

        def done(_):
            for region in self.sdr_regions.keys():
                self._set_region_visual(region)

        self._run_action_async(op, done)

    def _srt_reset(self):
        def op():
            self._reset_windows_proxy()
            self.blocked_regions.clear()

        def done(_):
            for region in self.sdr_regions.keys():
                self._set_region_visual(region)

        self._run_action_async(op, done)

    def _get_ping_ms(self, host):
        try:
            if not host:
                return "-- ms"
            cmd = ["ping", "-n", "1", "-w", "350", host] if sys.platform.startswith("win") else ["ping", "-c", "1", "-W", "1", host]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            out = result.stdout.lower()

            token = None
            for marker in ("time=", "time<", "время=", "время<"):
                if marker in out:
                    token = out.split(marker, 1)[1]
                    break

            if not token:
                return "-- ms"

            value = []
            dot_seen = False
            for ch in token:
                if ch.isdigit():
                    value.append(ch)
                    continue
                if ch in (".", ",") and not dot_seen:
                    value.append(".")
                    dot_seen = True
                    continue
                if value:
                    break

            if not value:
                return "-- ms"

            ping_value = float("".join(value))
            return f"{ping_value:.1f} ms"
        except Exception:
            return "-- ms"

    def _collect_region_pings(self):
        return {region: self._get_ping_ms(REGION_PING_TARGETS.get(region)) for region in self.srt_rows.keys()}

    def _schedule_ping_refresh(self):
        def refresh():
            try:
                if not self.ping_refresh_in_flight:
                    self.ping_refresh_in_flight = True

                    def done_callback(future):
                        self.ping_refresh_in_flight = False
                        try:
                            ping_map = future.result()
                            for region, row in self.srt_rows.items():
                                row["ping"].configure(text=ping_map.get(region, "-- ms"))
                        except Exception:
                            pass

                    self._run_action_async(self._collect_region_pings, done_callback)
            except Exception:
                self.ping_refresh_in_flight = False
            finally:
                if self.winfo_exists():
                    self.after(7000, refresh)

        self.after(500, refresh)

    def show_section(self, section_key):
        for key, frame in self.sections.items():
            if key == section_key:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_forget()

        for key, button in self.nav_buttons.items():
            button.configure(fg_color=BG_CARD if key == section_key else BG_CARD_ALT, border_color=ACCENT_GREEN if key == section_key else ACCENT_RED)

    def _log_startup_gpu_info(self, startup_gpu_info):
        if not startup_gpu_info:
            return
        vendor_id, device_id, source = startup_gpu_info
        source_label = "detected" if source == "detected" else "settings fallback"
        try:
            self.log_manager.add_log(f"🎮 GPU ({source_label}): VendorID={vendor_id}, DeviceID={device_id}")
        except Exception:
            pass

    def _connect_gsi_to_ui(self):
        try:
            if self.gsi_manager and self.accounts_list:
                self.gsi_manager.set_accounts_list_frame(self.accounts_list)
                print("✅ 🎮 GSIManager подключен к AccountsListFrame!")
            else:
                print("⚠️ GSIManager или AccountsListFrame недоступны")
        except Exception as exc:
            print(f"❌ Ошибка подключения GSIManager: {exc}")

    def _load_window_position(self):
        try:
            if not self.window_position_file.exists():
                return
            raw = self.window_position_file.read_text(encoding="utf-8").strip()
            if not raw:
                return
            parts = raw.split(",")
            if len(parts) != 2:
                return
            x = int(parts[0].strip())
            y = int(parts[1].strip())
            self.geometry(f"1100x600+{x}+{y}")
        except Exception:
            pass

    def _save_window_position(self):
        try:
            x = self.winfo_x()
            y = self.winfo_y()
            self.window_position_file.write_text(f"{x},{y}", encoding="utf-8")
        except Exception:
            pass

    def on_closing(self):
        self._save_window_position()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    def update_label(self):
        self._update_accounts_info()
        self._sync_switches_with_selection()
        self._apply_account_filter()


if __name__ == "__main__":
    app = App()
    app.mainloop()
