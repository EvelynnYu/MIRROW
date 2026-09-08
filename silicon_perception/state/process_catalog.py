"""通用进程分类清单 — 游戏/编码/其他（扫描 + 确认 + 自学习）。

100% 铁律下的设计：
- coding：内置编辑器 basename 集（well-known）+ 用户可增删 → 确定识别。
- gaming：只有"已确认"的进程才判 gaming（用户确认=铁证）。Steam/Epic 扫描只产出**候选**，
  手动添加 + 声明 gaming 时自适应记候选，用户确认后才生效。

匹配依据：前台进程 basename（screen.py 存的是 os.path.basename，如 "Code.exe"）。
持久化：data/process_catalog.json（与 user_status.json 同目录）。
"""

from __future__ import annotations

import os
import json
import glob
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CATALOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "process_catalog.json"
)

# 内置编码工具 basename（小写）。用户可在设置里增删。
_DEFAULT_CODING = {
    "code.exe", "cursor.exe", "devenv.exe", "sublime_text.exe",
    "idea64.exe", "pycharm64.exe", "webstorm64.exe", "goland64.exe",
    "clion64.exe", "rider64.exe", "fleet.exe", "hbuilderx.exe",
    "notepad++.exe", "atom.exe", "windowsterminal.exe", "wt.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "nvim.exe", "gvim.exe",
}

# 扫描游戏库时排除的常见非游戏 exe（引擎崩溃处理器/运行库/工具）
_NON_GAME_EXE = {
    "unitycrashhandler64.exe", "unitycrashhandler32.exe",
    "vcredist_x64.exe", "vcredist_x86.exe", "dxsetup.exe",
    "crashreportclient.exe", "easyanticheat.exe", "easyanticheat_setup.exe",
    "battleye.exe", "beservice.exe", "uninstall.exe", "installer.exe",
    "notification_helper.exe", "settings.exe", "launcher_installer.exe",
}

# 前台进程黑名单——声明 gaming 时不应被记成游戏（启动器/浏览器等）
_NOT_GAME_FOREGROUND = {
    "steam.exe", "steamservice.exe", "steamwebhelper.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe",
    "battle.net.exe", "battle.net helper.exe",
    "ubisoftconnect.exe", "upc.exe",
    "origin.exe", "ea app.exe",
    "gog galaxy.exe", "galaxyclient.exe",
    "chrome.exe", "msedge.exe", "firefox.exe",
    "explorer.exe", "taskmgr.exe", "notepad.exe",
    "discord.exe", "slack.exe", "teams.exe",
    "spotify.exe", "groove music.exe",
}


