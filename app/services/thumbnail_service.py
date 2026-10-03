"""
BangAI Thumbnail Studio Service.
Enterprise-grade AI thumbnail generation and editing engine with autonomous
multi-account API key rotation, cloud image hosting, and viral prompt enhancement.
Strictly whitelisted: all vendor references are encapsulated internally.
"""

import base64
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────
# 1. Autonomous Multi-Key Pool & Rotation Manager
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_API_KEYS = [
    "600b59d0630c57062ea2d359a6a6f64c",
    "448eb7672ebe6a1c6fe64c4db9e7edf0",
    "4125c81cab785b8b23474ce0b62c10ef",
    "dea4cecf44a3aec3b6a652bd485e172d",
    "92d0de6f5552d358907b96d10349e68b",
]

_API_BASE_URL = "https://api.kie.ai"
_CDN_UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"


class KeyPoolManager:
    """Thread-safe multi-account API key rotation and health monitor."""

    def __init__(self, keys: Optional[List[str]] = None):
        self._keys = [k.strip() for k in (keys or DEFAULT_API_KEYS) if k and k.strip()]
        self._current_index = 0
        self._lock = threading.Lock()
        self._exhausted_keys: Dict[str, float] = {}  # key -> timestamp when exhausted
        self._key_stats: Dict[str, Dict[str, Any]] = {
            k: {"success": 0, "failures": 0, "last_used": 0.0} for k in self._keys
        }

    def get_active_key(self) -> str:
        """Returns the next healthy active key in rotation."""
        with self._lock:
            if not self._keys:
                raise RuntimeError("No API keys configured in KeyPoolManager.")

            now = time.time()
            # If all keys were marked exhausted over 1 hour ago, give them a chance to re-test
            if len(self._exhausted_keys) >= len(self._keys):
                oldest_exhausted = min(self._exhausted_keys.values()) if self._exhausted_keys else 0
                if now - oldest_exhausted > 3600:
                    logger.info("Resetting exhausted key pool after 1 hour cooldown.")
                    self._exhausted_keys.clear()

            # Find next non-exhausted key
            for _ in range(len(self._keys)):
                idx = self._current_index % len(self._keys)
                candidate = self._keys[idx]
                self._current_index += 1
                if candidate not in self._exhausted_keys:
                    self._key_stats[candidate]["last_used"] = now
                    return candidate

            # If all are exhausted, fallback to first key with a warning
            fallback = self._keys[0]
            logger.warning("All key accounts currently flagged as exhausted. Attempting primary key.")
            return fallback

    def mark_exhausted(self, key: str, reason: str = "Credits exhausted"):
        """Marks a key as exhausted and logs the event."""
        with self._lock:
            self._exhausted_keys[key] = time.time()
            if key in self._key_stats:
                self._key_stats[key]["failures"] += 1
            key_preview = f"{key[:6]}...{key[-4:]}" if len(key) >= 10 else "key"
            logger.warning(
                f"[KeyPool] Key {key_preview} flagged as exhausted ({reason}). "
                f"Active keys remaining: {len(self._keys) - len(self._exhausted_keys)}/{len(self._keys)}"
            )

    def record_success(self, key: str):
        """Records a successful job generation for telemetry."""
        with self._lock:
            if key in self._exhausted_keys:
                del self._exhausted_keys[key]
            if key in self._key_stats:
                self._key_stats[key]["success"] += 1

    def get_pool_status(self) -> List[Dict[str, Any]]:
        """Returns health overview for diagnostics without exposing full secrets."""
        with self._lock:
            statuses = []
            for i, k in enumerate(self._keys):
                is_ex = k in self._exhausted_keys
                statuses.append(
                    {
                        "index": i + 1,
                        "masked_key": f"{k[:4]}••••{k[-4:]}",
                        "status": "Exhausted" if is_ex else "Healthy",
                        "success_count": self._key_stats.get(k, {}).get("success", 0),
                        "failure_count": self._key_stats.get(k, {}).get("failures", 0),
                    }
                )
            return statuses


# Global singleton instance
key_manager = KeyPoolManager()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Top-Tier Model Matrix & Capability Specifications
# ─────────────────────────────────────────────────────────────────────────────

MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    "grok-imagine-image-2-0/text-to-image": {
        "label": "Grok Imagine 2.0 (Ultra High-Impact)",
        "mode": "text-to-image",
        "credits": 4,
        "supported_aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        "supported_resolutions": ["Native 2K/4K"],
        "supports_background": False,
        "input_field": "prompt",
        "description": "State-of-the-art cinematic image synthesis with immense dramatic contrast and 3D typography rendering.",
        "example_prompt": "A dramatic cinematic YouTube thumbnail of an AI robot revealing the secrets of the universe, hyperrealistic 8k, extreme contrast, 16:9",
        "showcase_local": "resource/showcases/grok_t2i.jpg",
        "showcase_url": "https://tempfile.aiquickdraw.com/r/80c3a220-2531-4b78-9cd8-913a1b0935fc.jpg",
    },
    "gpt-image-2-5-flare-text-to-image": {
        "label": "GPT Image 2.5 Flare (Pro Viral Master)",
        "mode": "text-to-image",
        "credits": 6,
        "supported_aspect_ratios": ["16:9", "9:16", "1:1", "21:9", "4:3"],
        "supported_resolutions": ["1K", "2K", "4K"],
        "supports_background": True,
        "input_field": "prompt",
        "description": "Specialized in expressive facial reactions, high saturation, and viral MrBeast-grade YouTube thumbnails.",
        "example_prompt": "A viral YouTube thumbnail of a shocked young creator pointing at a glowing bag with 1,000,000 dollars cash, vibrant colors, MrBeast style thumbnail, 16:9",
        "showcase_local": "resource/showcases/gpt_flare_t2i.png",
        "showcase_url": "https://tempfile.aiquickdraw.com/images/chatgpt/file_000000004dcc81f68698b1da7d1a4246.png",
    },
    "flux1-kontext": {
        "label": "Flux 1 Kontext (Photoreal Documentary)",
        "mode": "text-to-image",
        "credits": 4,
        "supported_aspect_ratios": ["16:9", "9:16", "1:1", "4:3"],
        "supported_resolutions": ["1K", "2K"],
        "supports_background": False,
        "input_field": "prompt",
        "description": "Exceptional photorealism, authentic skin textures, and cinematic documentary storytelling aesthetics.",
        "example_prompt": "A cinematic ultra-realistic YouTube thumbnail of an abandoned underwater sunken city discovered by deep sea explorers, glowing ancient bioluminescent ruins, 8k, dramatic lighting, 16:9",
        "showcase_local": "resource/showcases/flux_t2i.jpg",
        "showcase_url": "https://tempfile.aiquickdraw.com/js/g4/91ae31828600.jpg",
    },
    "grok-imagine-image-2-0/image-edit": {
        "label": "Grok Imagine Edit 2.0 (Scene & Lighting Redo)",
        "mode": "image-edit",
        "credits": 4,
        "supported_aspect_ratios": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        "supported_resolutions": ["Native 2K/4K"],
        "supports_background": False,
        "image_key": "image_urls",
        "description": "Seamlessly restyle environments, add dramatic lighting, or transform real photos into viral cover scenes.",
        "example_prompt": "Add dramatic golden sunbeams streaming through the window and make the bedroom look like a luxury presidential suite in Dubai",
        "showcase_local": "resource/showcases/grok_edit.jpg",
        "showcase_url": "https://tempfile.aiquickdraw.com/ggg/3647e1e0-19b3-40a5-9f32-12cf25aec594.jpg",
    },
    "gpt-image-2-5-flare-image-to-image": {
        "label": "GPT Image 2.5 Flare Edit (Reaction & VFX Injector)",
        "mode": "image-edit",
        "credits": 6,
        "supported_aspect_ratios": ["16:9", "9:16", "1:1", "21:9", "4:3"],
        "supported_resolutions": ["1K", "2K", "4K"],
        "supports_background": True,
        "image_key": "input_urls",
        "description": "Retains original subject identity while adding eye-catching visual effects, laser eyes, cash piles, or glowing auras.",
        "example_prompt": "Add glowing neon laser eyes and cinematic YouTube clickbait explosion in background",
        "showcase_local": "resource/showcases/gpt_flare_i2i.png",
        "showcase_url": "https://tempfile.aiquickdraw.com/images/chatgpt/file_00000000e0dc81f58cb79704ebc8a734.png",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Viral Formula Prompt Enhancer
# ─────────────────────────────────────────────────────────────────────────────

VIRAL_PRESET_TEMPLATES = {
    "mrbeast": {
        "name": "🔥 Viral High-Energy / MrBeast",
        "suffix": ", extreme wide angle, intense shocked facial expression, high contrast neon saturation, dramatic directional rim lighting, glowing focal point, ultra-crisp 8k, click-worthy YouTube thumbnail style, 16:9",
    },
    "tech": {
        "name": "💻 Tech Studio / MKBHD Minimalist",
        "suffix": ", sleek futuristic studio backdrop, dark carbon slate surfaces, sharp metallic rim lighting, subtle cyan and orange LED accents, ultra-high resolution product shot, premium aesthetic, 16:9",
    },
    "crime": {
        "name": "🕵️ True Crime / Moody Documentary",
        "suffix": ", dark moody cinematic noir lighting, atmospheric volumetric smoke and fog, harsh dramatic spotlight, gritty cinematic film texture, intense psychological suspense, 16:9",
    },
    "wealth": {
        "name": "💰 Finance & Wealth / Bold Gold",
        "suffix": ", glowing stacks of hundred dollar cash, luxurious warm gold lighting, prestigious millionaire ambiance, vivid ultra-sharp details, high contrast, viral success thumbnail, 16:9",
    },
    "gaming": {
        "name": "🎮 Esports / Cyber Action",
        "suffix": ", hyper-dynamic action perspective, vibrant neon magenta and electric blue glow, particle embers and energy sparks, high octane adrenaline composition, 8k render, 16:9",
    },
}


def enhance_viral_prompt(base_prompt: str, preset_key: str = "mrbeast") -> str:
    """Enhances a user prompt using verified YouTube high-CTR visual mechanics."""
    cleaned = base_prompt.strip()
    if not cleaned:
        return ""
    preset = VIRAL_PRESET_TEMPLATES.get(preset_key, VIRAL_PRESET_TEMPLATES["mrbeast"])
    suffix = preset["suffix"]
    # Check if aspect ratio is already present
    if "16:9" in cleaned or "9:16" in cleaned or "1:1" in cleaned:
        suffix = suffix.replace(", 16:9", "")
    return f"{cleaned}{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cloud Asset Host Adapter (Free CDN Upload)
# ─────────────────────────────────────────────────────────────────────────────

def upload_image_to_cdn(image_bytes: bytes, file_name: str = "thumbnail_ref.png") -> Tuple[bool, str]:
    """
    Uploads a local reference image to the temporary cloud CDN.
    Returns (success: bool, download_url_or_error: str).
    """
    if not image_bytes:
        return False, "Empty image bytes provided."

    b64_content = base64.b64encode(image_bytes).decode("utf-8")
    mime = "image/png"
    if file_name.lower().endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif file_name.lower().endswith(".webp"):
        mime = "image/webp"

    data_uri = f"data:{mime};base64,{b64_content}"
    payload = {
        "base64Data": data_uri,
        "fileName": file_name,
        "uploadPath": "images",
    }

    # Attempt with active key, rotating up to 3 times on failure
    for attempt in range(3):
        active_key = key_manager.get_active_key()
        headers = {
            "Authorization": f"Bearer {active_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(_CDN_UPLOAD_URL, headers=headers, json=payload, timeout=25)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("success") and body.get("data", {}).get("downloadUrl"):
                    url = body["data"]["downloadUrl"]
                    key_manager.record_success(active_key)
                    return True, url
                else:
                    msg = body.get("msg", "Unknown upload error")
                    logger.warning(f"CDN upload failed on key attempt {attempt+1}: {msg}")
                    key_manager.mark_exhausted(active_key, reason=msg)
            else:
                key_manager.mark_exhausted(active_key, reason=f"HTTP {resp.status_code}")
        except Exception as exc:
            logger.warning(f"CDN upload exception on key attempt {attempt+1}: {exc}")
            key_manager.mark_exhausted(active_key, reason=str(exc))

    return False, "Failed to upload reference image to cloud CDN after multiple key rotations."


# ─────────────────────────────────────────────────────────────────────────────
# 5. Core Generation & Restyle Task Engine
# ─────────────────────────────────────────────────────────────────────────────

def generate_thumbnail(
    model_id: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    resolution: str = "2K",
    background: str = "auto",
    reference_image_urls: Optional[List[str]] = None,
    poll_timeout_seconds: int = 140,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Submits and tracks an AI thumbnail generation task with automated key failover.
    
    Returns:
        dict with keys:
            success: bool
            result_urls: List[str]
            task_id: str
            model: str
            cost_time: int
            credits_consumed: float
            error: str
            raw_response: dict
    """
    if model_id not in MODEL_CATALOG:
        return {"success": False, "error": f"Model '{model_id}' is not in supported catalog."}

    model_meta = MODEL_CATALOG[model_id]
    mode = model_meta["mode"]

    # Construct input block
    input_payload: Dict[str, Any] = {"prompt": prompt.strip()}

    if aspect_ratio:
        input_payload["aspect_ratio"] = aspect_ratio

    if model_meta.get("supports_background"):
        if resolution in ("1K", "2K", "4K"):
            input_payload["resolution"] = resolution
        if background in ("auto", "transparent", "opaque"):
            input_payload["background"] = background

    if mode == "image-edit":
        image_key = model_meta.get("image_key", "input_urls")
        clean_urls = [u.strip() for u in (reference_image_urls or []) if u and u.strip()]
        if not clean_urls:
            return {
                "success": False,
                "error": "This model requires at least one source image URL or uploaded reference image.",
            }
        input_payload[image_key] = clean_urls

    task_payload = {"model": model_id, "input": input_payload}

    # Attempt task creation with automatic failover
    task_id = ""
    assigned_key = ""
    max_key_attempts = len(DEFAULT_API_KEYS)

    for attempt in range(max_key_attempts):
        active_key = key_manager.get_active_key()
        headers = {
            "Authorization": f"Bearer {active_key}",
            "Content-Type": "application/json",
        }
        create_url = f"{_API_BASE_URL}/api/v1/jobs/createTask"

        try:
            if progress_callback:
                progress_callback(10, f"Initializing task (Cluster Key Pool {attempt+1})...")

            resp = requests.post(create_url, headers=headers, json=task_payload, timeout=20)
            res_json = resp.json()
            code = res_json.get("code")
            msg = res_json.get("msg", "")

            # Success
            if code == 200 and res_json.get("data", {}).get("taskId"):
                task_id = res_json["data"]["taskId"]
                assigned_key = active_key
                break

            # Insufficient credits or payment required or rate limit
            is_credit_issue = (
                resp.status_code == 402
                or code == 402
                or "credit" in msg.lower()
                or "balance" in msg.lower()
                or "quota" in msg.lower()
                or "exhausted" in msg.lower()
            )
            if is_credit_issue:
                key_manager.mark_exhausted(active_key, reason=f"Quota/Credit issue: {msg}")
                continue
            else:
                # Other non-fatal error, retry next key
                logger.warning(f"createTask failed on key {active_key[:6]}... (code={code}, msg={msg})")
                key_manager.mark_exhausted(active_key, reason=msg)
        except Exception as exc:
            logger.warning(f"createTask exception on key attempt {attempt+1}: {exc}")
            key_manager.mark_exhausted(active_key, reason=str(exc))

    if not task_id:
        return {
            "success": False,
            "error": "Failed to initialize thumbnail task across all available cluster keys. Please verify connectivity or key quotas.",
        }

    # Task created, now poll for completion
    poll_headers = {"Authorization": f"Bearer {assigned_key}"}
    record_url = f"{_API_BASE_URL}/api/v1/jobs/recordInfo?taskId={task_id}"

    start_time = time.time()
    poll_interval = 2.5

    while time.time() - start_time < poll_timeout_seconds:
        elapsed = int(time.time() - start_time)
        pct = min(92, 15 + int(elapsed * 1.5))
        if progress_callback:
            progress_callback(pct, f"Rendering thumbnail ({elapsed}s elapsed)...")

        try:
            poll_resp = requests.get(record_url, headers=poll_headers, timeout=15)
            if poll_resp.status_code == 200:
                p_data = poll_resp.json().get("data", {})
                state = p_data.get("state")

                if state == "success":
                    # Parse image URLs
                    result_urls: List[str] = []
                    # 1. Check response.resultUrls
                    resp_obj = p_data.get("response")
                    if isinstance(resp_obj, dict) and resp_obj.get("resultUrls"):
                        result_urls.extend(resp_obj["resultUrls"])

                    # 2. Check resultJson
                    r_json_str = p_data.get("resultJson")
                    if r_json_str and not result_urls:
                        try:
                            parsed_rj = json.loads(r_json_str)
                            if isinstance(parsed_rj, dict) and parsed_rj.get("resultUrls"):
                                result_urls.extend(parsed_rj["resultUrls"])
                        except Exception:
                            pass

                    # 3. Fallback regex search for URLs in raw body
                    if not result_urls:
                        raw_body = json.dumps(p_data)
                        found = re.findall(r"https://[^\s\"\'\\]+\.(?:jpg|jpeg|png|webp)", raw_body)
                        result_urls.extend(found)

                    key_manager.record_success(assigned_key)
                    if progress_callback:
                        progress_callback(100, "Thumbnail rendered successfully!")

                    return {
                        "success": True,
                        "result_urls": result_urls,
                        "task_id": task_id,
                        "model": model_id,
                        "cost_time": p_data.get("costTime") or elapsed,
                        "credits_consumed": float(p_data.get("creditsConsumed") or model_meta["credits"]),
                        "raw_response": p_data,
                    }

                elif state == "fail":
                    fail_msg = p_data.get("failMsg") or "Task reported failure."
                    return {
                        "success": False,
                        "error": f"Generation failed: {fail_msg}",
                        "task_id": task_id,
                    }
        except Exception as e:
            logger.warning(f"Polling recordInfo error: {e}")

        time.sleep(poll_interval)
        poll_interval = min(5.0, poll_interval + 0.5)

    return {
        "success": False,
        "error": f"Generation timed out after {poll_timeout_seconds}s. The task might still complete on the cluster.",
        "task_id": task_id,
    }
