import copy
import errno
import os
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager

import toml
from loguru import logger

from app import __version__
from app.utils import utils

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
config_file = f"{root_dir}/config.toml"

# When deployed on cloud containers (e.g. Modal with /root/storage mounted),
# persist config.toml to /root/storage/config.toml so user settings, API keys,
# and selected options survive container restarts and browser reloads.
_storage_dir = os.path.join(root_dir, "storage")
if os.path.isdir(_storage_dir) and (os.environ.get("MODAL_IMAGE_ID") or os.environ.get("MODAL_SERVE") or root_dir == "/root"):
    _persistent_config = os.path.join(_storage_dir, "config.toml")
    if not os.path.isfile(_persistent_config) and os.path.isfile(config_file):
        try:
            shutil.copyfile(config_file, _persistent_config)
        except Exception:
            pass
    if os.path.isfile(_persistent_config):
        config_file = _persistent_config
_CONTAINER_CGROUP_MARKERS = ("docker", "containerd", "kubepods", "libpod", "podman")
_DOCKER_HOST_GATEWAY_NAME = "host.docker.internal"
_config_save_lock = threading.RLock()
_pending_config_lock = threading.RLock()
_pending_config_updates = {}
_pending_config_save_requested = False
_pending_config_flush_scheduled = False
_MISSING = object()
_DELETE = object()
_UTF8_BOM = "\ufeff"
_current_active_user_id = None
_thread_local = threading.local()
_user_registry = {}
_user_registry_lock = threading.RLock()


_volume_commit_lock = threading.Lock()
_pending_volume_commit = False


def _async_commit_worker():
    global _pending_volume_commit
    import time
    time.sleep(1.0)
    with _volume_commit_lock:
        if not _pending_volume_commit:
            return
        _pending_volume_commit = False
    try:
        if os.path.isdir("/root/storage") or os.environ.get("MODAL_IMAGE_ID") or os.environ.get("MODAL_SERVE"):
            import modal
            vol = modal.Volume.from_name("bangai-storage")
            vol.commit()
            logger.info("Asynchronously committed persistent storage to Modal volume 'bangai-storage'")
    except Exception as e:
        logger.debug(f"modal volume commit note: {e}")


def sync_cloud_volume():
    """Commit persistent storage changes to Modal volume in a non-blocking background thread."""
    if not (os.path.isdir("/root/storage") or os.environ.get("MODAL_IMAGE_ID") or os.environ.get("MODAL_SERVE")):
        return
    global _pending_volume_commit
    with _volume_commit_lock:
        _pending_volume_commit = True
    threading.Thread(target=_async_commit_worker, daemon=True).start()


class _SynchronizedConfig(dict):
    """保持 dict 使用方式不变，同时让运行期配置写操作服从同一把锁。"""

    def __setitem__(self, key, value):
        current = super().get(key, _MISSING)
        if current is not _MISSING and current == value:
            return
        with _config_save_lock:
            super().__setitem__(key, value)

    def __delitem__(self, key):
        with _config_save_lock:
            super().__delitem__(key)

    def clear(self):
        if not self:
            return
        with _config_save_lock:
            super().clear()

    def pop(self, key, default=_MISSING):
        if key not in self:
            if default is _MISSING:
                raise KeyError(key)
            return default
        with _config_save_lock:
            if default is _MISSING:
                return super().pop(key)
            return super().pop(key, default)

    def setdefault(self, key, default=None):
        current = super().get(key, _MISSING)
        if current is not _MISSING:
            return current
        with _config_save_lock:
            return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        if all(
            (current := dict.get(self, key, _MISSING)) is not _MISSING
            and current == value
            for key, value in changes.items()
        ):
            return
        with _config_save_lock:
            super().update(changes)


