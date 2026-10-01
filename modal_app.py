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
        "socksio==1.0.0"
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
        # Sanitize persistent cloud storage base template to guarantee zero hardcoded API keys
        storage_cfg = "/root/storage/config.toml"
        if os.path.exists(storage_cfg):
            with open(storage_cfg, "r", encoding="utf-8") as f:
                c = toml.load(f)
            app_sec = c.setdefault("app", {})
            app_sec["gemini_api_key"] = ""
            app_sec["pexels_api_keys"] = []
            for k in ["openai_api_key", "anthropic_api_key", "azure_api_key", "deepseek_api_key"]:
                if k in app_sec:
                    app_sec[k] = ""
            # Zero pre-applied keys: strictly 100% Bring Your Own Key (BYOK)
            j2v_sec = c.setdefault("json2video", {})
            j2v_sec["api_key"] = ""
            with open(storage_cfg, "w", encoding="utf-8") as f:
                toml.dump(c, f)

        # Also purge any legacy pre-applied keys from existing user configs on the volume
        import glob
        for user_cfg in glob.glob("/root/storage/users/*/config.toml"):
            try:
                with open(user_cfg, "r", encoding="utf-8") as f:
                    uc = toml.load(f)
                dirty = False
                j2v_k = uc.get("json2video", {}).get("api_key", "")
                if j2v_k and j2v_k.startswith("CclCGmg"):
                    uc["json2video"]["api_key"] = ""
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
    
    from app.models.schema import VideoParams
    from app.services import task as tm
    
    task_dir = f"/root/storage/tasks/{task_id}"
    os.makedirs(task_dir, exist_ok=True)
    
    state_file = f"{task_dir}/state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"state": "processing", "progress": 10, "task_id": task_id, "step": "Starting generation..."}, f)
    volume.commit()
    
    try:
        params = VideoParams(**params_dict)
        logger.info(f"Modal Worker: Processing task {task_id}")
        tm._run_pipeline(task_id=task_id, params=params, stop_at=stop_at)
        
        final_video = f"{task_dir}/final-1.mp4"
        if os.path.exists(final_video):
            logger.success(f"Modal Worker: Task {task_id} completed successfully")
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "state": "complete",
                    "progress": 100,
                    "task_id": task_id,
                    "video_url": f"/api/bangai/download/{task_id}/final-1.mp4"
                }, f)
        else:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"state": "failed", "progress": 0, "task_id": task_id, "error": "Render completed but final video file not found"}, f)
    except Exception as e:
        import traceback
        err_str = str(e)
        logger.exception(f"Modal Worker Error on {task_id}: {err_str}")
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"state": "failed", "progress": 0, "task_id": task_id, "error": err_str, "trace": traceback.format_exc()}, f)
    finally:
        volume.commit()


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
    
    @bangai_router.post("/generate")
    async def bangai_generate(request: Request):
        body = await request.json()
        task_id = str(uuid4())
        
        render_task_worker.spawn(task_id, body, "video")
        
        return {
            "status": 200,
            "message": "success",
            "data": {
                "task_id": task_id
            }
        }
        
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
