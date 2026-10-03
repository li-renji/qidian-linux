#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# model_manager.py - intent 本地模型互斥管理器
# 设计依据: docs/local-model-manager.md
# 功能:
#   1. 文本模型 / 多模态模型互斥加载（同一时间只驻留一个）
#   2. 切换时通过 Ollama keep_alive=0 立即释放旧模型内存
#   3. 启动策略: 手动开启 / 离线时自动启用
# ============================================================
import json
import os
import time
import subprocess
import threading
import urllib.request
import urllib.error
from typing import Optional, Dict, Any

# ============================================================
# 常量配置（全部支持环境变量覆盖，去除平台硬编码）
# ============================================================
# Ollama 可执行文件：环境变量 OLLAMA_EXE 或 PATH 中查找，不再硬编码用户目录
DEFAULT_OLLAMA_EXE = os.environ.get("OLLAMA_EXE", "")
# 模型存储目录：环境变量 QD_OLLAMA_MODELS；仅 Linux 上默认 /data/ollama_models
DEFAULT_OLLAMA_MODELS = os.environ.get("QD_OLLAMA_MODELS",
                                       "/data/ollama_models")

DEFAULT_CONFIG = {
    "text_model": "qwen3:4b",            # Qwen3-4B-Instruct-2507
    "multimodal_model": "qwen3-vl:2b",   # Qwen3-VL-2B-Instruct
    "startup": "manual",                 # manual | offline | auto
    "auto_offline_fallback": True,       # 断网时自动启用本地文本模型
    "ollama_host": os.environ.get("QD_OLLAMA_HOST", "http://localhost:11434"),
}

# 模型槽位
SLOT_TEXT = "text"
SLOT_VL = "vl"