class _MultiTenantConfigSection(dict):
    """
    Thread-aware multi-tenant configuration section proxy.
    Automatically resolves accesses to the calling thread's isolated user configuration.
    Falls back cleanly to the process-wide default configuration.
    """

    def __init__(self, section_name: str, fallback_data: dict | None = None):
        super().__init__(fallback_data or {})
        self._section_name = section_name

    def _active_dict(self) -> dict:
        uid = utils.get_current_user_id() or getattr(_thread_local, "active_user_id", None) or _current_active_user_id
        if uid and uid in _user_registry:
            sections = _user_registry[uid].get("sections", {})
            if self._section_name in sections:
                return sections[self._section_name]
        return self

    def __getitem__(self, key):
        target = self._active_dict()
        if target is self:
            return super().__getitem__(key)
        return target[key]

    def __setitem__(self, key, value):
        current = self.get(key, _MISSING)
        if current is not _MISSING and current == value:
            return
        with _config_save_lock:
            super().__setitem__(key, value)
            target = self._active_dict()
            if target is not self:
                target[key] = value

    def __delitem__(self, key):
        with _config_save_lock:
            if key in self:
                super().__delitem__(key)
            target = self._active_dict()
            if target is not self and key in target:
                del target[key]

    def __contains__(self, key):
        target = self._active_dict()
        if target is self:
            return super().__contains__(key)
        return key in target

    def get(self, key, default=None):
        target = self._active_dict()
        if target is self:
            return super().get(key, default)
        return target.get(key, default)

    def setdefault(self, key, default=None):
        current = self.get(key, _MISSING)
        if current is not _MISSING:
            return current
        with _config_save_lock:
            super().setdefault(key, default)
            target = self._active_dict()
            if target is not self:
                return target.setdefault(key, default)
            return super().setdefault(key, default)

    def pop(self, key, *args):
        if key not in self:
            if not args:
                raise KeyError(key)
            return args[0]
        with _config_save_lock:
            super().pop(key, *args)
            target = self._active_dict()
            if target is not self:
                return target.pop(key, *args)
            return super().pop(key, *args)

    def update(self, *args, **kwargs):
        changes = dict(*args, **kwargs)
        if all(
            (current := self.get(key, _MISSING)) is not _MISSING
            and current == value
            for key, value in changes.items()
        ):
            return
        with _config_save_lock:
            super().update(changes)
            target = self._active_dict()
            if target is not self:
                target.update(changes)

    def clear(self):
        with _config_save_lock:
            super().clear()
            target = self._active_dict()
            if target is not self:
                target.clear()

    def keys(self):
        target = self._active_dict()
        return target.keys() if target is not self else super().keys()

    def values(self):
        target = self._active_dict()
        return target.values() if target is not self else super().values()

    def items(self):
        target = self._active_dict()
        return target.items() if target is not self else super().items()

    def copy(self):
        target = self._active_dict()
        return dict(target) if target is not self else super().copy()

    def __iter__(self):
        target = self._active_dict()
        return iter(target) if target is not self else super().__iter__()

    def __len__(self):
        target = self._active_dict()
        return len(target) if target is not self else super().__len__()


def _pending_update_key(config_section, key, user_id=None):
    """为进程内固定配置分区与租户生成待更新键。"""
    if user_id is None:
        user_id = utils.get_current_user_id()
    return user_id, id(config_section), key


def update_config_nonblocking(config_section, key, value):
    """
    非阻塞更新 WebUI 的运行期配置。

    视频生成会持有 ``runtime_config_lock``，确保同一任务不会在执行中途切换
    Provider、密钥或语音配置。Streamlit 控件发生变化时不能等待这把长任务锁，
    否则浏览器会表现为页面冻结。锁空闲时立即更新；锁繁忙时只保留每个配置项
    的最新值，并在当前任务释放锁时统一应用。

    返回 True 表示值已经生效，False 表示已进入待更新队列。
    """
    # 所有更新都先进入同一队列，再尝试获取配置锁。这样多个页面同时修改同一
    # 配置项时，写入队列的先后顺序就是最终顺序，不会出现较早线程在获取锁后
    # 把较新线程已经排队的值误删掉。
    current_uid = utils.get_current_user_id()
    with _pending_config_lock:
        _pending_config_updates[_pending_update_key(config_section, key, current_uid)] = (
            current_uid,
            config_section,
            key,
            copy.deepcopy(value),
        )

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        # 调用方通常会在本次 Streamlit rerun 末尾请求保存，但不能依赖这一步
        # 一定执行。例如页面中途异常或更新恰好发生在任务退出保存阶段时，仍需
        # 有后台刷新线程保证排队值最终生效。
        _schedule_deferred_config_flush()
        return False

    try:
        _apply_pending_config_updates_locked()
        return config_section.get(key, _MISSING) == value
    finally:
        _config_save_lock.release()


