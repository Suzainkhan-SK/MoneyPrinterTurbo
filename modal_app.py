import os
import sys
import json
from uuid import uuid4
import modal
from fastapi import APIRouter, FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger



# 1. Persistent cloud volume for generated tasks, audio, and videos (50 GB Free)
volume = modal.Volume.from_name("bangai-storage", create_if_missing=True)

# 2. Build cloud container image with FFmpeg, Devanagari Hindi fonts, and dependencies
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-deva", "fonts-noto-core")
    .pip_install(
        "toml",
        "fastapi==0.136.3",
        "uvicorn==0.32.1",
        "streamlit==1.59.1",
        "streamlit-tour==1.1.0",
        "moviepy==2.2.1",
        "edge-tts==7.2.7",
        "google-genai==2.11.0",
        "requests==2.33.1",
        "loguru==0.7.3",
        "pydantic",
        "python-multipart",
        "pyyaml",
        "pydub",
        "openai",
        "packaging",
        "pillow",
        "redis==5.2.0",
        "litellm==1.86.2",
        "socksio==1.0.0",
        "modal",
    )
    .add_local_file("patch_streamlit.py", remote_path="/root/patch_streamlit.py", copy=True)
    .run_commands("python /root/patch_streamlit.py")
    .add_local_dir("app", remote_path="/root/app")
    .add_local_dir("webui", remote_path="/root/webui")
    .add_local_dir("resource", remote_path="/root/resource")
    .add_local_dir(".streamlit", remote_path="/root/.streamlit")
    .add_local_file("config.toml", remote_path="/root/config.toml")
)

app = modal.App("bangai-stock-studio")

# Streamlit Hosted Web App on Modal Cloud
@app.function(
    image=image,
    volumes={"/root/storage": volume},
    scaledown_window=60,
    timeout=1200,
    cpu=2,
    memory=4096
)
@modal.concurrent(max_inputs=100)
@modal.web_server(8501, startup_timeout=60.0)
def ui():
    import os
    import subprocess
    import toml

    try:
        volume.reload()
        # Sanitize persistent cloud storage base template:
        # All BYOK keys (Gemini, Pexels, OpenAI, json2video, etc.) must remain clean/empty so new users start with their own keys.
        storage_cfg = "/root/storage/config.toml"
        legacy_keys = {
            "5RBJDXZfAjfT1CSJ6F18DlZ7hvACfw7hfCtEdC1p",
            "qGkUqZ4rFf14aQc2qGcl12b8z",
            "AIzaSyC_ozuedo6ueobvhbHDA6OFYa-d4uKDAKo",
            "wnzipdxV7TGWJQwatBQeOzMRL7LnYHbAJS09rRxwMpvuv89OSrs8B6Um"
        }
        if os.path.exists(storage_cfg):
            with open(storage_cfg, "r", encoding="utf-8") as f:
                c = toml.load(f)
            app_sec = c.setdefault("app", {})
            app_sec["gemini_api_key"] = ""
            app_sec["pexels_api_keys"] = []
            for k in ["openai_api_key", "anthropic_api_key", "azure_api_key", "deepseek_api_key"]:
                if k in app_sec:
                    app_sec[k] = ""
            # json2video is strictly BYOK
            j2v_sec = c.setdefault("json2video", {})
            if j2v_sec.get("api_key") in legacy_keys:
                j2v_sec["api_key"] = ""
            with open(storage_cfg, "w", encoding="utf-8") as f:
                toml.dump(c, f)

        # Also sanitize existing user configs: clear any legacy platform keys
        import glob
        for user_cfg in glob.glob("/root/storage/users/*/config.toml"):
            try:
                with open(user_cfg, "r", encoding="utf-8") as f:
                    uc = toml.load(f)
                dirty = False
                j2v_sec = uc.setdefault("json2video", {})
                if j2v_sec.get("api_key") in legacy_keys:
                    j2v_sec["api_key"] = ""
                    dirty = True
                if dirty:
                    with open(user_cfg, "w", encoding="utf-8") as f:
                        toml.dump(uc, f)
            except Exception:
                pass

        volume.commit()
    except Exception as e:
        print(f"[modal_app] Volume setup note: {e}")

    cmd = (
        "streamlit run webui/Main.py "
        "--server.port 8501 "
        "--server.address 0.0.0.0 "
        "--server.headless true "
        "--browser.gatherUsageStats false "
        "--server.showEmailPrompt false "
        "--server.enableCORS false "
        "--server.enableXsrfProtection false "
        "--server.fileWatcherType none"
    )
    env = {
        **os.environ,
        "PYTHONPATH": "/root",
    }
    subprocess.Popen(cmd, shell=True, cwd="/root", env=env)


