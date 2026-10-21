# app/services/key_rotator.py
# High-Availability Unlimited Multi-Key Rotation & Zero-Downtime Failover System for BangAI Credits
import os
import sys
import time
import json
import threading
from typing import Dict, List, Optional, Tuple, Any
from loguru import logger

# Paths for persistent cloud volume on Modal or local environment
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORAGE_DIR = "/root/storage" if (os.path.isdir("/root/storage") or os.environ.get("MODAL_IMAGE_ID") or ROOT_DIR == "/root") else os.path.join(ROOT_DIR, "storage")
SYSTEM_KEYS_FILE = os.path.join(STORAGE_DIR, "system_api_keys.json")
REMOTE_KEYS_ENDPOINT = "https://bangai.netlify.app/.netlify/functions/admin?action=get-system-keys"


class KeyRotator:
    """
    Manages multi-key pools for system services (JSON2Video, LLMs, Stock Footage, Voice TTS).
    Features:
    1. Unlimited API keys per service.
    2. Round-Robin load balancing across healthy keys.
    3. Automatic error detection (HTTP 429, Quota Exhausted, Rate Limit, Insufficient Balance).
    4. Zero-downtime failover to backup keys with automatic cooldown timers.
    5. Automatic self-healing: keys recover after cooldown expires.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._pools: Dict[str, List[dict]] = {}
        self._indices: Dict[str, int] = {}
        self._models: Dict[str, str] = {
            "gemini": "gemini-3.5-flash",
            "openai": "gpt-4o",
            "deepseek": "deepseek-chat",
            "groq": "llama-3.3-70b-versatile"
        }
        self._last_loaded = 0.0
        self.reload_keys()

    def get_active_model(self, provider: str) -> Optional[str]:
        """Returns the configured model name for this provider from the cloud control plane."""
        with self._lock:
            if not provider:
                return None
            return self._models.get(str(provider).lower().strip())

    def set_active_model(self, provider: str, model_name: str):
        """Sets the active model name for a provider."""
        with self._lock:
            if provider and model_name:
                self._models[str(provider).lower().strip()] = str(model_name).strip()

    def reload_keys(self, force_remote: bool = False) -> bool:
        """Loads or reloads keys from remote Netlify control plane (Atlas) or local persistent volume."""
        with self._lock:
            now = time.time()
            loaded_data = None

            # 1. Proactive remote fetch if forced, empty in memory, or cache TTL expired (>20s)
            is_stale = (now - self._last_loaded) > 20.0
            is_empty = len(self._pools) == 0 or all(len(p) == 0 for p in self._pools.values())

            if force_remote or is_stale or is_empty:
                loaded_data = self._fetch_remote_keys()

            # 2. If remote fetch succeeded, apply and persist
            if loaded_data and isinstance(loaded_data, dict) and len(loaded_data) > 0:
                self._apply_raw_pools(loaded_data)
                self._last_loaded = now
                try:
                    if not os.path.exists(STORAGE_DIR):
                        from app.utils import utils
                        utils.safe_makedirs(STORAGE_DIR, exist_ok=True)
                    with open(SYSTEM_KEYS_FILE, "w", encoding="utf-8") as f:
                        json.dump(loaded_data, f, indent=2)
                except Exception:
                    pass
                return True

            # 3. Fallback: load from persistent volume file if remote was unreachable
            if os.path.exists(SYSTEM_KEYS_FILE):
                try:
                    with open(SYSTEM_KEYS_FILE, "r", encoding="utf-8") as f:
                        disk_data = json.load(f)
                    if disk_data and isinstance(disk_data, dict):
                        self._apply_raw_pools(disk_data)
                        self._last_loaded = now
                        logger.info(f"[KeyRotator] Loaded keys from local storage fallback: {SYSTEM_KEYS_FILE}")
                        return True
                except Exception as e:
                    logger.warning(f"[KeyRotator] Local keys fallback read notice: {e}")

            return False

    def ensure_fresh_keys(self, max_age_seconds: float = 15.0) -> bool:
        """Ensures that in-memory key pools are updated within max_age_seconds."""
        with self._lock:
            if (time.time() - self._last_loaded) > max_age_seconds:
                return self.reload_keys(force_remote=True)
            return True

    def _fetch_remote_keys(self) -> Optional[dict]:
        """Fetch active system keys from Netlify administrative control plane (Atlas) or Modal API."""
        import requests

        # 1. Primary: Netlify get-system-keys (direct from live MongoDB Atlas)
        try:
            res = requests.get(REMOTE_KEYS_ENDPOINT, timeout=6)
            if res.ok:
                resp_json = res.json()
                data = resp_json.get("data")
                if resp_json.get("success") and isinstance(data, dict):
                    if resp_json.get("models") and isinstance(resp_json["models"], dict):
                        data["models"] = resp_json["models"]
                    logger.info(f"[KeyRotator] Successfully fetched live system keys from Netlify ({len(data)} pools)")
                    return data
        except Exception as e:
            logger.debug(f"[KeyRotator] Netlify remote fetch notice: {e}")

        # 2. Secondary fallback: Modal Cluster local/serve endpoints
        modal_endpoints = [
            "https://cmpunktg--bangai-stock-studio-serve.modal.run/api/bangai/system-keys",
            "https://cmpunktg1--bangai-stock-studio-serve.modal.run/api/bangai/system-keys",
        ]
        for ep in modal_endpoints:
            try:
                res = requests.get(ep, timeout=5)
                if res.ok:
                    resp_json = res.json()
                    data = resp_json.get("data")
                    if isinstance(data, dict) and len(data) > 0:
                        if resp_json.get("models") and isinstance(resp_json["models"], dict):
                            data["models"] = resp_json["models"]
                        logger.info(f"[KeyRotator] Successfully fetched system keys from Modal endpoint {ep}")
                        return data
            except Exception as me:
                logger.debug(f"[KeyRotator] Modal endpoint fetch notice ({ep}): {me}")

        return None

    def _apply_raw_pools(self, raw_data: dict):
        """Standardize raw key lists into structured pool items."""
        with self._lock:
            # Apply configured models if present
            models_dict = raw_data.get("models")
            if isinstance(models_dict, dict):
                for p, m in models_dict.items():
                    if isinstance(m, str) and m.strip():
                        self._models[p.lower().strip()] = m.strip()

            for provider, keys_raw in raw_data.items():
                if provider == "models":
                    continue
                if not isinstance(keys_raw, list):
                    continue
                pool_items = []
                for idx, item in enumerate(keys_raw):
                    if isinstance(item, str):
                        k_str = item.strip()
                        if not k_str:
                            continue
                        pool_items.append({
                            "key": k_str,
                            "label": f"{provider.capitalize()} Key #{idx + 1}",
                            "status": "active",
                            "cooldown_until": 0.0,
                            "fail_count": 0,
                            "success_count": 0,
                            "last_used": 0.0
                        })
                    elif isinstance(item, dict) and item.get("key"):
                        k_str = str(item.get("key", "")).strip()
                        if not k_str:
                            continue
                        status = str(item.get("status", "active")).lower()
                        bal = float(item.get("balance", 100.0)) if item.get("balance") is not None else 100.0
                        now_ts = time.time()
                        init_cooldown = (now_ts + 3600.0) if (status in ("exhausted", "invalid") or bal <= 0) else float(item.get("cooldown_until", 0.0))
                        pool_items.append({
                            "key": k_str,
                            "label": item.get("label") or f"{provider.capitalize()} Key #{idx + 1}",
                            "status": status,
                            "balance": bal,
                            "cooldown_until": init_cooldown,
                            "fail_count": int(item.get("fail_count", 0)),
                            "success_count": int(item.get("success_count", 0)),
                            "last_used": float(item.get("last_used", 0.0))
                        })

                # Sort keys: healthy keys with higher balance first, exhausted/cooling keys last
                now_ts = time.time()
                pool_items.sort(
                    key=lambda x: (
                        1 if x["cooldown_until"] <= now_ts else 0,
                        x.get("balance", 0.0)
                    ),
                    reverse=True
                )
                self._pools[provider] = pool_items
                if provider not in self._indices:
                    self._indices[provider] = 0

            # Automatically synchronize active pool keys to runtime config
            self.sync_to_runtime_config()

    def get_keys(self, provider: str) -> List[str]:
        """Returns all raw key strings in this provider's pool."""
        with self._lock:
            clean = str(provider).strip().lower()
            pool = self._pools.get(clean, [])
            return [p["key"] for p in pool if p.get("key")]

    def has_keys(self, provider: str) -> bool:
        """Returns True if this provider has at least one key configured in its pool or aliases."""
        with self._lock:
            clean = str(provider).strip().lower()
            pool = self._pools.get(clean, [])
            if len(pool) > 0:
                return True
            aliases = {
                "stock_footage": ["pexels", "pixabay"],
                "voice_tts": ["azure_speech", "elevenlabs", "siliconflow"],
                "llm": ["gemini", "openai", "deepseek", "groq"]
            }
            for alias in aliases.get(clean, []):
                if len(self._pools.get(alias, [])) > 0:
                    return True
            return False

    def sync_to_runtime_config(self):
        """
        Dynamically bridges active Bang API Credits from Atlas/KeyRotator into runtime config.app.
        Guarantees that ANY legacy validation check or material lookup across the app succeeds
        seamlessly when Bang API Credits are active.
        """
        try:
            from app.config import config
            # 1. Stock Footage (Pexels / Pixabay)
            if config.bangai_credits.get("use_bang_material_credits", False):
                pexels_keys = self.get_keys("pexels")
                if pexels_keys:
                    config.app["pexels_api_keys"] = pexels_keys
                pixabay_keys = self.get_keys("pixabay")
                if pixabay_keys:
                    config.app["pixabay_api_keys"] = pixabay_keys

            # 2. JSON2Video & Voice
            if config.bangai_credits.get("use_bang_j2v_credits", False) or config.bangai_credits.get("use_bang_voice_credits", False):
                j2v_keys = self.get_keys("json2video")
                if j2v_keys and not config.json2video.get("api_key"):
                    config.json2video["api_key"] = j2v_keys[0]

            # 3. ElevenLabs TTS
            if config.bangai_credits.get("use_bang_voice_credits", False):
                el_keys = self.get_keys("elevenlabs")
                if el_keys and not config.elevenlabs.get("api_key"):
                    config.elevenlabs["api_key"] = el_keys[0]

            # 4. Azure Speech TTS
            if config.bangai_credits.get("use_bang_voice_credits", False):
                az_keys = self.get_keys("azure_speech")
                if az_keys and not config.azure.get("speech_key"):
                    config.azure["speech_key"] = az_keys[0]
                    if not config.azure.get("speech_region"):
                        config.azure["speech_region"] = "eastus"

            # 5. LLM Script & Storyboard Providers (Gemini, OpenAI, DeepSeek, Groq)
            use_llm_credits = bool(config.bangai_credits.get("use_bang_llm_credits", False))
            if use_llm_credits:
                for p in ["gemini", "openai", "deepseek", "groq"]:
                    cfg_k = f"{p}_api_key"
                    p_keys = self.get_keys(p)
                    if p_keys:
                        config.app[cfg_k] = p_keys[0]
        except Exception as e:
            logger.debug(f"[KeyRotator] sync_to_runtime_config notice: {e}")

    def get_key(self, service: str, provider: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Returns (api_key, label) for the requested service/provider using round-robin across healthy keys.
        Automatically skips keys in rate-limit cooldown.
        When a specific provider is requested (e.g. 'gemini'), strictly checks that provider's pool
        to avoid passing mismatched keys (e.g. OpenAI key to Gemini client).
        """
        with self._lock:
            target_pool_key = None
            if provider:
                clean_prov = str(provider).strip().lower()
                if clean_prov in self._pools and len(self._pools[clean_prov]) > 0:
                    target_pool_key = clean_prov
                elif service in self._pools and len(self._pools[service]) > 0 and service == clean_prov:
                    target_pool_key = service
            elif service in self._pools and len(self._pools[service]) > 0:
                target_pool_key = service
            else:
                # Alias lookups only when NO specific provider was requested
                aliases = {
                    "stock_footage": ["pexels", "pixabay"],
                    "voice_tts": ["azure_speech", "elevenlabs", "siliconflow"],
                    "llm": ["gemini", "openai", "deepseek", "groq"]
                }
                for alias in aliases.get(service, []):
                    if alias in self._pools and len(self._pools[alias]) > 0:
                        target_pool_key = alias
                        break

            if not target_pool_key or target_pool_key not in self._pools:
                return None, None

            pool = self._pools[target_pool_key]
            if not pool:
                return None, None

            now = time.time()
            total_keys = len(pool)
            start_idx = self._indices.get(target_pool_key, 0)

            # Look for an active or recovered key starting from current index
            selected_key = None
            for offset in range(total_keys):
                curr_idx = (start_idx + offset) % total_keys
                item = pool[curr_idx]

                # Check if cooling down
                if item["cooldown_until"] > now:
                    continue

                # Key is available! If it was cooling down, restore status
                if item["status"] != "active":
                    item["status"] = "active"

                item["last_used"] = now
                self._indices[target_pool_key] = (curr_idx + 1) % total_keys
                selected_key = (item["key"], item["label"])
                break

            # If all keys are currently cooling down, return the one with the earliest cooldown expiry
            if not selected_key:
                earliest_item = min(pool, key=lambda x: x["cooldown_until"])
                selected_key = (earliest_item["key"], earliest_item["label"])

            return selected_key

    def get_all_keys(self, service: str, provider: Optional[str] = None) -> List[str]:
        """Returns all raw key strings in the pool (for services like Pexels that accept multiple keys)."""
        with self._lock:
            target_pool = self._pools.get(provider or service) or []
            return [item["key"] for item in target_pool if item.get("key")]

    def report_failure(self, service: str, key: str, status_code: int = 429, error_text: str = ""):
        """
        Marks a key as rate-limited or exhausted, setting a cooldown period so subsequent requests
        seamlessly failover to the next healthy key without crashing.
        """
        with self._lock:
            now = time.time()
            is_rate_limit = (status_code == 429) or ("rate" in error_text.lower() and "limit" in error_text.lower()) or ("429" in error_text)
            is_quota_exhausted = (status_code in (401, 403)) or any(
                w in error_text.lower() for w in [
                    "quota", "exhausted", "credits", "balance", "insufficient", "resource_exhausted",
                    "upgrade your plan", "not enough credits", "doesn't have enough credits", "failed in render"
                ]
            )

            # Determine cooldown duration (3 minutes for rate limits; 1 hour for exhausted balance)
            cooldown_secs = 180.0 if is_rate_limit else (3600.0 if is_quota_exhausted else 120.0)
            status_label = "rate_limited" if is_rate_limit else "exhausted"

            for p_name, pool in self._pools.items():
                for item in pool:
                    if item["key"] == key:
                        item["fail_count"] += 1
                        item["status"] = status_label
                        item["cooldown_until"] = now + cooldown_secs
                        logger.warning(
                            f"[KeyRotator] Service '{p_name}' key '{item.get('label')}' hit error ({status_code}). "
                            f"Marked as {status_label} for {int(cooldown_secs)}s. Automatic failover active."
                        )
                        self._persist_current_state()
                        return

    def report_success(self, service: str, key: str):
        """Marks key as healthy and increments success count."""
        with self._lock:
            for p_name, pool in self._pools.items():
                for item in pool:
                    if item["key"] == key:
                        item["success_count"] += 1
                        item["status"] = "active"
                        item["cooldown_until"] = 0.0
                        return

    def is_exhaustion_error(self, error: Any, status_code: Optional[int] = None) -> bool:
        """Determines if an error is a rate limit or quota exhaustion that warrants failover."""
        if status_code in (429, 401, 403):
            return True
        err_str = str(error).lower()
        trigger_terms = [
            "429", "too many requests", "rate limit", "ratelimit",
            "resource_exhausted", "quota", "exceeded", "balance",
            "insufficient_quota", "credits", "unauthorized", "expired",
            "forbidden", "credit balance", "upgrade your plan",
            "not enough credits", "doesn't have enough credits", "failed in render",
            "payment required"
        ]
        return any(term in err_str for term in trigger_terms)

    def get_pool_status(self, service: Optional[str] = None) -> dict:
        """Returns statistics for UI indicators."""
        with self._lock:
            now = time.time()
            if service:
                pool = self._pools.get(service) or []
                total = len(pool)
                active = sum(1 for k in pool if k["cooldown_until"] <= now)
                cooling = total - active
                return {"total": total, "active": active, "cooling": cooling, "provider": service}

            stats = {}
            for prov, pool in self._pools.items():
                total = len(pool)
                active = sum(1 for k in pool if k["cooldown_until"] <= now)
                stats[prov] = {"total": total, "active": active, "cooling": total - active}
            return stats

    def update_keys_from_dict(self, keys_data: dict):
        """Saves new keys received via API/UI to storage and applies immediately."""
        with self._lock:
            self._apply_raw_pools(keys_data)
            try:
                if not os.path.exists(STORAGE_DIR):
                    from app.utils import utils
                    utils.safe_makedirs(STORAGE_DIR, exist_ok=True)
                with open(SYSTEM_KEYS_FILE, "w", encoding="utf-8") as f:
                    json.dump(keys_data, f, indent=2)
                logger.success(f"[KeyRotator] Persisted updated keys to {SYSTEM_KEYS_FILE}")
            except Exception as e:
                logger.error(f"[KeyRotator] Failed saving keys to storage: {e}")

    def _persist_current_state(self):
        """Quietly updates storage file with fail_count and cooldown states."""
        try:
            if not os.path.exists(STORAGE_DIR):
                from app.utils import utils
                utils.safe_makedirs(STORAGE_DIR, exist_ok=True)
            with open(SYSTEM_KEYS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._pools, f, indent=2)
        except Exception:
            pass


# Global singleton instance
key_rotator = KeyRotator()