def delete_config_nonblocking(config_section, key):
    """
    非阻塞删除 WebUI 配置项。

    “使用默认值”需要真正移除配置项，而不是写入空字符串。视频任务占用配置
    锁时，删除意图会覆盖同一配置项之前排队的更新，并在任务结束后执行。
    """
    current_uid = utils.get_current_user_id()
    with _pending_config_lock:
        _pending_config_updates[_pending_update_key(config_section, key, current_uid)] = (
            current_uid,
            config_section,
            key,
            _DELETE,
        )

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        _schedule_deferred_config_flush()
        return False

    try:
        _apply_pending_config_updates_locked()
        return key not in config_section
    finally:
        _config_save_lock.release()


def _apply_pending_config_updates_locked():
    """在持有配置写锁时应用 WebUI 暂存的最新配置值。"""
    with _pending_config_lock:
        updates = list(_pending_config_updates.values())
        _pending_config_updates.clear()

    affected_uids = set()
    for entry in updates:
        if len(entry) == 4:
            uid, config_section, key, value = entry
        else:
            config_section, key, value = entry
            uid = utils.get_current_user_id()
        affected_uids.add(uid)
        orig_uid = utils.get_current_user_id()
        try:
            utils.set_current_user_id(uid)
            if value is _DELETE:
                config_section.pop(key, None)
            else:
                config_section[key] = value
        finally:
            utils.set_current_user_id(orig_uid)
    return affected_uids


def snapshot_config_with_pending(config_section):
    """
    返回配置分区的有效快照，并合并尚未应用的 WebUI 更新。

    视频任务持锁期间不能改写全局配置，但用户仍可准备下一条内容。LLM 请求
    使用这个快照后，界面中刚选择的 Provider、模型和密钥会参与新请求，同时
    不会改变正在执行的视频任务。
    """
    uid = utils.get_current_user_id()
    with _pending_config_lock:
        snapshot = dict(config_section)
        section_id = id(config_section)
        for pending_key, entry in _pending_config_updates.items():
            if len(entry) == 4:
                entry_uid, p_section, k, value = entry
                p_section_id = id(p_section)
            else:
                entry_uid = uid
                p_section, k, value = entry
                p_section_id = id(p_section)
            if entry_uid != uid or p_section_id != section_id:
                continue
            if value is _DELETE:
                snapshot.pop(k, None)
            else:
                snapshot[k] = copy.deepcopy(value)
    return snapshot


def _flush_pending_config_locked(*, suppress_save_errors):
    """在持有配置写锁时应用并保存当前所有待处理配置。"""
    global _pending_config_save_requested

    affected_uids = _apply_pending_config_updates_locked()
    with _pending_config_lock:
        save_requested = _pending_config_save_requested
        _pending_config_save_requested = False

    if not affected_uids and not save_requested:
        return True

    all_uids = set(affected_uids)
    curr_uid = utils.get_current_user_id()
    if save_requested:
        all_uids.add(curr_uid)

    try:
        if not all_uids or (len(all_uids) == 1 and curr_uid in all_uids):
            save_config()
        else:
            for uid in all_uids:
                if uid == curr_uid:
                    save_config()
                else:
                    save_config(uid)
        return True
    except Exception as exc:
        # 内存中的配置已经成功应用，保存失败时只保留待保存标记。视频任务不应
        # 因配置文件暂时不可写而被改判失败；下一次页面交互会再次触发保存。
        with _pending_config_lock:
            _pending_config_save_requested = True
        if not suppress_save_errors:
            raise
        logger.exception(f"failed to save deferred runtime config: {exc}")
        return False