# Dedicated background rendering worker (20 concurrent cloud rendering slots)
@app.function(
    image=image,
    volumes={"/root/storage": volume},
    timeout=1200,
    cpu=4,
    memory=8192,
    max_containers=20
)
def render_task_worker(task_id: str, params_dict: dict, stop_at: str = "video"):
    sys.path.insert(0, "/root")
    os.chdir("/root")
    volume.reload()
    
    from app.utils import utils
    from app.config import config
    from app.models.schema import VideoParams
    from app.services import task as tm
    
    user_id = str(params_dict.get("user_id") or "default_user").strip()
    utils.set_current_user_id(user_id)
    config.switch_user_config(user_id)
    
    # Propagate task-level Bang API Credits flags to runtime config
    if params_dict.get("use_bangai_credits") or params_dict.get("bang_api_credits"):
        config.bangai_credits["use_bang_llm_credits"] = True
        config.bangai_credits["use_bang_j2v_credits"] = True
        config.bangai_credits["use_bang_voice_credits"] = True
        config.bangai_credits["use_bang_material_credits"] = True
    
    task_dir = utils.task_dir(task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    # Persist input settings and parameters for task auditing & Admin Panel inspection
    params_file = f"{task_dir}/params.json"
    with open(params_file, "w", encoding="utf-8") as pf:
        json.dump(params_dict, pf, default=str)
    
    state_file = f"{task_dir}/state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"state": "processing", "progress": 10, "task_id": task_id, "step": "Starting generation..."}, f)
    volume.commit()

    # 1. Attach live logger sink so WebUI streams terminal logs below progress bar in real-time
    log_file = os.path.join(task_dir, "task.log")
    log_handler_id = None
    try:
        from app.services.webui_task import format_log_record
        log_handler_id = logger.add(
            log_file,
            level="DEBUG",
            format=format_log_record,
            colorize=False,
            enqueue=True
        )
    except Exception as le:
        logger.debug(f"Modal Worker logger sink notice: {le}")

    # 2. Synchronize fresh system keys & models from KeyRotator for active Bang API Credits
    try:
        from app.services.key_rotator import key_rotator
        key_rotator.reload_keys(force_remote=True)
        key_rotator.sync_to_runtime_config()
    except Exception as ke:
        logger.debug(f"Modal Worker key_rotator notice: {ke}")
    
    try:
        params = VideoParams(**params_dict)
        logger.info(f"Modal Worker: Processing task {task_id}")
        pipeline_res = tm._run_pipeline(task_id=task_id, params=params, stop_at=stop_at)
        
        final_video = f"{task_dir}/final-1.mp4"
        if os.path.exists(final_video):
            logger.success(f"Modal Worker: Task {task_id} completed successfully")
            final_dl_url = f"https://cmpunktg--bangai-stock-studio-serve.modal.run/api/bangai/download/{task_id}/final-1.mp4"
            
            existing_st = {}
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as sf:
                        existing_st = json.load(sf)
                except Exception:
                    pass

            task_state_payload = {
                **existing_st,
                "state": 1,
                "status": "complete",
                "progress": 100,
                "task_id": task_id,
                "videos": [final_video],
                "video_url": f"/api/bangai/download/{task_id}/final-1.mp4",
                "video_subject": params_dict.get("video_subject", "")
            }
            if isinstance(pipeline_res, dict) and pipeline_res.get("youtube_url"):
                task_state_payload["youtube_url"] = pipeline_res["youtube_url"]
                task_state_payload["youtube_channel_title"] = pipeline_res.get("youtube_channel_title", "")
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(task_state_payload, f, default=str)
            volume.commit()
            
            # Autopilot: Automatic YouTube Upload if enabled
            if params_dict.get("auto_upload_youtube") and params_dict.get("user_youtube_access_token"):
                try:
                    import requests
                    import re
                    yt_webhook = params_dict.get("youtube_webhook_url") or "https://cmpunktg29.app.n8n.cloud/webhook/viral-shorts-ai-youtube-upload"
                    subj = params_dict.get("video_subject") or "Viral Stock Studio Short"
                    logger.info(f"Modal Worker: Auto-uploading video {task_id} ('{subj}') to YouTube...")
                    yt_payload = {
                        "videoUrl": final_dl_url,
                        "title": subj[:95],
                        "description": f"{subj}\n\nRendered automatically with BangAI Stock Studio Autopilot.\n#shorts #viral",
                        "tags": ["shorts", "viral", "ai", "stockstudio"],
                        "googleAccessToken": params_dict.get("user_youtube_access_token"),
                        "userYouTubeAccessToken": params_dict.get("user_youtube_access_token"),
                        "channelId": params_dict.get("user_youtube_channel_id", ""),
                        "channelTitle": params_dict.get("user_youtube_channel_title", ""),
                        "userYouTubeChannelTitle": params_dict.get("user_youtube_channel_title", ""),
                        "userYouTubeChannelId": params_dict.get("user_youtube_channel_id", ""),
                        "privacyStatus": params_dict.get("youtube_privacy", "public"),
                        "webhookSecret": "s-vshorts-sec-9a8b7c6d5e4f3a2b1c0"
                    }
                    yt_res = requests.post(yt_webhook, json=yt_payload, timeout=45)
                    logger.info(f"Modal Worker: YouTube auto-upload status for {task_id}: {yt_res.status_code}")
                    
                    yt_video_url = ""
                    try:
                        y_data = yt_res.json()
                        if isinstance(y_data, list) and len(y_data) > 0:
                            y_data = y_data[0]
                        if isinstance(y_data, dict):
                            if "json" in y_data and isinstance(y_data["json"], dict):
                                y_data = y_data["json"]
                            vid_id = y_data.get("videoId") or y_data.get("id")
                            if not vid_id and isinstance(y_data.get("snippet"), dict):
                                vid_id = y_data.get("snippet", {}).get("resourceId", {}).get("videoId")
                            yt_video_url = y_data.get("videoUrl") or y_data.get("url") or (f"https://youtube.com/shorts/{vid_id}" if vid_id else "")
                    except Exception as parse_err:
                        logger.warning(f"Failed to parse n8n response: {parse_err}")

                    if not yt_video_url and yt_res.text:
                        yt_match = re.search(r'https?://[^\s"\'<>]+(?:youtu\.be|youtube\.com)[^\s"\'<>]*', yt_res.text)
                        if yt_match:
                            yt_video_url = yt_match.group(0)
                        else:
                            id_match = re.search(r'["\'](?:videoId|id)["\']\s*:\s*["\']([a-zA-Z0-9_-]{11})["\']', yt_res.text)
                            if id_match:
                                yt_video_url = f"https://youtube.com/shorts/{id_match.group(1)}"

                    # Reliable fallback: query YouTube channel directly to get the uploaded short URL
                    if not yt_video_url and params_dict.get("user_youtube_access_token"):
                        try:
                            ch_id = params_dict.get("user_youtube_channel_id", "")
                            yt_api_url = "https://www.googleapis.com/youtube/v3/search?part=snippet&order=date&type=video&maxResults=3"
                            if ch_id:
                                yt_api_url += f"&channelId={ch_id}"
                            q_res = requests.get(yt_api_url, headers={"Authorization": f"Bearer {params_dict.get('user_youtube_access_token')}"}, timeout=10)
                            if q_res.status_code == 200:
                                items = q_res.json().get("items", [])
                                for item in items:
                                    v_title = item.get("snippet", {}).get("title", "")
                                    v_id = item.get("id", {}).get("videoId")
                                    if v_id and (v_title.lower() in subj.lower() or subj.lower() in v_title.lower()):
                                        yt_video_url = f"https://youtube.com/shorts/{v_id}"
                                        break
                                if not yt_video_url and items and items[0].get("id", {}).get("videoId"):
                                    yt_video_url = f"https://youtube.com/shorts/{items[0]['id']['videoId']}"
                        except Exception as q_err:
                            logger.warning(f"YouTube search fallback error: {q_err}")

                    task_state_payload["youtube_url"] = yt_video_url
                    task_state_payload["youtube_channel_title"] = params_dict.get("user_youtube_channel_title", "")
                    task_state_payload["youtube_channel_id"] = params_dict.get("user_youtube_channel_id", "")
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(task_state_payload, f, default=str)
                    volume.commit()
                except Exception as ye:
                    logger.warning(f"Modal Worker: YouTube auto-upload warning for {task_id}: {ye}")
        else:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"state": -1, "status": "failed", "progress": 0, "task_id": task_id, "error": "Render completed but final video file not found"}, f)
            volume.commit()
    except Exception as e:
        import traceback
        err_str = str(e)
        logger.exception(f"Modal Worker Error on {task_id}: {err_str}")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"state": "failed", "progress": 0, "task_id": task_id, "error": err_str, "trace": traceback.format_exc()}, f)
    finally:
        if log_handler_id is not None:
            try:
                logger.remove(log_handler_id)
            except Exception:
                pass
        volume.commit()


