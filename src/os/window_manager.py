# src/aicos/window_manager.py
import time

from .models import Window, WindowStatus

# 从配置文件或常量中定义窗口限制
MAX_NORMAL_WINDOWS = 4


class WindowManager:
    """管理 AIC-OS 中所有窗口的生命周期、状态和层级.

    执行窗口数量限制规则.
    """

    def __init__(self) -> None:
        # _windows 是唯一真实来源，存储所有已打开（未关闭）的窗口
        self._windows: dict[str, Window] = {}
        # z_order 用于决定窗口的堆叠顺序，数值越大越靠前
        self._z_order_counter: int = 0

    def open_window(self, window: Window) -> None:
        """打开一个新窗口.

        这会将其添加到管理器中，并自动执行窗口限制规则。
        """
        if window.id in self._windows:
            # 如果窗口已存在（例如只是被最小化了），则将其聚焦
            self.focus_window(window.id)
            return

        self._z_order_counter += 1
        window.z_order = self._z_order_counter
        window.last_focused_timestamp = time.time()

        self._windows[window.id] = window
        self._enforce_window_limits(newly_opened_window_id=window.id)

    def close_window(self, window_id: str) -> bool:
        """关闭一个窗口."""
        if window_id in self._windows:
            del self._windows[window_id]
            return True
        return False

    def set_window_status(self, window_id: str, new_status: WindowStatus) -> bool:
        """设置窗口的状态 (normal, minimize, maximize)。
        这也会触发窗口限制规则的执行.
        """  # noqa: D205
        if window_id not in self._windows:
            return False

        window = self._windows[window_id]
        window.status = new_status

        # 聚焦这个被操作的窗口
        self.focus_window(window_id)

        self._enforce_window_limits(newly_opened_window_id=window_id)
        return True

    def focus_window(self, window_id: str) -> None:
        """将一个窗口带到最前面."""
        if window_id in self._windows:
            self._z_order_counter += 1
            self._windows[window_id].z_order = self._z_order_counter
            self._windows[window_id].last_focused_timestamp = time.time()

    def get_window(self, window_id: str) -> Window | None:
        """获取单个窗口的状态."""
        return self._windows.get(window_id)

    def get_all_windows_sorted(self) -> list[Window]:
        """获取所有已打开的窗口，并按 z_order 排序（从低到高）.

        这是给渲染器使用的.
        """
        return sorted(self._windows.values(), key=lambda w: w.z_order)

    def _enforce_window_limits(self, newly_opened_window_id: str | None = None) -> None:
        """核心规则执行器.

        - 如果有任何一个窗口是最大化的，其他所有窗口都必须是最小化的。
        - 最多只能有 MAX_NORMAL_WINDOWS 个普通窗口，多余的按最久未使用的顺序最小化.
        """
        # 规则1: 最大化窗口具有独占性
        maximized_window = next(
            (w for w in self._windows.values() if w.status == WindowStatus.MAXIMIZE), None
        )
        if maximized_window:
            for window in self._windows.values():
                if window.id != maximized_window.id:
                    window.status = WindowStatus.MINIMIZE
            return  # 独占规则优先，直接返回

        # 规则2: 普通窗口数量限制
        normal_windows = [w for w in self._windows.values() if w.status == WindowStatus.NORMAL]

        if len(normal_windows) > MAX_NORMAL_WINDOWS:
            # 找出需要被最小化的窗口
            # 我们不最小化刚刚被打开或操作的窗口
            windows_to_consider = [w for w in normal_windows if w.id != newly_opened_window_id]

            # 按 last_focused_timestamp 排序，最旧的在前面
            windows_to_consider.sort(key=lambda w: w.last_focused_timestamp)

            num_to_minimize = len(normal_windows) - MAX_NORMAL_WINDOWS
            for i in range(num_to_minimize):
                windows_to_consider[i].status = WindowStatus.MINIMIZE