def _run_deferred_config_flush():
    """等待长任务释放配置锁，并可靠清空期间积累的配置更新。"""
    global _pending_config_flush_scheduled

    while True:
        with _config_save_lock:
            flush_succeeded = _flush_pending_config_locked(
                suppress_save_errors=True
            )

        with _pending_config_lock:
            has_pending_work = bool(
                _pending_config_updates or _pending_config_save_requested
            )
            if not flush_succeeded or not has_pending_work:
                _pending_config_flush_scheduled = False
                return


def _schedule_deferred_config_flush():
    """保证同一时间最多只有一个后台线程等待刷新配置。"""
    global _pending_config_flush_scheduled

    with _pending_config_lock:
        if _pending_config_flush_scheduled:
            return
        _pending_config_flush_scheduled = True

    threading.Thread(
        target=_run_deferred_config_flush,
        name="mpt-config-flush",
        daemon=True,
    ).start()


def try_save_config():
    """
    非阻塞保存 WebUI 配置，锁繁忙时交由当前长任务结束后保存。

    普通 API、CLI 和维护脚本仍可调用 ``save_config`` 获得原来的阻塞写入语义；
    只有 Streamlit rerun 使用本函数，避免页面为等待视频任务而长时间无响应。
    """
    global _pending_config_save_requested

    with _pending_config_lock:
        _pending_config_save_requested = True

    acquired = _config_save_lock.acquire(blocking=False)
    if not acquired:
        _schedule_deferred_config_flush()
        return False

    try:
        return _flush_pending_config_locked(suppress_save_errors=False)
    finally:
        _config_save_lock.release()


@contextmanager
def runtime_config_lock():
    """
    在一次依赖全局配置的完整操作期间阻止其它 WebUI 会话改写配置。

    当前项目默认绑定本地回环地址，配置仍然是单用户全局配置。这个轻量锁主要
    保护生成、试听等长操作，避免另一个标签页在操作中途切换 Provider 或密钥。
    """
    with _config_save_lock:
        # 如果上一个短操作释放锁时后台刷新线程尚未获得调度，新任务必须在读取
        # Provider、密钥等全局配置前先应用队列，不能继续使用旧配置执行整条流水线。
        _flush_pending_config_locked(suppress_save_errors=True)
        try:
            yield
        finally:
            _flush_pending_config_locked(suppress_save_errors=True)


@contextmanager
def try_runtime_config_lock():
    """
    尝试获取运行期配置锁，并立即返回是否成功。

    WebUI 试听属于用户主动触发的短操作，不应在后台视频任务持锁时等待数分钟。
    调用方可以在未获取锁时就近提示用户稍后重试；成功获取后仍能保证试听期间
    Provider、密钥和模型配置不会被其它会话修改。
    """
    acquired = _config_save_lock.acquire(blocking=False)
    try:
        if acquired:
            _flush_pending_config_locked(suppress_save_errors=True)
        yield acquired
    finally:
        if acquired:
            _flush_pending_config_locked(suppress_save_errors=True)
            _config_save_lock.release()


def is_running_in_container(
    dockerenv_path: str = "/.dockerenv",
    containerenv_path: str = "/run/.containerenv",
    cgroup_path: str = "/proc/1/cgroup",
) -> bool:
    """
    判断当前进程是否运行在容器内。

    这个判断主要用于 Ollama 默认地址选择：
    - 普通本机运行时，`localhost` 指向用户机器本身；
    - Docker 容器内，`localhost` 指向容器自己，访问宿主机 Ollama
      通常需要使用 `host.docker.internal`。

    不能只判断 `/proc/1/cgroup` 是否存在，因为普通 Linux 也会有这个文件。
    这里只在检测到明确的容器标记时返回 True，避免误伤非 Docker Linux 用户。
    参数保留为可注入路径，便于单元测试覆盖不同运行环境。
    """
    if os.path.isfile(dockerenv_path) or os.path.isfile(containerenv_path):
        return True

    try:
        with open(cgroup_path, mode="r", encoding="utf-8") as fp:
            cgroup_content = fp.read().lower()
    except OSError:
        return False

    return any(marker in cgroup_content for marker in _CONTAINER_CGROUP_MARKERS)


def _can_resolve_hostname(hostname: str) -> bool:
    try:
        socket.gethostbyname(hostname)
    except OSError:
        return False
    return True