# Cloud-Native 24-Hour Autonomous Stock Studio Autopilot Scheduler (runs every 15 mins)
@app.function(
    image=image,
    schedule=modal.Cron("*/15 * * * *"),
    timeout=180
)
def stock_studio_autopilot_cron():
    """
    Autonomous 24-Hour Stock Studio Autopilot Scheduler:
    Runs automatically every 15 minutes in Modal cloud, queries BangAI to process any due 24h video automations.
    Dispatches due topics to Modal GPU cluster and auto-publishes to YouTube.
    """
    import requests
    try:
        res = requests.post(
            "https://bangai.netlify.app/.netlify/functions/stock-studio-automation",
            json={"action": "process-due"},
            headers={"Content-Type": "application/json"},
            timeout=120
        )
        logger.info(f"Modal Autopilot Cron: Checked due automations (HTTP {res.status_code})")
    except Exception as e:
        logger.warning(f"Modal Autopilot Cron notice: {e}")


@app.function(
    image=image,
    volumes={"/root/storage": volume},
    scaledown_window=60,
    timeout=600,
    cpu=2,
    memory=4096
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def serve():
    sys.path.insert(0, "/root")
    os.chdir("/root")
    os.environ["CORS_ALLOWED_ORIGINS"] = "*"
    
    from app.asgi import app as fastapi_app
    
    bangai_router = APIRouter(prefix="/api/bangai")

    @bangai_router.post("/system-keys")
    async def bangai_save_system_keys(request: Request):
        try:
            body = await request.json()
            keys_data = body.get("keys", body)
            if not isinstance(keys_data, dict):
                keys_data = {}
            if "models" in body and isinstance(body["models"], dict):
                keys_data["models"] = body["models"]
            if "default_provider" in body:
                keys_data["default_provider"] = body["default_provider"]
            storage_path = "/root/storage/system_api_keys.json"
            os.makedirs("/root/storage", exist_ok=True)
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(keys_data, f, indent=2)
            volume.commit()
            try:
                from app.services.key_rotator import key_rotator
                key_rotator.reload_keys(force_remote=True)
            except Exception as re:
                logger.debug(f"key_rotator reload note: {re}")
            logger.success(f"Modal Cluster: Successfully synchronized system API keys ({len(keys_data)} pools & models)")
            return {"status": 200, "message": "System keys and models synchronized successfully", "pools": list(keys_data.keys())}
        except Exception as e:
            logger.error(f"Failed to synchronize system keys: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @bangai_router.get("/system-keys")
    async def bangai_get_system_keys():
        volume.reload()
        storage_path = "/root/storage/system_api_keys.json"
        if os.path.exists(storage_path):
            try:
                with open(storage_path, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                    models_data = file_data.pop("models", {}) if isinstance(file_data, dict) else {}
                    default_prov = file_data.pop("default_provider", "gemini") if isinstance(file_data, dict) else "gemini"
                    return {"status": 200, "data": file_data, "models": models_data, "default_provider": default_prov}
            except Exception:
                pass
        return {"status": 200, "data": {}, "models": {}}
    
    @bangai_router.post("/generate")
    async def bangai_generate(request: Request):
        body = await request.json()
        task_id = str(body.get("task_id") or uuid4()).strip()
        stop_at = str(body.get("stop_at") or "video").strip()
        
        render_task_worker.spawn(task_id, body, stop_at)
        
        return {
            "status": 200,
            "message": "success",
            "data": {
                "task_id": task_id
            }
        }

    @bangai_router.get("/tasks")
    async def bangai_get_all_tasks(request: Request, limit: int = 60):
        import glob
        import time
        volume.reload()
        base_url = str(request.base_url).rstrip('/')
        
        user_task_dirs = glob.glob("/root/storage/users/*/tasks/*")
        global_task_dirs = glob.glob("/root/storage/tasks/*")
        all_dirs = list(set(user_task_dirs + global_task_dirs))
        
        tasks_list = []
        for t_dir in all_dirs:
            if not os.path.isdir(t_dir):
                continue
            task_id = os.path.basename(t_dir)
            if task_id.startswith(".") or task_id.startswith("_"):
                continue

            try:
                mtime = os.path.getmtime(t_dir) if os.path.exists(t_dir) else time.time()
                now_ts = time.time()

                # Load params.json
                params_file = os.path.join(t_dir, "params.json")
                params = {}
                if os.path.exists(params_file):
                    try:
                        with open(params_file, "r", encoding="utf-8") as pf:
                            params = json.load(pf)
                    except Exception:
                        pass

                # Load script.json fallback
                script_file = os.path.join(t_dir, "script.json")
                script_data = {}
                if os.path.exists(script_file):
                    try:
                        with open(script_file, "r", encoding="utf-8") as scf:
                            script_data = json.load(scf)
                    except Exception:
                        pass
                if not params and isinstance(script_data, dict) and "params" in script_data:
                    params = script_data.get("params", {})

                # Load state.json
                state_file = os.path.join(t_dir, "state.json")
                state_data = {}
                if os.path.exists(state_file):
                    try:
                        with open(state_file, "r", encoding="utf-8") as sf:
                            state_data = json.load(sf)
                    except Exception:
                        pass

                # Check for final video
                final_mp4 = os.path.join(t_dir, "final-1.mp4")
                has_video = os.path.exists(final_mp4) and os.path.getsize(final_mp4) > 1024
                video_url = f"{base_url}/api/bangai/download/{task_id}/final-1.mp4" if has_video else None

                # Resolve user_id
                user_id = params.get("user_id") or state_data.get("user_id") or ""
                if not user_id:
                    path_parts = t_dir.replace("\\", "/").split("/")
                    if "users" in path_parts:
                        u_idx = path_parts.index("users")
                        if u_idx + 1 < len(path_parts):
                            user_id = path_parts[u_idx + 1]
                if not user_id:
                    user_id = "default_user"

                # Determine state with Stale Task Watchdog
                raw_state = state_data.get("state")
                raw_updated = state_data.get("updated_at")
                task_updated = mtime
                if raw_updated:
                    try:
                        task_updated = float(raw_updated)
                    except (ValueError, TypeError):
                        task_updated = mtime
                task_age = now_ts - task_updated

                # Watchdog: mark tasks inactive for over 20 mins without video as failed
                is_stale_or_failed = (raw_state in (-1, "failed")) or (not has_video and task_age > 1200)

                if has_video:
                    status_str = "completed"
                    progress = 100
                elif raw_state in (1, "complete"):
                    status_str = "completed"
                    progress = 100
                elif is_stale_or_failed:
                    status_str = "failed"
                    progress = 0
                else:
                    status_str = "rendering"
                    progress = int(state_data.get("progress", 10) or 10)

                # Credit Mode detection:
                # If user has toggled Bang API Credits or no BYOK custom keys are present
                has_custom_keys = bool(params.get("gemini_api_key") or params.get("openai_api_key") or params.get("pexels_api_keys") or params.get("elevenlabs_api_key"))
                is_bang_credits = bool(params.get("use_bangai_credits") or params.get("bang_api_credits") or params.get("use_bang_material_credits") or not has_custom_keys)

                created_ts = state_data.get("updated_at") or mtime
                if isinstance(created_ts, (int, float)) and created_ts < 2000000000:
                    created_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(created_ts))
                else:
                    created_iso = str(created_ts)

                subject = params.get("video_subject") or script_data.get("script", "")[:45] or state_data.get("video_subject") or f"Stock Studio Video #{task_id[:8]}"

                yt_url = state_data.get("youtube_url") or params.get("youtube_url") or ""
                yt_ch_title = state_data.get("youtube_channel_title") or params.get("user_youtube_channel_title") or ""
                yt_ch_id = state_data.get("youtube_channel_id") or params.get("user_youtube_channel_id") or ""

                tasks_list.append({
                    "id": task_id,
                    "task_id": task_id,
                    "title": subject,
                    "userId": user_id,
                    "status": status_str,
                    "progress": progress,
                    "currentStep": state_data.get("step") or state_data.get("failed_stage") or ("Render Complete" if has_video else "Processing Pipeline..."),
                    "error": state_data.get("error", ""),
                    "failedStage": state_data.get("failed_stage", ""),
                    "videoUrl": video_url,
                    "videos": [video_url] if video_url else [],
                    "youtube_url": yt_url,
                    "youtubeUrl": yt_url,
                    "youtube_channel_title": yt_ch_title,
                    "youtube_channel_id": yt_ch_id,
                    "createdAt": created_iso,
                    "mtime": mtime,
                    "creditMode": "bang_credits" if is_bang_credits else "byok",
                    "settings": {
                        "video_subject": subject,
                        "video_language": params.get("video_language") or state_data.get("video_language") or "hi-IN",
                        "video_aspect": params.get("video_aspect", "9:16"),
                        "video_source": params.get("video_source", "pexels"),
                        "voice_provider": params.get("voice_provider", "ElevenLabs / json2video"),
                        "voice_name": params.get("voice_name", "Adam"),
                        "voice_speed": params.get("voice_speed", 1.1),
                        "script_engine": params.get("llm_provider", "gemini"),
                        "script_model": params.get("llm_model", "gemini-2.5-flash"),
                        "match_materials_to_script": bool(params.get("match_materials_to_script", True)),
                        "auto_timeline_sync": bool(params.get("auto_timeline_sync", True)),
                        "paragraph_number": params.get("paragraph_number", 2),
                        "subtitle_preset": params.get("subtitle_preset", "mrbeast-viral"),
                        "font_size": params.get("font_size", 60),
                        "bgm_volume": params.get("bgm_volume", 0.08),
                        "duration": state_data.get("duration") or 60
                    }
                })
            except Exception as task_err:
                logger.error(f"Error parsing task {task_id}: {task_err}")
                continue

        try:
            volume.commit()
        except Exception:
            pass

        tasks_list.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        return {"status": 200, "data": {"tasks": tasks_list[:limit], "total": len(tasks_list)}}
        
    @bangai_router.get("/task/{task_id}")
    async def bangai_task_status(task_id: str, request: Request):
        import glob
        volume.reload()
        base_url = str(request.base_url).rstrip('/')
        task_dir = f"/root/storage/tasks/{task_id}"
        if not os.path.exists(task_dir):
            matches = glob.glob(f"/root/storage/users/*/tasks/{task_id}")
            if matches:
                task_dir = matches[0]
                
        state_file = f"{task_dir}/state.json"
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                    
                    # Watchdog check for stale tasks
                    raw_st = state_data.get("state")
                    raw_up = state_data.get("updated_at")
                    task_updated = os.path.getmtime(state_file) if os.path.exists(state_file) else time.time()
                    if raw_up:
                        try:
                            task_updated = float(raw_up)
                        except (ValueError, TypeError):
                            pass
                    if raw_st not in (-1, "failed", 1, "complete") and (time.time() - task_updated > 1200):
                        state_data["state"] = -1
                        state_data["status"] = "failed"
                        state_data["error"] = state_data.get("error") or "Task timed out: worker container terminated or stalled"
                        state_data["failed_stage"] = state_data.get("failed_stage") or "timeout"
                        try:
                            with open(state_file, "w", encoding="utf-8") as sf:
                                json.dump(state_data, sf, default=str)
                        except Exception:
                            pass

                    if state_data.get("video_url"):
                        vurl = state_data["video_url"]
                        if vurl.startswith("/"):
                            state_data["video_url"] = f"{base_url}{vurl}"
                        elif ".modal.run" in vurl:
                            path_part = vurl.split(".modal.run", 1)[-1]
                            state_data["video_url"] = f"{base_url}{path_part}"
                    return {"status": 200, "data": state_data}
            except Exception:
                pass
                
        final_mp4 = f"{task_dir}/final-1.mp4"
        combined_mp4 = f"{task_dir}/combined-1.mp4"
        subtitle_srt = f"{task_dir}/subtitle.srt"
        audio_mp3 = f"{task_dir}/audio.mp3"
        script_json = f"{task_dir}/script.json"
        
        has_temp = False
        if os.path.exists(task_dir):
            try:
                has_temp = any("temp" in f.lower() or f.endswith(".tmp") for f in os.listdir(task_dir))
            except Exception:
                pass

        if os.path.exists(final_mp4) and not has_temp and os.path.getsize(final_mp4) > 1024:
            return {
                "status": 200,
                "data": {
                    "state": "complete",
                    "progress": 100,
                    "task_id": task_id,
                    "video_url": f"{base_url}/api/bangai/download/{task_id}/final-1.mp4"
                }
            }
        elif os.path.exists(combined_mp4):
            return {"status": 200, "data": {"state": "processing", "progress": 85, "task_id": task_id, "step": "Rendering final composition..."}}
        elif os.path.exists(subtitle_srt):
            return {"status": 200, "data": {"state": "processing", "progress": 65, "task_id": task_id, "step": "Downloading stock footage & subtitles..."}}
        elif os.path.exists(audio_mp3):
            return {"status": 200, "data": {"state": "processing", "progress": 40, "task_id": task_id, "step": "Synthesizing neural voice..."}}
        elif os.path.exists(script_json) or os.path.exists(task_dir):
            return {"status": 200, "data": {"state": "processing", "progress": 20, "task_id": task_id, "step": "Generating AI script..."}}
        else:
            return {"status": 200, "data": {"state": "processing", "progress": 10, "task_id": task_id, "step": "Task queued..."}}

    @bangai_router.post("/clear-cache")
    async def bangai_clear_cache():
        import glob
        import shutil
        import time
        volume.reload()
        
        purged_failed = 0
        deleted_cache_files = 0
        reclaimed_bytes = 0
        
        # 1. Clean video material caches
        cache_dir = "/root/storage/cache_videos"
        if os.path.exists(cache_dir):
            for f in os.listdir(cache_dir):
                fp = os.path.join(cache_dir, f)
                try:
                    if os.path.isfile(fp):
                        reclaimed_bytes += os.path.getsize(fp)
                        os.remove(fp)
                        deleted_cache_files += 1
                except Exception:
                    pass

        # 2. Clean temporary directories
        temp_dir = "/root/storage/temp"
        if os.path.exists(temp_dir):
            for f in os.listdir(temp_dir):
                fp = os.path.join(temp_dir, f)
                try:
                    if os.path.isfile(fp):
                        reclaimed_bytes += os.path.getsize(fp)
                        os.remove(fp)
                    elif os.path.isdir(fp):
                        shutil.rmtree(fp, ignore_errors=True)
                except Exception:
                    pass

        # 3. Purge all failed or stalled task directories completely
        user_task_dirs = glob.glob("/root/storage/users/*/tasks/*")
        global_task_dirs = glob.glob("/root/storage/tasks/*")
        all_dirs = list(set(user_task_dirs + global_task_dirs))
        
        now_ts = time.time()
        for t_dir in all_dirs:
            if not os.path.isdir(t_dir):
                continue
            has_video = os.path.exists(os.path.join(t_dir, "final-1.mp4"))
            state_file = os.path.join(t_dir, "state.json")
            is_failed = False
            
            if not has_video:
                if os.path.exists(state_file):
                    try:
                        with open(state_file, "r", encoding="utf-8") as sf:
                            st = json.load(sf)
                            if st.get("state") in (-1, "failed"):
                                is_failed = True
                    except Exception:
                        is_failed = True
                else:
                    is_failed = True
                
                try:
                    mtime = os.path.getmtime(t_dir)
                    if (now_ts - mtime) > 1200:
                        is_failed = True
                except Exception:
                    pass
            
            if is_failed and not has_video:
                try:
                    shutil.rmtree(t_dir, ignore_errors=True)
                    purged_failed += 1
                except Exception:
                    pass
                    
        try:
            volume.commit()
        except Exception:
            pass
            
        return {
            "status": 200,
            "message": "Caches cleared and failed tasks purged successfully",
            "data": {
                "deleted_cache_files": deleted_cache_files,
                "purged_failed_tasks": purged_failed,
                "reclaimed_mb": round(reclaimed_bytes / (1024 * 1024), 2)
            }
        }

    @bangai_router.api_route("/download/{task_id}/{filename}", methods=["GET", "HEAD"])
    async def bangai_download(task_id: str, filename: str):
        import glob
        volume.reload()
        file_path = f"/root/storage/tasks/{task_id}/{filename}"
        if not os.path.exists(file_path):
            matches = glob.glob(f"/root/storage/users/*/tasks/{task_id}/{filename}")
            if matches and os.path.exists(matches[0]):
                file_path = matches[0]
            else:
                raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(
            file_path,
            media_type="video/mp4",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes"
            }
        )

    fastapi_app.include_router(bangai_router)
    bangai_routes = [r for r in fastapi_app.router.routes if getattr(r, "path", "").startswith("/api/bangai")]
    for r in bangai_routes:
        fastapi_app.router.routes.remove(r)
    for r in reversed(bangai_routes):
        fastapi_app.router.routes.insert(0, r)

    return fastapi_app