class ModelManager:
    """本地模型互斥管理器。

    同一时间内存中只保留一个模型：
      - 请求文本/编程 -> 若多模态驻留则先卸载 -> 加载文本模型
      - 请求视觉/多模态 -> 若文本驻留则先卸载 -> 加载多模态模型
    卸载通过 Ollama 的 keep_alive=0 立即释放内存。
    """

    def __init__(self, config: Optional[dict] = None):
        self.config: Dict[str, Any] = {**DEFAULT_CONFIG, **(config or {})}
        self._active_slot: Optional[str] = None   # None / "text" / "vl"
        self._lock = threading.Lock()             # 保证切换原子性
        self._last_error: Optional[str] = None
        self._offline_detected: bool = False
        self._offline_checked_at: float = 0.0
        # 修复（全链审查 C2）：spawn 失败/被禁用后不再重试——
        # 旧行为：每次推理调用都可能重复拉起 ollama serve 并空等 10s，
        # 导致离线环境下 chat 降级路径阻塞 10s+/次、测试被拖死。
        self._spawn_disabled = os.environ.get('QD_OLLAMA_NO_SPAWN') == '1'
        self._start_attempted = False

    # --------------------------------------------------------
    # 对外接口
    # --------------------------------------------------------
    @property
    def active_slot(self) -> Optional[str]:
        return self._active_slot

    @property
    def active_model(self) -> Optional[str]:
        if self._active_slot == SLOT_TEXT:
            return self.config["text_model"]
        if self._active_slot == SLOT_VL:
            return self.config["multimodal_model"]
        return None

    def is_available(self) -> bool:
        """Ollama 是否可连。"""
        return self._ping_ollama()

    def request_text(self, prompt: str, images: Optional[list] = None,
                     system_prompt: str = "", **kwargs) -> Optional[str]:
        """文本/编程推理请求（含 Agent 工具调用场景）。"""
        return self._infer(SLOT_TEXT, prompt, images, system_prompt=system_prompt, **kwargs)

    def request_vision(self, prompt: str, images: list,
                       system_prompt: str = "", **kwargs) -> Optional[str]:
        """多模态推理请求（图片/截图输入）。"""
        if not images:
            raise ValueError("request_vision 需要至少一张图片")
        return self._infer(SLOT_VL, prompt, images, system_prompt=system_prompt, **kwargs)

    def unload(self) -> bool:
        """卸载当前模型，释放内存。"""
        model = self.active_model
        if model is None:
            return True
        ok = self._unload_model(model)
        if ok:
            self._active_slot = None
        return ok

    def shutdown(self):
        """退出前卸载模型。"""
        self.unload()

    # --------------------------------------------------------
    # 核心互斥调度
    # --------------------------------------------------------
    def _infer(self, target_slot: str, prompt: str, images: Optional[list],
               system_prompt: str = "", **kwargs) -> Optional[str]:
        with self._lock:
            # 确保 Ollama 服务在运行（未运行则自动拉起）
            if not self._ensure_ollama_running():
                self._last_error = "Ollama 未运行且无法自动启动"
                return None

            # 启动策略检查
            if not self._should_start():
                self._last_error = "本地模型未启用（startup 策略不允许）"
                return None

            # 目标模型
            model = self.config["text_model"] if target_slot == SLOT_TEXT else self.config["multimodal_model"]
            # 互斥：确保 Ollama 中只驻留目标模型（含历史残留，鲁棒卸载）
            if not self._ensure_exclusive(model):
                self._last_error = f"切换模型失败（无法卸载其他驻留模型）: {model}"
                return None
            self._active_slot = target_slot

            return self._chat(model, prompt, images, system_prompt=system_prompt, **kwargs)

    def _should_start(self) -> bool:
        """按启动策略判断是否允许启用本地模型。"""
        mode = self.config["startup"]
        if mode == "manual":
            return True  # 手动模式下由界面调用本管理器即视为已开启
        if mode == "auto":
            return True
        if mode == "offline":
            # 离线模式：仅断网时启用
            return self.is_offline()
        return False

    def is_offline(self) -> bool:
        """离线检测（带缓存，5 秒内不重复探测）。"""
        now = time.time()
        if now - self._offline_checked_at < 5.0:
            return self._offline_detected
        self._offline_checked_at = now
        # 探测一个外部可达性（可替换为云端 API 探测）
        self._offline_detected = not self._probe_network()
        return self._offline_detected

    # --------------------------------------------------------
    # Ollama 交互
    # --------------------------------------------------------
    def _base_url(self) -> str:
        return self.config["ollama_host"].rstrip("/")

    def _ping_ollama(self) -> bool:
        try:
            req = urllib.request.Request(f"{self._base_url()}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _ensure_ollama_running(self) -> bool:
        """Ollama 未运行则自动启动（后台 serve，无窗口）。

        启动时强制设置 OLLAMA_MODELS 为无中文路径，规避中文用户名编码 bug。
        """
        if self._ping_ollama():
            return True
        # 全链审查修复：仅尝试拉起一次；QD_OLLAMA_NO_SPAWN=1（测试/离线）完全不 spawn
        if self._spawn_disabled or self._start_attempted:
            return False
        self._start_attempted = True
        try:
            exe = os.environ.get("OLLAMA_EXE") or DEFAULT_OLLAMA_EXE
            if not exe or not os.path.exists(exe):
                import shutil
                exe = shutil.which("ollama") or exe
            if not exe or not os.path.exists(exe):
                return False
            env = dict(os.environ)
            if DEFAULT_OLLAMA_MODELS:
                env["OLLAMA_MODELS"] = DEFAULT_OLLAMA_MODELS
            subprocess.Popen(
                [exe, "serve"],
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(20):  # 最多等 10 秒
                time.sleep(0.5)
                if self._ping_ollama():
                    return True
        except Exception:
            pass
        return False

    def _chat(self, model: str, prompt: str, images: Optional[list],
              system_prompt: str = "", **kwargs) -> Optional[str]:
        """调用 Ollama /api/chat。模型驻留由 keep_alive 控制。"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            # 非目标模型由互斥调度卸载；本模型保持短驻留，超时自动释放
            "keep_alive": kwargs.get("keep_alive", "5m"),
        }
        if images:
            payload["messages"][-1]["images"] = images
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=kwargs.get("timeout", 300)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("message", {}).get("content")
        except Exception as e:
            self._last_error = f"Ollama 调用失败: {e}"
            return None

    def _ensure_exclusive(self, target_model: str) -> bool:
        """确保 Ollama 中只驻留目标模型：查询 /api/ps 并卸载其他驻留模型。

        相比仅靠内部状态跟踪，此方法对历史残留（如手动加载的模型、
        上次异常退出的驻留）同样有效，保证"同一时间只驻留一个"。
        """
        try:
            req = urllib.request.Request(f"{self._base_url()}/api/ps", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # 查询失败不阻塞推理（模型本身可能未加载）
            return True
        ok = True
        for m in data.get("models", []):
            name = m.get("name", "")
            if name != target_model:
                if not self._unload_model(name):
                    ok = False
        return ok

    def _unload_model(self, model: str) -> bool:
        """通过 keep_alive=0 立即卸载模型，释放内存。"""
        payload = {
            "model": model,
            "keep_alive": 0,
            "messages": [],  # 空请求仅用于触发卸载
        }
        try:
            req = urllib.request.Request(
                f"{self._base_url()}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            self._last_error = f"卸载模型失败: {e}"
            return False

    def _probe_network(self) -> bool:
        """网络探测（可替换为对 intent 云端 API 的可达性探测）。"""
        try:
            urllib.request.urlopen("https://www.baidu.com", timeout=3)
            return True
        except Exception:
            return False

    # --------------------------------------------------------
    # 诊断
    # --------------------------------------------------------
    def get_state(self) -> dict:
        return {
            "active_slot": self._active_slot,
            "active_model": self.active_model,
            "offline": self._offline_detected,
            "ollama_available": self.is_available(),
            "last_error": self._last_error,
            "config": {k: v for k, v in self.config.items()},
        }


# ============================================================
# 自测
# ============================================================
if __name__ == "__main__":
    mgr = ModelManager()
    print("[状态]", json.dumps(mgr.get_state(), ensure_ascii=False, indent=2))
    if not mgr.is_available():
        print("[提示] Ollama 未运行，无法推理。请先启动 Ollama: ollama serve")
        print("       并拉取模型: ollama pull qwen3:4b && ollama pull qwen3-vl:2b")