def _decode_linux_route_gateway(hex_gateway: str) -> str:
    # /proc/net/route 里的 Gateway 是 16 进制小端序，例如 010011AC 表示
    # 172.17.0.1。这里单独解析，是为了在原生 Linux Docker 没有
    # host.docker.internal DNS 记录时，还能尝试访问容器默认网关上的宿主机。
    if len(hex_gateway) != 8:
        raise ValueError("invalid gateway length")

    octets = [
        str(int(hex_gateway[index : index + 2], 16)) for index in range(6, -1, -2)
    ]
    return ".".join(octets)


def get_container_default_gateway_ip(route_path: str = "/proc/net/route") -> str:
    """
    读取 Linux 容器里的默认网关 IP。

    Docker Desktop 通常提供 `host.docker.internal`，但原生 Linux Docker
    默认不一定提供这个 DNS 名称。默认网关通常可以作为访问宿主机服务的
    兜底地址；如果用户的 Ollama 只监听 127.0.0.1，则仍需要用户让
    Ollama 监听宿主机网卡或手动配置 `ollama_base_url`。
    """
    try:
        with open(route_path, mode="r", encoding="utf-8") as fp:
            route_lines = fp.readlines()
    except OSError:
        return ""

    for line in route_lines[1:]:
        fields = line.strip().split()
        if len(fields) < 3:
            continue

        destination = fields[1]
        gateway = fields[2]
        if destination != "00000000" or gateway == "00000000":
            continue

        try:
            return _decode_linux_route_gateway(gateway)
        except ValueError:
            logger.warning(f"invalid container gateway route entry: {line.strip()}")
            return ""

    return ""


def get_default_ollama_base_url() -> str:
    """
    返回 Ollama 的默认 OpenAI-compatible base_url。

    用户显式配置 `ollama_base_url` 时不会走这里；这里只处理“未配置时的
    最佳默认值”。容器内默认指向宿主机，普通本机运行默认指向 localhost。
    """
    if not is_running_in_container():
        return "http://localhost:11434/v1"

    if _can_resolve_hostname(_DOCKER_HOST_GATEWAY_NAME):
        return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"

    gateway_ip = get_container_default_gateway_ip()
    if gateway_ip:
        logger.info(
            "host.docker.internal is not resolvable, fallback to container "
            f"default gateway for Ollama: {gateway_ip}"
        )
        return f"http://{gateway_ip}:11434/v1"

    logger.warning(
        "failed to resolve host.docker.internal and container default gateway; "
        "fallback to host.docker.internal for Ollama"
    )
    return f"http://{_DOCKER_HOST_GATEWAY_NAME}:11434/v1"


def _load_toml_config(config_path: str):
    """
    加载 TOML，并兼容 Windows 编辑器可能写入的重复 UTF-8 BOM。

    ``utf-8-sig`` 只会移除文件开头的一个 BOM。部分 Windows 编辑器或
    解压、保存流程可能再次写入 BOM，导致第二个不可见字符进入 TOML
    解析器并在第一行报错。这里仅在标准解析失败后做一次只读归一化，
    不回写原文件，避免意外覆盖用户已经填写的 API Key。
    """
    try:
        return toml.load(config_path)
    except (toml.TomlDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "load config failed, retry with UTF-8 BOM compatibility: "
            f"path={config_path}, error={type(exc).__name__}: {exc}"
        )

    try:
        with open(config_path, mode="r", encoding="utf-8-sig") as fp:
            config_content = fp.read()

        normalized_content = config_content.lstrip(_UTF8_BOM)
        removed_bom_count = len(config_content) - len(normalized_content)
        if removed_bom_count:
            logger.warning(
                "removed repeated UTF-8 BOM characters while loading config: "
                f"path={config_path}, count={removed_bom_count}"
            )
        return toml.loads(normalized_content)
    except (toml.TomlDecodeError, UnicodeDecodeError) as exc:
        logger.error(
            "config file is not valid TOML after UTF-8 BOM normalization: "
            f"path={config_path}, error={type(exc).__name__}: {exc}"
        )
        raise


