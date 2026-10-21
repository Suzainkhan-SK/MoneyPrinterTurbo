import os
import threading
from collections import deque

from loguru import logger

from app.config import config
from app.controllers.manager.memory_manager import InMemoryTaskManager
from app.models import const
from app.models.schema import VideoParams
from app.services import state as sm
from app.services import task as tm
from app.services.loomloom import LoomLoomConfirmedVideoRequest
from app.utils.logging_utils import format_log_record
from app.utils import utils


# WebUI parallel cloud task manager: 20 concurrent rendering workers, with FIFO queue
# tracking for any requests submitted beyond the active 20 slots.
_task_manager = InMemoryTaskManager(
    max_concurrent_tasks=20,
    max_queued_tasks=max(1, int(config.app.get("max_queued_tasks", 200))),
)
_task_logs: dict[str, deque[str]] = {}
_task_logs_lock = threading.RLock()
_MAX_LOG_TASKS = 50
_MAX_LOG_RECORDS_PER_TASK = 1000

_queued_task_ids: list[str] = []
_queued_lock = threading.RLock()

# Streamlit polls every 0.5s to display real-time logs and queue position updates.
TASK_LOG_REFRESH_INTERVAL_SECONDS = 0.5


def _refresh_queued_positions() -> None:
    """Updates the queue position and estimated wait time for all tasks waiting in line."""
    with _queued_lock:
        for idx, q_id in enumerate(_queued_task_ids):
            pos = idx + 1
            est_wait = f"{max(1, round(pos * 1.5))} min" if pos <= 2 else f"{round(pos * 1.5)} mins"
            sm.state.patch_task(
                q_id,
                state="queued",
                queue_position=pos,
                estimated_wait=est_wait,
            )


def has_active_tasks() -> bool:
    """Check if there are any actively rendering or queued tasks in this process."""
    with _task_manager.lock:
        if _task_manager.current_tasks > 0:
            return True
    with _queued_lock:
        return len(_queued_task_ids) > 0


def _append_task_log(task_id: str, message: str) -> None:
    """按任务保存有限数量的日志，供 Streamlit Fragment 安全轮询。"""
    cleaned = message.rstrip()
    with _task_logs_lock:
        records = _task_logs.get(task_id)
        if records is None:
            if len(_task_logs) >= _MAX_LOG_TASKS:
                oldest_task_id = next(iter(_task_logs))
                _task_logs.pop(oldest_task_id, None)
            records = deque(maxlen=_MAX_LOG_RECORDS_PER_TASK)
            _task_logs[task_id] = records
        records.append(cleaned)

    try:
        task_dir = utils.task_dir(task_id)
        if os.path.isdir(task_dir):
            with open(os.path.join(task_dir, "task.log"), "a", encoding="utf-8") as lf:
                lf.write(cleaned + "\n")
    except Exception:
        pass


def get_task_logs(task_id: str) -> list[str]:
    """返回日志快照，支持从内存队列或持久化 task.log 文件回读（适用于跨容器/Modal云工作线程）。"""
    with _task_logs_lock:
        mem_logs = list(_task_logs.get(task_id, ()))
    if mem_logs:
        return mem_logs

    try:
        import glob
        task_dir = utils.task_dir(task_id)
        cand_log_files = [os.path.join(task_dir, "task.log")]
        base_storage = os.path.abspath(os.path.join(utils.root_dir(), "storage"))
        cand_log_files += glob.glob(os.path.join(base_storage, "users", "*", "tasks", task_id, "task.log"))
        cand_log_files += glob.glob(os.path.join(base_storage, "tasks", task_id, "task.log"))
        for log_f in cand_log_files:
            if os.path.isfile(log_f):
                with open(log_f, "r", encoding="utf-8", errors="replace") as f:
                    lines = [line.rstrip() for line in f.readlines()]
                    if lines:
                        return lines[-_MAX_LOG_RECORDS_PER_TASK:]
    except Exception:
        pass

    return []