class ProcessCatalog:
    """进程分类清单单例。"""

    def __init__(self):
        # {basename_lower: {"name": str, "source": str, "confirmed": bool}}
        self.games: Dict[str, dict] = {}
        self.coding: Dict[str, dict] = {}
        # 候选（待用户确认）：{basename_lower: {"name","source"}}
        self.candidates: Dict[str, dict] = {}
        # Windows GameConfigStore 系统级游戏库（跨启动器、零维护，加载时读注册表）
        self._gcs_games: set = set()
        self._load()
        self._load_gameconfigstore()

    # ---------- 持久化 ----------
    def _load(self):
        # 内置编码工具先铺底
        for exe in _DEFAULT_CODING:
            self.coding[exe] = {"name": exe, "source": "builtin", "confirmed": True}
        try:
            if os.path.exists(_CATALOG_FILE):
                with open(_CATALOG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.games = data.get("games", {}) or {}
                # 用户对编码工具的增删覆盖内置
                for exe, meta in (data.get("coding", {}) or {}).items():
                    self.coding[exe] = meta
                for exe in data.get("coding_removed", []) or []:
                    self.coding.pop(exe, None)
                self.candidates = data.get("candidates", {}) or {}
        except Exception as e:
            logger.warning(f"ProcessCatalog 加载失败，用默认: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(_CATALOG_FILE), exist_ok=True)
            # 记录被用户移除的内置编码工具，供下次加载还原删除
            coding_removed = [e for e in _DEFAULT_CODING if e not in self.coding]
            coding_user = {e: m for e, m in self.coding.items() if m.get("source") != "builtin"}
            with open(_CATALOG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "games": self.games,
                    "coding": coding_user,
                    "coding_removed": coding_removed,
                    "candidates": self.candidates,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"ProcessCatalog 保存失败: {e}")

    # ---------- 分类（供引擎调用）----------
    def classify(self, exe: Optional[str]) -> Optional[str]:
        """把前台进程 basename 分类为 'gaming' / 'coding' / None。

        gaming 铁证来源：Windows GameConfigStore（系统级）或已确认清单（用户确认）。
        """
        if not exe:
            return None
        key = exe.strip().lower()
        if key in self.coding:
            return "coding"
        if key in self._gcs_games:          # 系统级游戏库铁证（跨启动器、零维护）
            return "gaming"
        g = self.games.get(key)
        if g and g.get("confirmed"):
            return "gaming"
        return None

    def _load_gameconfigstore(self):
        """读 HKCU\\System\\GameConfigStore\\Children\\*\\MatchedExeFullPath 填 _gcs_games。

        Xbox Game Bar/GameDVR 按"全屏+DirectX"自动识别游戏并记录，跨启动器、纯本地、零维护。
        非 Windows / 注册表不可用时静默降级（_gcs_games 保持空）。
        """
        try:
            import winreg
        except Exception:
            return
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children")
        except OSError:
            return
        found = set()
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i); i += 1
            except OSError:
                break
            try:
                with winreg.OpenKey(root, sub) as sk:
                    exe_path, _ = winreg.QueryValueEx(sk, "MatchedExeFullPath")
                    if exe_path:
                        base = os.path.basename(exe_path).strip().lower()
                        if base and base not in _NON_GAME_EXE:
                            found.add(base)
            except OSError:
                continue
        try:
            root.Close()
        except Exception:
            pass
        self._gcs_games = found
        if found:
            logger.info(f"ProcessCatalog: GameConfigStore 加载 {len(found)} 款系统级游戏")

    def is_game(self, exe: Optional[str]) -> bool:
        """basename 是否为已知游戏（GameConfigStore 或已确认清单）。"""
        if not exe:
            return False
        key = exe.strip().lower()
        if key in self._gcs_games:
            return True
        g = self.games.get(key)
        return bool(g and g.get("confirmed"))

    def is_any_game_running(self) -> bool:
        """枚举当前 PC 进程列表，是否有任一已知游戏在运行（进程级复判核心）。

        psutil 不可用时返回 False（降级：退回只看前台）。进程枚举≠截屏，afk/锁屏照样能读。
        """
        try:
            import psutil
        except Exception:
            return False
        try:
            for p in psutil.process_iter(['name']):
                n = (p.info.get('name') or '').strip().lower()
                if n and self.is_game(n):
                    return True
        except Exception:
            return False
        return False

    # ---------- 游戏清单管理 ----------
    def add_game(self, exe: str, name: str = "", source: str = "manual"):
        key = exe.strip().lower()
        if not key:
            return
        self.games[key] = {"name": name or exe, "source": source, "confirmed": True}
        self.candidates.pop(key, None)
        self._save()

    def remove_game(self, exe: str):
        self.games.pop(exe.strip().lower(), None)
        self._save()

    def confirm_candidate(self, exe: str) -> bool:
        """把候选确认为游戏。"""
        key = exe.strip().lower()
        cand = self.candidates.pop(key, None)
        if cand is None:
            return False
        self.games[key] = {"name": cand.get("name", exe), "source": cand.get("source", "candidate"), "confirmed": True}
        self._save()
        return True

    def reject_candidate(self, exe: str):
        self.candidates.pop(exe.strip().lower(), None)
        self._save()

    def record_declared_gaming(self, exe: Optional[str]):
        """用户声明 gaming 时，把当时前台进程记为**已确认游戏**（用户认证=铁证，补 GameConfigStore 盲区）。

        排除编码工具/启动器/浏览器/系统进程等明显非游戏前台。声明一次即记住，以后自动识别。
        误记可在设置里 remove_game 移除。
        """
        if not exe:
            return
        key = exe.strip().lower()
        if key in self.games or key in self._gcs_games:
            return  # 已是已知游戏
        if key in self.coding or key in _NON_GAME_EXE or key in _NOT_GAME_FOREGROUND:
            return  # 明显非游戏前台，不误记
        self.games[key] = {"name": exe, "source": "declared", "confirmed": True}
        self.candidates.pop(key, None)
        self._save()
        logger.info(f"ProcessCatalog: 声明自适应记为已确认游戏 {exe}")

    # ---------- 编码工具管理 ----------
    def add_coding(self, exe: str):
        key = exe.strip().lower()
        if key:
            self.coding[key] = {"name": exe, "source": "manual", "confirmed": True}
            self._save()

    def remove_coding(self, exe: str):
        self.coding.pop(exe.strip().lower(), None)
        self._save()

    # ---------- 首次扫描游戏平台库 → 候选 ----------
    def scan_libraries(self) -> int:
        """扫 Steam/Epic 库枚举候选游戏 exe。返回新增候选数。best-effort，不抛异常。"""
        found = 0
        try:
            for exe, name in self._scan_steam():
                key = exe.lower()
                if key in self.games or key in self.candidates or key in self.coding:
                    continue
                if key in _NON_GAME_EXE:
                    continue
                self.candidates[key] = {"name": name, "source": "steam"}
                found += 1
        except Exception as e:
            logger.debug(f"扫描 Steam 失败: {e}")
        if found:
            self._save()
            logger.info(f"ProcessCatalog: 扫描游戏库新增 {found} 个候选")
        return found

    def _steam_root(self) -> Optional[str]:
        """从注册表读 Steam 安装路径。"""
        try:
            import winreg
            for hive, sub in [
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            ]:
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        val, _ = winreg.QueryValueEx(k, "SteamPath" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")
                        if val and os.path.isdir(val):
                            return val
                except OSError:
                    continue
        except Exception:
            pass
        return None

    def _scan_steam(self):
        """枚举 steamapps/common/<Game>/ 下的 exe 作为候选。yield (exe_basename, game_name)。"""
        root = self._steam_root()
        if not root:
            return
        # library folders（含多磁盘库）
        lib_roots = [os.path.join(root, "steamapps")]
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            if os.path.exists(vdf):
                with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        # 粗解析 "path" "D:\\SteamLibrary"
                        if '"path"' in line:
                            parts = line.split('"')
                            if len(parts) >= 4:
                                p = parts[3].replace("\\\\", "\\")
                                sa = os.path.join(p, "steamapps")
                                if os.path.isdir(sa):
                                    lib_roots.append(sa)
        except Exception:
            pass

        seen_dirs = set()
        for sa in lib_roots:
            common = os.path.join(sa, "common")
            if not os.path.isdir(common):
                continue
            try:
                for game_dir in os.listdir(common):
                    full = os.path.join(common, game_dir)
                    if not os.path.isdir(full) or full in seen_dirs:
                        continue
                    seen_dirs.add(full)
                    # 枚举该游戏目录下（含一层子目录）的 exe
                    exes = glob.glob(os.path.join(full, "*.exe")) + glob.glob(os.path.join(full, "*", "*.exe"))
                    for exe_path in exes:
                        base = os.path.basename(exe_path)
                        if base.lower() in _NON_GAME_EXE:
                            continue
                        yield base, game_dir
            except Exception:
                continue

    # ---------- 前端读取 ----------
    def snapshot(self) -> dict:
        """供前端渲染：games / coding / candidates 三组。"""
        return {
            "games": self.games,
            "coding": self.coding,
            "candidates": self.candidates,
        }


_catalog: Optional[ProcessCatalog] = None


def get_catalog() -> ProcessCatalog:
    """全局单例。"""
    global _catalog
    if _catalog is None:
        _catalog = ProcessCatalog()
    return _catalog