def _sanitize_config_dict(cfg: dict) -> dict:
    """Ensure all API keys, secrets, tokens, and personal prompts are completely empty (strict Bring-Your-Own-Key system)."""
    app_sec = cfg.setdefault("app", {})
    # Strip LLM and external service keys
    for k in [
        "gemini_api_key", "openai_api_key", "anthropic_api_key", "deepseek_api_key",
        "qwen_api_key", "azure_api_key", "moonshot_api_key", "shengsuanyun_api_key",
        "apimart_api_key", "fluxionai_api_key", "cheaperinference_api_key", "grok_api_key",
        "minimax_api_key", "mimo_api_key", "cloudflare_api_key", "modelscope_api_key",
        "aihubmix_api_key", "aimlapi_api_key", "evolink_api_key", "openrouter_api_key",
        "api_route_api_key", "oneapi_api_key", "groq_api_key", "pollinations_api_key",
        "loomloom_api_token", "volcengine_seedance_api_key", "ofox_api_key", "muapi_api_key",
        "metaso_minimax_api_key", "sonilo_api_key", "upload_post_api_key", "redis_password"
    ]:
        if k in app_sec:
            app_sec[k] = ""

    # Strip list-based keys
    app_sec["pexels_api_keys"] = []
    app_sec["pixabay_api_keys"] = []
    app_sec["coverr_api_keys"] = []
    app_sec["wavespeed_api_keys"] = []
    app_sec["openai_image_api_keys"] = []
    app_sec["twelvelabs_api_keys"] = []

    # Strip voice / TTS service keys (BYOK services - no pre-applied keys)
    for sec_name in [
        "azure", "siliconflow", "minimax_tts", "elevenlabs",
        "chatterbox", "kokoro", "fish_audio", "voxcpm", "json2video"
    ]:
        sec = cfg.setdefault(sec_name, {})
        for key_field in ["api_key", "speech_key", "cloudconvert_api_key"]:
            if key_field in sec:
                sec[key_field] = ""

    # Strip any known legacy platform or test keys that might linger
    legacy_keys = {
        "5RBJDXZfAjfT1CSJ6F18DlZ7hvACfw7hfCtEdC1p",
        "qGkUqZ4rFf14aQc2qGcl12b8z",
        "AIzaSyC_ozuedo6ueobvhbHDA6OFYa-d4uKDAKo",
        "wnzipdxV7TGWJQwatBQeOzMRL7LnYHbAJS09rRxwMpvuv89OSrs8B6Um"
    }
    j2v = cfg.setdefault("json2video", {})
    if j2v.get("api_key") in legacy_keys:
        j2v["api_key"] = ""

    yt = cfg.setdefault("youtube_oauth", {})
    yt["selected_channel_id"] = ""

    # Strip personal user prompts so new users get clean input boxes
    ui_sec = cfg.setdefault("ui", {})
    ui_sec["video_subject"] = ""
    ui_sec["video_script"] = ""
    ui_sec["video_script_prompt"] = ""
    ui_sec["custom_system_prompt"] = ""

    return cfg


def load_config():
    # fix: IsADirectoryError: [Errno 21] Is a directory: '/MoneyPrinterTurbo/config.toml'
    if os.path.isdir(config_file):
        shutil.rmtree(config_file)

    if not os.path.isfile(config_file):
        example_file = f"{root_dir}/config.example.toml"
        if os.path.isfile(example_file):
            shutil.copyfile(example_file, config_file)
            logger.info("copy config.example.toml to config.toml")

    logger.info(f"load config from file: {config_file}")

    loaded = _load_toml_config(config_file)
    _sanitize_config_dict(loaded)
    return loaded


