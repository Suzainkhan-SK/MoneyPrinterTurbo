"""
BangAI Google OAuth 2.0 YouTube Publishing Service for Stock Studio (MoneyPrinterTurbo).

Features:
- Discovers user's connected YouTube channels from BangAI MongoDB/Netlify backend.
- Supports multi-channel selection (same as BangAI website).
- Direct Google Resumable Upload streaming from local/cloud disk without third-party fees.
- Dual-mode support: Manual Review Upload (from Task Manager) and Automatic Upload (post-render).
- Coexists seamlessly alongside Upload-Post.
"""

import os
import re
import json
import requests
from typing import Optional, List, Dict, Any
from loguru import logger
from app.config import config

BANGAI_API_BASE = os.getenv("BANGAI_API_BASE", "https://bangai.netlify.app/.netlify/functions")


class YouTubeOAuthService:
    """Service to handle BangAI Google OAuth channel synchronization and video publishing."""

    def __init__(self):
        self._channels_cache = {}

    def get_user_credentials(self) -> Dict[str, str]:
        """Resolve active user ID, email, and JWT token from session or environment."""
        import streamlit as st
        token = ""
        user_id = ""
        email = ""

        try:
            # 1. Check Streamlit query params
            params = st.query_params
            token = params.get("token", "") or ""
            user_id = params.get("user_id", "") or ""
            email = params.get("email", "") or ""
        except Exception:
            pass

        # 2. Check session state fallback
        try:
            token = token or st.session_state.get("bangai_token", "") or ""
            user_id = user_id or st.session_state.get("bangai_user_id", "") or ""
            email = email or st.session_state.get("bangai_email", "") or ""
        except Exception:
            pass

        # 3. Check active config user ID fallback
        if not user_id:
            try:
                from app.config.config import get_active_user_id
                user_id = get_active_user_id()
            except Exception:
                pass

        return {
            "token": str(token).strip(),
            "user_id": str(user_id).strip(),
            "email": str(email).strip().lower(),
        }

    def fetch_connected_channels(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch the list of connected YouTube channels for the current user from BangAI.
        Returns list of channel dicts with channelId, channelTitle, avatarUrl, isDefault, etc.
        """
        creds = self.get_user_credentials()
        token = creds.get("token")
        user_id = creds.get("user_id")
        email = creds.get("email")

        cache_key = f"{user_id}_{email}_{token[:10] if token else ''}"
        if not force_refresh and cache_key in self._channels_cache:
            return self._channels_cache[cache_key]

        if not token and not user_id and not email:
            logger.debug("[YouTube OAuth] No active user credentials found for channel discovery")
            return []

        url = f"{BANGAI_API_BASE}/google-oauth?action=list"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        params = {}
        if user_id:
            params["userId"] = user_id
        if email:
            params["email"] = email
        if token:
            params["token"] = token

        try:
            res = requests.get(url, headers=headers, params=params, timeout=12)
            if res.ok:
                data = res.json()
                channels = data.get("channels", [])
                self._channels_cache[cache_key] = channels
                logger.info(f"[YouTube OAuth] Discovered {len(channels)} connected channel(s) for user {user_id or email}")
                return channels
            else:
                logger.warning(f"[YouTube OAuth] Channel lookup returned HTTP {res.status_code}: {res.text[:200]}")
        except Exception as e:
            logger.warning(f"[YouTube OAuth] Failed to query connected channels: {e}")

        return []

    def get_channel_token(self, channel_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Obtain a fresh Google OAuth access token for the specified channel ID (or default channel).
        """
        creds = self.get_user_credentials()
        token = creds.get("token")
        user_id = creds.get("user_id")
        email = creds.get("email")

        url = f"{BANGAI_API_BASE}/google-oauth?action=get-token"
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        params = {}
        if channel_id:
            params["channelId"] = channel_id
        if user_id:
            params["userId"] = user_id
        if email:
            params["email"] = email
        if token:
            params["token"] = token

        try:
            res = requests.get(url, headers=headers, params=params, timeout=15)
            if res.ok:
                return res.json()
            else:
                logger.error(f"[YouTube OAuth] Failed to get channel token: HTTP {res.status_code} {res.text[:200]}")
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
    ) -> Dict[str, Any]:
        """
        Directly upload video to YouTube via official Google Resumable Upload protocol.
        """
        if not os.path.exists(video_path):
            error_msg = f"Video file not found: {video_path}"
            logger.error(f"[YouTube OAuth] {error_msg}")
            return {"success": False, "error": error_msg}

        file_size = os.path.getsize(video_path)
        if file_size <= 0:
            error_msg = f"Video file is empty (0 bytes): {video_path}"
            logger.error(f"[YouTube OAuth] {error_msg}")
            return {"success": False, "error": error_msg}

        # 1. Fetch fresh access token
        token_info = self.get_channel_token(channel_id)
        if not token_info or not token_info.get("accessToken"):
            error_msg = (
                token_info.get("message")
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

        clean_tags = [str(t).replace("<", "").replace(">", "").strip() for t in (tags or []) if str(t).strip()]
        if not clean_tags:
            clean_tags = ["Shorts", "AI", "BangAI"]

        valid_privacy = privacy_status.lower() if privacy_status.lower() in ["public", "private", "unlisted"] else "public"

        metadata = {
            "snippet": {
                "title": clean_title,
                "description": clean_desc,
                "tags": clean_tags[:30],
                "categoryId": "24",
                "defaultLanguage": "en",
            },
            "status": {
                "privacyStatus": valid_privacy,
                "selfDeclaredMadeForKids": bool(made_for_kids),
                "embeddable": True,
                "publicStatsViewable": True,
            },
        }

        logger.info(f"[YouTube OAuth] Initiating Resumable Upload for '{clean_title}' ({file_size} bytes) to channel '{channel_title}' ({valid_privacy})")

        init_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        }

        init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
        try:
            init_res = requests.post(init_url, headers=init_headers, json=metadata, timeout=30)
            if not init_res.ok:
                err_text = init_res.text[:300]
                logger.error(f"[YouTube OAuth] Init failed (HTTP {init_res.status_code}): {err_text}")
                return {
                    "success": False,
                    "error": f"YouTube API rejected initialization: {err_text}",
                    "status_code": init_res.status_code,
                }

            upload_url = init_res.headers.get("Location")
            if not upload_url:
                return {"success": False, "error": "YouTube API did not return upload location URL"}

            logger.info("[YouTube OAuth] Session URL received. Streaming video binary buffer to Google...")

            # 3. Stream binary video directly to Google Resumable Upload URI
            stream_headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(file_size),
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            }

            with open(video_path, "rb") as video_file:
                put_res = requests.put(upload_url, headers=stream_headers, data=video_file, timeout=600)

            video_id = None
            if put_res.status_code in (200, 201):
                res_json = put_res.json()
                video_id = res_json.get("id")
            elif put_res.status_code == 308:
                # Query range recovery if partial
                logger.warning("[YouTube OAuth] Received HTTP 308, verifying completion...")
                status_res = requests.put(
                    upload_url,
                    headers={"Content-Length": "0", "Content-Range": f"bytes */{file_size}"},
                    timeout=15,
                )
                if status_res.status_code in (200, 201):
                    video_id = status_res.json().get("id")

            if not video_id:
                err_body = put_res.text[:300] if hasattr(put_res, "text") else "Unknown error"
                logger.error(f"[YouTube OAuth] Upload finished without video ID: HTTP {put_res.status_code} {err_body}")
                return {"success": False, "error": f"Upload streaming failed: {err_body}"}

            youtube_url = f"https://youtube.com/shorts/{video_id}"
            logger.success(f"[YouTube OAuth] 🎉 Video successfully published to YouTube! URL: {youtube_url}")

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
        import glob
        from datetime import datetime

        # Find task directory
        possible_dirs = [
            f"storage/tasks/{task_id}",
            f"/root/storage/tasks/{task_id}",
        ]
        # Also check multi-tenant user paths
        matches = glob.glob(f"storage/users/*/tasks/{task_id}") + glob.glob(f"/root/storage/users/*/tasks/{task_id}")
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
