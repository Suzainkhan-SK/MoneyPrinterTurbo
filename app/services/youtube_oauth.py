"""
BangAI Google OAuth 2.0 YouTube Publishing Service for Stock Studio (MoneyPrinterTurbo).

Features:
- Discovers user's connected YouTube channels from BangAI MongoDB/Netlify backend.
- Supports multi-channel selection (same as BangAI website).
- Direct Google Resumable Upload streaming from local/cloud disk without third-party fees.
- Dual-mode support: Manual Review Upload (from Task Manager) and Automatic Upload (post-render).
- Decodes JWT tokens to extract real BangAI user identities even on direct full-window visits.
- Coexists seamlessly alongside Upload-Post.
"""

import os
import re
import json
import base64
import glob
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any
from loguru import logger
from app.config import config

BANGAI_API_BASE = os.getenv("BANGAI_API_BASE", "https://bangai.netlify.app/.netlify/functions")


class YouTubeOAuthService:
    """Service to handle BangAI Google OAuth channel synchronization and video publishing."""

    def __init__(self):
        self._channels_cache = {}
        self._cached_user_id = ""
        self._cached_token = ""
        self._cached_email = ""

    @staticmethod
    def decode_jwt_unverified(token_str: str) -> Dict[str, Any]:
        """Decode unverified JWT payload to extract user ID and email."""
        if not token_str or not isinstance(token_str, str):
            return {}
        parts = token_str.strip().split(".")
        if len(parts) < 2:
            return {}
        try:
            payload_b64 = parts[1]
            padding = 4 - (len(payload_b64) % 4)
            padded = payload_b64 + ("=" * (padding % 4))
            raw = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
            data = json.loads(raw)
            return {
                "userId": data.get("userId") or data.get("id") or data.get("uid") or "",
                "email": data.get("email") or "",
                "name": data.get("name") or "",
            }
        except Exception:
            return {}

    def is_configured(self) -> bool:
        """Check if YouTube OAuth is enabled and configured with connected channels."""
        try:
            cfg = getattr(config, "youtube_oauth", {})
            if not cfg.get("enabled", True):
                return False
            channels = self.fetch_connected_channels()
            return len(channels) > 0
        except Exception as e:
            logger.debug(f"[YouTube OAuth] is_configured check: {e}")
            return False

    @property
    def auto_upload(self) -> bool:
        """Check if auto-upload mode is active."""
        try:
            cfg = getattr(config, "youtube_oauth", {})
            mode = str(cfg.get("upload_mode", "manual")).lower()
            return bool(cfg.get("auto_upload", False) or mode in ("auto", "automatic"))
        except Exception:
            return False

    @property
    def default_privacy_status(self) -> str:
        """Default privacy status for uploaded videos (public, unlisted, private)."""
        try:
            cfg = getattr(config, "youtube_oauth", {})
            return str(cfg.get("default_privacy_status", "public")).lower()
        except Exception:
            return "public"

    @property
    def made_for_kids(self) -> bool:
        """Self declared made for kids flag."""
        try:
            cfg = getattr(config, "youtube_oauth", {})
            return bool(cfg.get("made_for_kids", False))
        except Exception:
            return False

    @property
    def selected_channel_id(self) -> str:
        """Selected YouTube Channel ID."""
        try:
            cfg = getattr(config, "youtube_oauth", {})
            cid = cfg.get("selected_channel_id", "")
            if not cid:
                channels = self.fetch_connected_channels()
                if channels:
                    default_ch = next((c for c in channels if c.get("isDefault")), channels[0])
                    cid = default_ch.get("channelId", "")
            return cid
        except Exception:
            return ""

    def get_user_credentials(
        self, user_id: Optional[str] = None, task_id: Optional[str] = None
    ) -> Dict[str, str]:
        """Resolve active user ID, email, and JWT token from memory, session, task, disk or environment."""
        token = ""
        resolved_uid = str(user_id or "").strip()
        email = ""

        # 0. Check in-memory cache if not supplied
        if not resolved_uid or resolved_uid in ("guest", "default_user"):
            if self._cached_user_id and self._cached_user_id not in ("guest", "default_user"):
                resolved_uid = self._cached_user_id
        if self._cached_token and not token:
            token = self._cached_token
        if self._cached_email and not email:
            email = self._cached_email

        # 1. Check utils.get_current_user_id()
        if not resolved_uid or resolved_uid in ("guest", "default_user"):
            try:
                u_uid = utils.get_current_user_id()
                if u_uid and u_uid not in ("guest", "default_user"):
                    resolved_uid = u_uid
            except Exception:
                pass

        # 2. Check config active user ID
        if not resolved_uid or resolved_uid in ("guest", "default_user"):
            try:
                from app.config.config import get_active_user_id
                active_c = get_active_user_id()
                if active_c and active_c not in ("guest", "default_user"):
                    resolved_uid = active_c
            except Exception:
                pass

        # 3. Check Streamlit context safely if available
        try:
            import streamlit as st
            # Session state first (most reliable during active user session)
            token = token or st.session_state.get("bangai_token", "") or ""
            if not resolved_uid or resolved_uid in ("guest", "default_user"):
                resolved_uid = st.session_state.get("bangai_user_id", "") or ""
            email = email or st.session_state.get("bangai_email", "") or ""

            # Then query params
            params = st.query_params
            token = token or params.get("token", "") or ""
            if not resolved_uid or resolved_uid in ("guest", "default_user"):
                resolved_uid = params.get("user_id", "") or params.get("uid", "") or ""
            email = email or params.get("email", "") or ""
        except Exception:
            pass

        # 4. If task_id provided, deduce user ID from task storage directory
        if (not resolved_uid or resolved_uid in ("guest", "default_user")) and task_id:
            try:
                task_dir = utils.task_dir()
                norm = os.path.normpath(task_dir).replace("\\", "/")
                parts = norm.split("/")
                if "users" in parts:
                    idx = parts.index("users")
                    if idx + 1 < len(parts):
                        candidate = parts[idx + 1].strip()
                        if candidate and candidate not in ("guest", "default_user"):
                            resolved_uid = candidate
            except Exception:
                pass

        # 5. If token present, decode unverified JWT payload to extract userId and email
        if token:
            jwt_data = self.decode_jwt_unverified(token)
            if not resolved_uid or resolved_uid in ("guest", "default_user"):
                resolved_uid = jwt_data.get("userId") or resolved_uid
            if not email:
                email = jwt_data.get("email") or email

        # 6. Check persisted user auth file on disk if user_id known
        if resolved_uid and resolved_uid not in ("guest", "default_user"):
            for base_dir in ["storage", "/root/storage"]:
                auth_file = os.path.join(base_dir, "users", resolved_uid, "auth.json")
                if os.path.isfile(auth_file):
                    try:
                        with open(auth_file, "r", encoding="utf-8") as f:
                            disk_auth = json.load(f)
                        token = token or disk_auth.get("token", "")
                        email = email or disk_auth.get("email", "")
                        break
                    except Exception:
                        pass

        # Cache valid findings
        if resolved_uid and resolved_uid not in ("guest", "default_user"):
            self._cached_user_id = resolved_uid
        if token:
            self._cached_token = token
        if email:
            self._cached_email = email

        return {
            "token": str(token).strip(),
            "user_id": str(resolved_uid).strip(),
            "email": str(email).strip().lower(),
        }

    def save_user_credentials(self, user_id: str, token: str = "", email: str = ""):
        """Persist user auth to memory and disk so background render tasks can access it without Streamlit context."""
        clean_uid = str(user_id).strip()
        if not clean_uid or clean_uid in ("default_user", "guest"):
            return
        token_str = str(token).strip() if token else ""
        email_str = str(email).strip().lower() if email else ""

        # Avoid redundant disk writes on repeated Streamlit reruns
        if (
            self._cached_user_id == clean_uid
            and self._cached_token == token_str
            and self._cached_email == email_str
        ):
            return

        self._cached_user_id = clean_uid
        if token_str:
            self._cached_token = token_str
        if email_str:
            self._cached_email = email_str

        for base_path in ["storage", "/root/storage"]:
            user_dir = os.path.join(base_path, "users", clean_uid)
            try:
                os.makedirs(user_dir, exist_ok=True)
                auth_file = os.path.join(user_dir, "auth.json")
                with open(auth_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {"token": self._cached_token, "email": self._cached_email, "userId": clean_uid},
                        f,
                        indent=2,
                    )
            except Exception as ex:
                logger.debug(f"[YouTube OAuth] Could not save {user_dir}/auth.json: {ex}")

    def fetch_connected_channels(
        self, force_refresh: bool = False, user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch the list of connected YouTube channels for the current user from BangAI.
        Returns list of channel dicts with channelId, channelTitle, avatarUrl, isDefault, etc.
        """
        creds = self.get_user_credentials(user_id=user_id)
        token = creds.get("token")
        uid = creds.get("user_id")
        email = creds.get("email")

        cache_key = f"{uid}_{email}_{token[:10] if token else ''}"
        if not force_refresh and cache_key in self._channels_cache:
            return self._channels_cache[cache_key]

        if not token and not uid and not email:
            logger.debug("[YouTube OAuth] No active user credentials found for channel discovery")
            return []

        url = f"{BANGAI_API_BASE}/google-oauth"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        params = {"action": "list"}
        if uid and uid not in ("guest", "default_user"):
            params["userId"] = uid
        if email:
            params["email"] = email
        if token:
            params["token"] = token

        try:
            res = requests.get(url, headers=headers, params=params, timeout=4)
            if res.ok:
                data = res.json()
                channels = data.get("channels", [])
                self._channels_cache[cache_key] = channels
                logger.info(
                    f"[YouTube OAuth] Discovered {len(channels)} connected channel(s) for user {uid or email}"
                )
                return channels
            else:
                logger.warning(
                    f"[YouTube OAuth] Channel lookup returned HTTP {res.status_code}: {res.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"[YouTube OAuth] Failed to query connected channels: {e}")

        return []

    def get_channel_token(
        self,
        channel_id: Optional[str] = None,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Obtain a fresh Google OAuth access token for the specified channel ID (or default channel).
        """
        creds = self.get_user_credentials(user_id=user_id, task_id=task_id)
        token = creds.get("token")
        uid = creds.get("user_id")
        email = creds.get("email")

        url = f"{BANGAI_API_BASE}/google-oauth"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        params = {"action": "get-token"}
        if channel_id:
            params["channelId"] = channel_id
        if uid and uid not in ("guest", "default_user"):
            params["userId"] = uid
        if email:
            params["email"] = email
        if token:
            params["token"] = token

        try:
            res = requests.get(url, headers=headers, params=params, timeout=15)
            if res.ok:
                return res.json()
            else:
                logger.error(
                    f"[YouTube OAuth] Failed to get channel token: HTTP {res.status_code} {res.text[:200]}"
                )
                try:
                    return res.json()
                except Exception:
                    return {"success": False, "error": f"HTTP {res.status_code}: {res.text[:100]}"}
        except Exception as e:
            logger.error(f"[YouTube OAuth] Exception fetching channel token: {e}")

        return None

    def upload_video_oauth(
        self,
        video_path: str,
        title: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        privacy_status: str = "public",
        made_for_kids: bool = False,
        channel_id: Optional[str] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Directly upload video to YouTube via official Google Resumable Upload protocol.
        """
        # Resolve real path
        resolved_video_path = video_path
        if not resolved_video_path or not os.path.exists(resolved_video_path):
            candidates = [
                os.path.abspath(video_path or ""),
                os.path.join("/root", (video_path or "").lstrip("./")),
                os.path.join(".", (video_path or "").lstrip("./")),
            ]
            found = False
            for c in candidates:
                if c and os.path.isfile(c):
                    resolved_video_path = c
                    found = True
                    break
            if not found:
                error_msg = f"Video file not found: {video_path}"
                logger.error(f"[YouTube OAuth] {error_msg}")
                return {"success": False, "error": error_msg}

        file_size = os.path.getsize(resolved_video_path)
        if file_size <= 0:
            error_msg = f"Video file is empty (0 bytes): {resolved_video_path}"
            logger.error(f"[YouTube OAuth] {error_msg}")
            return {"success": False, "error": error_msg}

        # 1. Fetch fresh access token
        token_info = self.get_channel_token(
            channel_id=channel_id, user_id=user_id, task_id=task_id
        )
        if not token_info or not token_info.get("accessToken"):
            error_msg = (
                token_info.get("message") or token_info.get("error")
                if token_info
                else "No valid YouTube OAuth token found. Please connect your channel in BangAI Profile."
            )
            logger.error(f"[YouTube OAuth] {error_msg}")
            return {"success": False, "error": error_msg, "needsReconnect": True}

        access_token = token_info["accessToken"]
        target_channel_id = token_info.get("channelId", channel_id or "")
        channel_title = token_info.get("channelTitle", "YouTube Channel")

        # 2. Sanitize and prepare metadata
        clean_title = (title or "AI Generated Video")[:100]
        base_desc = (description or "").strip()
        if "#Shorts" not in base_desc and "#shorts" not in base_desc:
            clean_desc = f"{base_desc}\n\n#Shorts\nGenerated with Bang AI".strip()
        else:
            clean_desc = base_desc

        clean_tags = [
            str(t).replace("<", "").replace(">", "").strip()
            for t in (tags or [])
            if str(t).strip()
        ]
        if not clean_tags:
            clean_tags = ["Shorts", "AI", "BangAI"]

        valid_privacy = (
            privacy_status.lower()
            if privacy_status.lower() in ["public", "private", "unlisted"]
            else "public"
        )

        metadata = {
            "snippet": {
                "title": clean_title,
                "description": clean_desc,
                "tags": clean_tags[:30],
                "categoryId": "24",
            },
            "status": {
                "privacyStatus": valid_privacy,
                "selfDeclaredMadeForKids": bool(made_for_kids),
                "embeddable": True,
                "publicStatsViewable": True,
            },
        }

        logger.info(
            f"[YouTube OAuth] Initiating Resumable Upload for '{clean_title}' "
            f"({file_size} bytes) to channel '{channel_title}' ({valid_privacy})"
        )

        init_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        }

        init_url = (
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status"
        )
        try:
            init_res = requests.post(
                init_url, headers=init_headers, json=metadata, timeout=30
            )
            if not init_res.ok:
                err_text = init_res.text[:400]
                logger.error(
                    f"[YouTube OAuth] Init failed (HTTP {init_res.status_code}): {err_text}"
                )
                return {
                    "success": False,
                    "error": f"YouTube API rejected initialization ({init_res.status_code}): {err_text}",
                    "status_code": init_res.status_code,
                }

            upload_url = init_res.headers.get("Location")
            if not upload_url:
                return {
                    "success": False,
                    "error": "YouTube API did not return upload location URL",
                }

            logger.info(
                f"[YouTube OAuth] Upload session created. Streaming {file_size} bytes to Google..."
            )

            # 3. Stream binary video directly to Google Resumable Upload URI
            with open(resolved_video_path, "rb") as vf:
                video_bytes = vf.read()

            stream_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(video_bytes)),
                "Content-Range": f"bytes 0-{len(video_bytes) - 1}/{len(video_bytes)}",
            }

            put_res = requests.put(
                upload_url, headers=stream_headers, data=video_bytes, timeout=600
            )

            video_id = None
            if put_res.status_code in (200, 201):
                res_json = put_res.json()
                video_id = res_json.get("id")
            elif put_res.status_code == 308:
                # Query range recovery if partial
                logger.warning("[YouTube OAuth] Received HTTP 308, verifying completion...")
                status_res = requests.put(
                    upload_url,
                    headers={
                        "Content-Length": "0",
                        "Content-Range": f"bytes */{len(video_bytes)}",
                    },
                    timeout=15,
                )
                if status_res.status_code in (200, 201):
                    video_id = status_res.json().get("id")

            if not video_id:
                err_body = (
                    put_res.text[:400] if hasattr(put_res, "text") else "Unknown error"
                )
                logger.error(
                    f"[YouTube OAuth] Upload finished without video ID: "
                    f"HTTP {put_res.status_code} {err_body}"
                )
                return {
                    "success": False,
                    "error": f"Upload streaming failed: {err_body}",
                }

            youtube_url = f"https://youtube.com/shorts/{video_id}"
            logger.success(
                f"[YouTube OAuth] [SUCCESS] Video successfully published to YouTube! URL: {youtube_url}"
            )

            # 4. Save to task state if task_id provided
            if task_id:
                self.record_task_youtube_upload(
                    task_id=task_id,
                    youtube_url=youtube_url,
                    video_id=video_id,
                    channel_title=channel_title,
                    channel_id=target_channel_id,
                    privacy_status=valid_privacy,
                )
                try:
                    from app.services import state as sm
                    sm.state.patch_task(
                        task_id,
                        youtube_url=youtube_url,
                        youtube_channel_title=channel_title,
                        youtube_status="uploaded",
                    )
                except Exception:
                    pass

            return {
                "success": True,
                "videoId": video_id,
                "videoUrl": youtube_url,
                "channelTitle": channel_title,
                "channelId": target_channel_id,
                "privacy": valid_privacy,
            }

        except Exception as e:
            logger.exception(f"[YouTube OAuth] Upload error: {e}")
            return {"success": False, "error": str(e)}

    def record_task_youtube_upload(
        self,
        task_id: str,
        youtube_url: str,
        video_id: str,
        channel_title: str,
        channel_id: str,
        privacy_status: str,
    ):
        """Update task's state.json so Task Manager permanently displays the published YouTube status."""
        possible_dirs = [
            f"storage/tasks/{task_id}",
            f"/root/storage/tasks/{task_id}",
        ]
        matches = glob.glob(f"storage/users/*/tasks/{task_id}") + glob.glob(
            f"/root/storage/users/*/tasks/{task_id}"
        )
        possible_dirs.extend(matches)

        for tdir in possible_dirs:
            state_file = os.path.join(tdir, "state.json")
            if os.path.isfile(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    data["youtube_url"] = youtube_url
                    data["youtube_video_id"] = video_id
                    data["youtube_channel_title"] = channel_title
                    data["youtube_channel_id"] = channel_id
                    data["youtube_privacy"] = privacy_status
                    data["youtube_uploaded_at"] = datetime.utcnow().isoformat() + "Z"
                    data["youtube_status"] = "uploaded"

                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)

                    logger.info(f"[YouTube OAuth] Saved YouTube upload state to {state_file}")

                    # Commit to cloud volume if on Modal
                    try:
                        import modal
                        modal.Volume.from_name("bangai-storage").commit()
                    except Exception:
                        pass
                    break
                except Exception as ex:
                    logger.warning(f"[YouTube OAuth] Could not update {state_file}: {ex}")


youtube_oauth_service = YouTubeOAuthService()