def switch_user_config(user_id: str):
    """Switch runtime config to an isolated user-scoped config.toml."""
    global config_file, _current_active_user_id
    if not user_id:
        return
    clean_uid = str(user_id).strip()
    utils.set_current_user_id(clean_uid)
    _thread_local.active_user_id = clean_uid
    _current_active_user_id = clean_uid

    user_storage = os.path.join(root_dir, "storage", "users", clean_uid)
    os.makedirs(user_storage, exist_ok=True)
    user_config = os.path.join(user_storage, "config.toml")

    with _user_registry_lock:
        if clean_uid in _user_registry:
            config_file = _user_registry[clean_uid]["config_file"]
            return

        # If user doesn't have a config yet, create a clean sanitized config
        if not os.path.isfile(user_config):
            base_template = os.path.join(root_dir, "storage", "config.toml")
            if not os.path.isfile(base_template):
                base_template = os.path.join(root_dir, "config.toml")
            try:
                template_cfg = _load_toml_config(base_template) if os.path.isfile(base_template) else {}
                _sanitize_config_dict(template_cfg)
                with open(user_config, "w", encoding="utf-8") as f:
                    toml.dump(template_cfg, f)
            except Exception as e:
                logger.warning(f"failed to initialize clean user config: {e}")

        if os.path.isfile(user_config):
            config_file = user_config
            try:
                new_cfg = _load_toml_config(config_file)

                # Ensure no legacy platform keys linger in user config
                legacy_keys = {
                    "5RBJDXZfAjfT1CSJ6F18DlZ7hvACfw7hfCtEdC1p",
                    "qGkUqZ4rFf14aQc2qGcl12b8z",
                    "AIzaSyC_ozuedo6ueobvhbHDA6OFYa-d4uKDAKo",
                    "wnzipdxV7TGWJQwatBQeOzMRL7LnYHbAJS09rRxwMpvuv89OSrs8B6Um"
                }
                j2v = new_cfg.setdefault("json2video", {})
                if j2v.get("api_key") in legacy_keys:
                    j2v["api_key"] = ""
                    try:
                        with open(user_config, "w", encoding="utf-8") as f:
                            toml.dump(new_cfg, f)
                    except Exception:
                        pass

                user_sections = {
                    "app": _SynchronizedConfig(new_cfg.get("app", {})),
                    "azure": _SynchronizedConfig(new_cfg.get("azure", {})),
                    "siliconflow": _SynchronizedConfig(new_cfg.get("siliconflow", {})),
                    "minimax_tts": _SynchronizedConfig(new_cfg.get("minimax_tts", {})),
                    "elevenlabs": _SynchronizedConfig(new_cfg.get("elevenlabs", {})),
                    "chatterbox": _SynchronizedConfig(new_cfg.get("chatterbox", {})),
                    "kokoro": _SynchronizedConfig(new_cfg.get("kokoro", {})),
                    "fish_audio": _SynchronizedConfig(new_cfg.get("fish_audio", {})),
                    "voxcpm": _SynchronizedConfig(new_cfg.get("voxcpm", {})),
                    "json2video": _SynchronizedConfig(new_cfg.get("json2video", {})),
                    "ui": _SynchronizedConfig(new_cfg.get("ui", {"hide_log": False})),
                    "youtube_oauth": _SynchronizedConfig(new_cfg.get("youtube_oauth", {
                        "enabled": True,
                        "upload_mode": "manual",
                        "selected_channel_id": "",
                        "default_privacy_status": "public",
                        "made_for_kids": False,
                    })),
                }

                _user_registry[clean_uid] = {
                    "config_file": user_config,
                    "cfg": new_cfg,
                    "sections": user_sections,
                }
                logger.info(f"switched to isolated user config for user '{clean_uid}': {config_file}")
            except Exception as e:
                logger.warning(f"failed to load user config {config_file}: {e}")


def get_active_user_id() -> str:
    """Return the currently active user ID for the calling thread."""
    return getattr(_thread_local, "active_user_id", None) or _current_active_user_id or ""