def _run_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
) -> dict:
    """
    在后台线程中执行现有视频流水线。最多支持 20 个工作线程同时并行渲染。
    """
    with _queued_lock:
        if task_id in _queued_task_ids:
            _queued_task_ids.remove(task_id)
    _refresh_queued_positions()

    task_user_id = params.user_id or utils.get_current_user_id() or "guest"
    utils.set_current_user_id(task_user_id)
    if not has_active_tasks():
        try:
            import modal
            modal.Volume.from_name("bangai-storage").reload()
        except Exception:
            pass

    try:
        user_storage_path = os.path.abspath(os.path.join(utils.root_dir(), "storage", "users", task_user_id))
        utils.safe_makedirs(user_storage_path, exist_ok=True)
    except Exception as udir_err:
        logger.warning(f"user_storage_path initialization note: {udir_err}")

    try:
        config.switch_user_config(task_user_id)
    except Exception as sw_err:
        logger.warning(f"switch_user_config non-fatal note: {sw_err}")

    try:
        # Pre-create task dir using safe_makedirs
        utils.task_dir(task_id, create=True)
    except Exception as td_err:
        logger.warning(f"task_dir initialization note: {td_err}")

    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=5,
        queue_position=0,
    )

    log_handler_id = None
    worker_thread_id = threading.get_ident()
    try:
        if capture_logs:
            log_handler_id = logger.add(
                lambda message: _append_task_log(task_id, str(message)),
                level="DEBUG",
                format=format_log_record,
                colorize=False,
                filter=lambda record: record["thread"].id == worker_thread_id,
            )

        try:
            config.try_save_config()
        except Exception as sc_err:
            logger.warning(f"try_save_config non-fatal note in generation worker: {sc_err}")
        return tm.start(
            task_id=task_id,
            params=params,
            voice_preview=voice_preview,
            loomloom_video_request=loomloom_video_request,
            voxcpm_reference_audio=voxcpm_reference_audio,
            voxcpm_prompt_audio=voxcpm_prompt_audio,
            voxcpm_prompt_text=voxcpm_prompt_text,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        failure = {
            "task_id": task_id,
            "state": const.TASK_STATE_FAILED,
            "progress": 0,
            "failed_stage": "webui_worker",
            "error": error,
        }
        sm.state.update_task(
            task_id,
            state=failure["state"],
            progress=failure["progress"],
            failed_stage=failure["failed_stage"],
            error=failure["error"],
        )
        logger.exception(
            f"unexpected WebUI generation worker failure, "
            f"task_id={task_id}, error={exc}"
        )
        return failure
    finally:
        with _queued_lock:
            if task_id in _queued_task_ids:
                _queued_task_ids.remove(task_id)
        _refresh_queued_positions()
        if log_handler_id is not None:
            try:
                logger.remove(log_handler_id)
            except ValueError:
                logger.debug(
                    f"WebUI task log handler already removed: task_id={task_id}"
                )
        try:
            config.sync_cloud_volume()
        except Exception:
            pass


def submit_generation(
    task_id: str,
    params: VideoParams,
    capture_logs: bool = True,
    voice_preview: dict | None = None,
    loomloom_video_request: LoomLoomConfirmedVideoRequest | None = None,
    voxcpm_reference_audio: bytes | None = None,
    voxcpm_prompt_audio: bytes | None = None,
    voxcpm_prompt_text: str = "",
) -> None:
    """
    登记并提交 WebUI 视频生成任务。若当前 20 个渲染槽位已满，则自动进入排队队列并计算排位。
    """
    task_params = params.model_copy(deep=True)
    voice_preview_snapshot = dict(voice_preview) if voice_preview else None
    voxcpm_reference_audio_snapshot = voxcpm_reference_audio
    voxcpm_prompt_audio_snapshot = voxcpm_prompt_audio
    voxcpm_prompt_text_snapshot = voxcpm_prompt_text or ""
    loomloom_request_snapshot = loomloom_video_request

    with _task_manager.lock:
        is_at_capacity = _task_manager.current_tasks >= _task_manager.max_concurrent_tasks

    if is_at_capacity:
        with _queued_lock:
            if task_id not in _queued_task_ids:
                _queued_task_ids.append(task_id)
            pos = len(_queued_task_ids)
        est_wait = f"{max(1, round(pos * 1.5))} min" if pos <= 2 else f"{round(pos * 1.5)} mins"
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_QUEUED,
            status="queued",
            progress=0,
            queue_position=pos,
            estimated_wait=est_wait,
            video_subject=task_params.video_subject or task_params.video_script or task_id,
            user_id=utils.get_current_user_id() or "guest",
        )
    else:
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_PROCESSING,
            progress=0,
            queue_position=0,
            video_subject=task_params.video_subject or task_params.video_script or task_id,
            user_id=utils.get_current_user_id() or "guest",
        )

    try:
        _task_manager.add_task(
            _run_generation,
            task_id=task_id,
            params=task_params,
            capture_logs=capture_logs,
            voice_preview=voice_preview_snapshot,
            loomloom_video_request=loomloom_request_snapshot,
            voxcpm_reference_audio=voxcpm_reference_audio_snapshot,
            voxcpm_prompt_audio=voxcpm_prompt_audio_snapshot,
            voxcpm_prompt_text=voxcpm_prompt_text_snapshot,
        )
        logger.info(f"WebUI task {task_id} successfully queued to background worker (real-time terminal streaming active)")
    except Exception as exc:
        with _queued_lock:
            if task_id in _queued_task_ids:
                _queued_task_ids.remove(task_id)
        _refresh_queued_positions()
        error = f"{type(exc).__name__}: {exc}"
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_FAILED,
            progress=0,
            failed_stage="scheduling",
            error=error,
        )
        logger.exception(
            f"failed to submit WebUI generation task, task_id={task_id}, error={exc}"
        )
        raise