def save_config(user_id: str | None = None):
    """
    Atomically saves the runtime configuration for the active user (or specified user).
    Includes all configuration sections and synchronizes cloud storage volume.
    """
    with _config_save_lock:
        uid = user_id or utils.get_current_user_id() or getattr(_thread_local, "active_user_id", None) or _current_active_user_id
        target_file = config_file
        if uid and uid in _user_registry:
            target_file = _user_registry[uid]["config_file"]
            sections = _user_registry[uid]["sections"]
            config_to_save = dict(_user_registry[uid]["cfg"])
            for sec_name, sec_dict in sections.items():
                config_to_save[sec_name] = dict(sec_dict)
        else:
            config_to_save = dict(_cfg)
            config_to_save["app"] = dict(app)
            config_to_save["azure"] = dict(azure)
            config_to_save["siliconflow"] = dict(siliconflow)
            config_to_save["minimax_tts"] = dict(minimax_tts)
            config_to_save["elevenlabs"] = dict(elevenlabs)
            config_to_save["chatterbox"] = dict(chatterbox)
            config_to_save["kokoro"] = dict(kokoro)
            config_to_save["fish_audio"] = dict(fish_audio)
            config_to_save["voxcpm"] = dict(voxcpm)
            config_to_save["json2video"] = dict(json2video)
            config_to_save["ui"] = dict(ui)
            config_to_save["youtube_oauth"] = dict(youtube_oauth)

        serialized_config = toml.dumps(config_to_save)

        try:
            with open(target_file, mode="r", encoding="utf-8") as f:
                if f.read() == serialized_config:
                    return
        except (OSError, UnicodeError):
            pass

        temp_path = ""
        try:
            os.makedirs(os.path.dirname(target_file), exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=".config-",
                suffix=".toml.tmp",
                dir=os.path.dirname(target_file),
            )
            with os.fdopen(fd, mode="w", encoding="utf-8") as f:
                f.write(serialized_config)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(temp_path, target_file)
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                logger.warning(
                    "atomic config replacement is unavailable for the mounted "
                    f"file, fallback to in-place write: {target_file}"
                )
                with open(target_file, mode="w", encoding="utf-8") as f:
                    f.write(serialized_config)
                    f.flush()
                    os.fsync(f.fileno())
            sync_cloud_volume()
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


_cfg = load_config()
app = _MultiTenantConfigSection("app", _cfg.get("app", {}))
whisper = _cfg.get("whisper", {})
proxy = _cfg.get("proxy", {})
azure = _MultiTenantConfigSection("azure", _cfg.get("azure", {}))
siliconflow = _MultiTenantConfigSection("siliconflow", _cfg.get("siliconflow", {}))
minimax_tts = _MultiTenantConfigSection("minimax_tts", _cfg.get("minimax_tts", {}))
elevenlabs = _MultiTenantConfigSection("elevenlabs", _cfg.get("elevenlabs", {}))
chatterbox = _MultiTenantConfigSection("chatterbox", _cfg.get("chatterbox", {}))
kokoro = _MultiTenantConfigSection("kokoro", _cfg.get("kokoro", {}))
fish_audio = _MultiTenantConfigSection("fish_audio", _cfg.get("fish_audio", {}))
voxcpm = _MultiTenantConfigSection("voxcpm", _cfg.get("voxcpm", {}))
json2video = _MultiTenantConfigSection("json2video", _cfg.get("json2video", {}))
ui = _MultiTenantConfigSection(
    "ui",
    _cfg.get(
        "ui",
        {
            "hide_log": False,
        },
    ),
)
youtube_oauth = _MultiTenantConfigSection(
    "youtube_oauth",
    _cfg.get(
        "youtube_oauth",
        {
            "enabled": True,
            "upload_mode": "manual",
            "selected_channel_id": "",
            "default_privacy_status": "public",
            "made_for_kids": False,
        },
    ),
)

hostname = socket.gethostname()

log_level = _cfg.get("log_level", "DEBUG")
listen_host = _cfg.get("listen_host", "0.0.0.0")
listen_port = _cfg.get("listen_port", 8080)
project_name = _cfg.get("project_name", "MoneyPrinterTurbo")
project_description = _cfg.get(
    "project_description",
    "<a href='https://github.com/harry0703/MoneyPrinterTurbo'>https://github.com/harry0703/MoneyPrinterTurbo</a>",
)
project_version = _cfg.get("project_version", __version__)
reload_debug = False

app["redis_host"] = os.getenv(
    "MPT_APP_REDIS_HOST",
    os.getenv("REDIS_HOST", app.get("redis_host", "localhost")),
)

ffmpeg_path = app.get("ffmpeg_path", "")
if ffmpeg_path and os.path.isfile(ffmpeg_path):
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_path

logger.info(f"{project_name} v{project_version}")
