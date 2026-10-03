"""
BangAI Thumbnail Studio - Dual-Panel UI Component
Professional YouTube viral thumbnail generation & editing studio.
Whitelisted branding: Powered by BangAI Studio Engine.
"""

import html
import json
import os
import time
from typing import Any, Dict, List, Optional
import requests
import streamlit as st
from loguru import logger

from app.services.thumbnail_service import (
    MODEL_CATALOG,
    VIRAL_PRESET_TEMPLATES,
    enhance_viral_prompt,
    generate_thumbnail,
    key_manager,
    upload_image_to_cdn,
)
from app.utils import utils


def _init_thumbnail_studio_state():
    """Initializes all necessary session_state keys for Thumbnail Studio."""
    default_model = "gpt-image-2-5-flare-text-to-image"
    st.session_state.setdefault("ts_selected_model", default_model)
    st.session_state.setdefault("ts_prompt", MODEL_CATALOG[default_model]["example_prompt"])
    st.session_state.setdefault("ts_aspect_ratio", "16:9")
    st.session_state.setdefault("ts_resolution", "2K")
    st.session_state.setdefault("ts_background", "auto")
    st.session_state.setdefault("ts_reference_urls", [])
    st.session_state.setdefault("ts_history", [])
    st.session_state.setdefault("ts_current_output", None)
    st.session_state.setdefault("ts_raw_json_output", None)
    st.session_state.setdefault("ts_last_uploaded_file_id", None)


def _handle_model_change():
    """Triggered when the user switches model to auto-update showcase context."""
    m_id = st.session_state.get("ts_selected_model")
    if m_id in MODEL_CATALOG:
        m_meta = MODEL_CATALOG[m_id]
        # If user hasn't heavily customized prompt or if switching modes, load example prompt
        st.session_state["ts_prompt"] = m_meta.get("example_prompt", "")


def render_thumbnail_studio():
    """Renders the complete BangAI Thumbnail Studio page."""
    _init_thumbnail_studio_state()

    # 1. Studio Header Banner
    st.markdown(
        """
        <div class="ts-header-box">
            <div class="ts-header-badge">⚡ BangAI Creative Suite</div>
            <h1 class="ts-header-title">YouTube Thumbnail Studio</h1>
            <p class="ts-header-subtitle">
                Engineered for maximum CTR. Generate and edit viral, hyper-engaging thumbnails with state-of-the-art vision models.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2. Main Dual-Panel Grid
    col_input, col_output = st.columns([1.02, 0.98], gap="medium")

    current_model_id = st.session_state["ts_selected_model"]
    model_meta = MODEL_CATALOG.get(current_model_id, MODEL_CATALOG["gpt-image-2-5-flare-text-to-image"])
    is_edit_mode = (model_meta["mode"] == "image-edit")

    # ─────────────────────────────────────────────────────────────────────────
    # LEFT PANEL: INPUT WORKBENCH
    # ─────────────────────────────────────────────────────────────────────────
    with col_input:
        st.markdown('<div class="ts-panel-card">', unsafe_allow_html=True)
        tab_form, tab_input_json = st.tabs(["Form", "JSON"])

        with tab_form:
            # Model & Mode Selector
            model_options = list(MODEL_CATALOG.keys())
            current_idx = model_options.index(current_model_id) if current_model_id in model_options else 0

            def _format_model_choice(m_id):
                meta = MODEL_CATALOG[m_id]
                icon = "🖼️" if meta["mode"] == "text-to-image" else "🎨"
                return f"{icon} {meta['label']}  •  [{meta['credits']} Credits]"

            selected_model = st.selectbox(
                "AI Model & Engine",
                options=model_options,
                index=current_idx,
                format_func=_format_model_choice,
                key="ts_model_selector_box",
                help="Select between state-of-the-art text generation or reference-based image editing engines.",
            )
            if selected_model != current_model_id:
                st.session_state["ts_selected_model"] = selected_model
                _handle_model_change()
                st.rerun()

            st.caption(f"ℹ️ {model_meta['description']}")

            # Viral Preset Chips
            st.markdown("<div style='margin-top: 12px; margin-bottom: 4px; font-weight: 600; font-size: 13px;'>🔥 Viral Hook Formula Presets</div>", unsafe_allow_html=True)
            preset_cols = st.columns(len(VIRAL_PRESET_TEMPLATES))
            for i, (pkey, pdata) in enumerate(VIRAL_PRESET_TEMPLATES.items()):
                with preset_cols[i]:
                    chip_label = pdata["name"].split("/")[0].strip()
                    if st.button(chip_label, key=f"ts_preset_btn_{pkey}", use_container_width=True):
                        base_txt = st.session_state.get("ts_prompt", "").strip() or model_meta["example_prompt"]
                        st.session_state["ts_prompt"] = enhance_viral_prompt(base_txt, pkey)
                        st.rerun()

            # Prompt Area with Character Counter & Enhance Button
            prompt_header_c1, prompt_header_c2 = st.columns([1, 1])
            with prompt_header_c1:
                st.markdown("**prompt \***")
            with prompt_header_c2:
                curr_len = len(st.session_state.get("ts_prompt", ""))
                st.markdown(f"<div style='text-align: right; color: gray; font-size: 12px;'>{curr_len} / 20000</div>", unsafe_allow_html=True)

            user_prompt = st.text_area(
                "prompt_hidden_label",
                value=st.session_state.get("ts_prompt", ""),
                height=110,
                key="ts_prompt_input_field",
                label_visibility="collapsed",
                placeholder="Describe your click-worthy YouTube thumbnail (subjects, lighting, facial expressions, background, text)...",
            )
            st.session_state["ts_prompt"] = user_prompt
            st.caption("Used to describe the generated images.")

            # Quick Action: Enhance Viral Prompt
            col_enh1, col_enh2 = st.columns([0.65, 0.35])
            with col_enh1:
                if st.button("✨ Enhance to Viral High-CTR Prompt", key="ts_enhance_prompt_btn", use_container_width=True):
                    enhanced = enhance_viral_prompt(user_prompt or model_meta["example_prompt"], "mrbeast")
                    st.session_state["ts_prompt"] = enhanced
                    st.toast("Prompt optimized with YouTube viral hook formula!", icon="🚀")
                    st.rerun()
            with col_enh2:
                if st.button("🔄 Load Model Example", key="ts_load_example_btn", use_container_width=True):
                    st.session_state["ts_prompt"] = model_meta["example_prompt"]
                    st.rerun()

            # Reference Images (For Image Edit / Image-to-Image models)
            if is_edit_mode:
                st.markdown("---")
                ref_h1, ref_h2 = st.columns([1, 1])
                with ref_h1:
                    st.markdown("**input_urls (Reference & Source Images) \***")
                with ref_h2:
                    if st.session_state["ts_reference_urls"]:
                        if st.button("Remove All", key="ts_remove_all_refs", type="secondary"):
                            st.session_state["ts_reference_urls"] = []
                            st.rerun()

                # Upload local files directly to cloud CDN
                uploaded_ref = st.file_uploader(
                    "Add Image for Reference (Up to 5 images)",
                    type=["png", "jpg", "jpeg", "webp"],
                    key="ts_ref_file_uploader",
                    help="Upload creator faces, product photos, or background scenes to edit.",
                )
                if uploaded_ref is not None:
                    # Check if already processed
                    if st.session_state.get("ts_last_uploaded_file_id") != uploaded_ref.file_id:
                        st.session_state["ts_last_uploaded_file_id"] = uploaded_ref.file_id
                        with st.spinner("Uploading image to cloud CDN..."):
                            ok_up, cdn_url = upload_image_to_cdn(uploaded_ref.getvalue(), uploaded_ref.name)
                            if ok_up:
                                if cdn_url not in st.session_state["ts_reference_urls"]:
                                    st.session_state["ts_reference_urls"].append(cdn_url)
                                st.toast("Reference image uploaded successfully!", icon="✅")
                            else:
                                st.error(f"Upload failed: {cdn_url}")

                # Display existing reference image chips
                if st.session_state["ts_reference_urls"]:
                    for idx, r_url in enumerate(st.session_state["ts_reference_urls"]):
                        chip_col1, chip_col2 = st.columns([0.85, 0.15], vertical_alignment="center")
                        with chip_col1:
                            st.image(r_url, caption=f"File {idx+1} (Source Reference)", width=220)
                        with chip_col2:
                            if st.button("Remove", key=f"ts_rm_ref_{idx}"):
                                st.session_state["ts_reference_urls"].pop(idx)
                                st.rerun()
                else:
                    # Provide default showcase reference image for instant testing
                    st.caption("No reference image uploaded yet. A default example scene will be used if left empty.")
                    if st.button("📎 Insert Showcase Reference Image", key="ts_insert_default_ref_btn"):
                        st.session_state["ts_reference_urls"].append(model_meta["showcase_url"])
                        st.rerun()

            st.markdown("---")

            # Aspect Ratio
            supported_aspects = model_meta.get("supported_aspect_ratios", ["16:9", "9:16", "1:1"])
            curr_aspect = st.session_state.get("ts_aspect_ratio", "16:9")
            aspect_idx = supported_aspects.index(curr_aspect) if curr_aspect in supported_aspects else 0

            selected_aspect = st.selectbox(
                "aspect_ratio",
                options=supported_aspects,
                index=aspect_idx,
                key="ts_aspect_ratio_box",
                help="16:9 is standard for YouTube horizontal thumbnails. 9:16 is for YouTube Shorts and Instagram Reels.",
            )
            st.session_state["ts_aspect_ratio"] = selected_aspect
            st.caption("The 21:16, 16:21, 9:8 and 8:9 aspect ratios support 1K only. 2K and 4K are available for other aspect ratios.")

            # Resolution (If model supports it)
            if model_meta.get("supported_resolutions"):
                res_opts = model_meta["supported_resolutions"]
                if len(res_opts) > 1:
                    st.markdown("**resolution**")
                    curr_res = st.session_state.get("ts_resolution", "2K")
                    res_cols = st.columns(len(res_opts))
                    for idx, r_val in enumerate(res_opts):
                        with res_cols[idx]:
                            is_active = (curr_res == r_val)
                            btn_type = "primary" if is_active else "secondary"
                            if st.button(r_val, key=f"ts_res_btn_{r_val}", type=btn_type, use_container_width=True):
                                st.session_state["ts_resolution"] = r_val
                                st.rerun()
                    st.caption("Image resolution output standard.")

            # Background Mode (If model supports it)
            if model_meta.get("supports_background"):
                st.markdown("**background**")
                bg_opts = ["transparent", "opaque", "auto"]
                curr_bg = st.session_state.get("ts_background", "auto")
                bg_cols = st.columns(3)
                for idx, bg_val in enumerate(bg_opts):
                    with bg_cols[idx]:
                        is_active = (curr_bg == bg_val)
                        btn_type = "primary" if is_active else "secondary"
                        if st.button(bg_val, key=f"ts_bg_btn_{bg_val}", type=btn_type, use_container_width=True):
                            st.session_state["ts_background"] = bg_val
                            st.rerun()
                st.caption("Image background output mode.")

            # Bottom Action Bar
            st.markdown("<div style='margin-top: 24px;'></div>", unsafe_allow_html=True)
            bot_col1, bot_col2 = st.columns([0.3, 0.7])
            with bot_col1:
                if st.button("Reset", key="ts_reset_form_btn", use_container_width=True):
                    st.session_state["ts_prompt"] = model_meta["example_prompt"]
                    st.session_state["ts_reference_urls"] = []
                    st.session_state["ts_aspect_ratio"] = "16:9"
                    st.session_state["ts_resolution"] = "2K"
                    st.rerun()

            with bot_col2:
                cost_credits = model_meta["credits"]
                run_btn = st.button(
                    f"⚡ Run / Generate  ({cost_credits} Credits)",
                    type="primary",
                    key="ts_run_generation_btn",
                    use_container_width=True,
                )

        with tab_input_json:
            # Power User JSON View
            debug_input = {
                "model": current_model_id,
                "input": {
                    "prompt": st.session_state.get("ts_prompt", ""),
                    "aspect_ratio": st.session_state.get("ts_aspect_ratio", "16:9"),
                },
            }
            if is_edit_mode:
                debug_input["input"][model_meta.get("image_key", "input_urls")] = st.session_state.get("ts_reference_urls", [])
            if model_meta.get("supports_background"):
                debug_input["input"]["resolution"] = st.session_state.get("ts_resolution", "2K")
                debug_input["input"]["background"] = st.session_state.get("ts_background", "auto")

            st.code(json.dumps(debug_input, indent=2), language="json")

        st.markdown("</div>", unsafe_allow_html=True)

    # ─────────────────────────────────────────────────────────────────────────
    # RIGHT PANEL: OUTPUT STAGE & SHOWCASE
    # ─────────────────────────────────────────────────────────────────────────
    with col_output:
        st.markdown('<div class="ts-panel-card">', unsafe_allow_html=True)
        tab_preview, tab_output_json = st.tabs(["Preview", "JSON"])

        # Execute Generation if Run button pressed
        if run_btn:
            active_prompt = st.session_state.get("ts_prompt", "").strip()
            if not active_prompt:
                st.error("Please enter a prompt describing your thumbnail.")
            else:
                progress_bar = st.progress(5, text="Submitting task to BangAI Neural Cluster...")
                with st.spinner(f"Generating high-resolution thumbnail with {model_meta['label']}..."):
                    def _update_prog(p, msg):
                        progress_bar.progress(p, text=msg)

                    refs = st.session_state.get("ts_reference_urls", [])
                    if is_edit_mode and not refs:
                        refs = [model_meta["showcase_url"]]

                    gen_res = generate_thumbnail(
                        model_id=current_model_id,
                        prompt=active_prompt,
                        aspect_ratio=st.session_state.get("ts_aspect_ratio", "16:9"),
                        resolution=st.session_state.get("ts_resolution", "2K"),
                        background=st.session_state.get("ts_background", "auto"),
                        reference_image_urls=refs,
                        progress_callback=_update_prog,
                    )

                if gen_res.get("success") and gen_res.get("result_urls"):
                    final_url = gen_res["result_urls"][0]
                    st.session_state["ts_current_output"] = {
                        "url": final_url,
                        "model": current_model_id,
                        "prompt": active_prompt,
                        "credits": gen_res.get("credits_consumed", cost_credits),
                        "cost_time": gen_res.get("cost_time", 0),
                        "task_id": gen_res.get("task_id", ""),
                        "timestamp": time.time(),
                    }
                    st.session_state["ts_raw_json_output"] = gen_res.get("raw_response", {})
                    # Record to history
                    st.session_state["ts_history"].insert(0, st.session_state["ts_current_output"])
                    st.toast("🎉 Thumbnail rendered successfully!", icon="🔥")
                    st.rerun()
                else:
                    err_msg = gen_res.get("error", "Unknown generation error.")
                    st.error(f"Generation error: {err_msg}")

        # Determine which image to display
        displayed_img_url = ""
        displayed_caption = ""
        is_live_render = False

        if st.session_state.get("ts_current_output"):
            out_data = st.session_state["ts_current_output"]
            displayed_img_url = out_data["url"]
            cost_s = out_data.get("cost_time", 0)
            c_val = out_data.get("credits", model_meta["credits"])
            displayed_caption = f"Generated by {model_meta['label']} in {cost_s}s • {c_val} Credits"
            is_live_render = True
        else:
            # Use pre-loaded showcase asset for the selected model
            showcase_path = model_meta.get("showcase_local")
            if showcase_path and os.path.exists(showcase_path):
                displayed_img_url = showcase_path
            else:
                displayed_img_url = model_meta.get("showcase_url", "")
            displayed_caption = f"✨ Showcase Output for {model_meta['label']} • {model_meta['credits']} Credits"

        with tab_preview:
            # Header info
            st.markdown(
                """
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                    <div><span style="color:gray; font-size:13px;">output type:</span> <span style="background:rgba(59,130,246,0.15); color:#3b82f6; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:600;">image</span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Image Display Container
            if displayed_img_url:
                st.image(
                    displayed_img_url,
                    caption=displayed_caption,
                    use_container_width=True,
                )

                # Download & Tool Actions
                act_col1, act_col2, act_col3 = st.columns([0.45, 0.28, 0.27])
                with act_col1:
                    # Provide direct download
                    try:
                        img_bytes = b""
                        if str(displayed_img_url).startswith("http"):
                            d_resp = requests.get(displayed_img_url, timeout=15)
                            if d_resp.status_code == 200:
                                img_bytes = d_resp.content
                        elif os.path.isfile(displayed_img_url):
                            with open(displayed_img_url, "rb") as f:
                                img_bytes = f.read()

                        if img_bytes:
                            st.download_button(
                                label="⬇️ Download High-Res",
                                data=img_bytes,
                                file_name="bangai_thumbnail_master.jpg",
                                mime="image/jpeg",
                                key="ts_download_thumb_btn",
                                use_container_width=True,
                                type="primary",
                            )
                    except Exception as e:
                        logger.warning(f"Failed to prepare download bytes: {e}")

                with act_col2:
                    if st.button("🖌️ Edit in Studio", key="ts_pipe_to_edit_btn", use_container_width=True, help="Switch to edit mode and use this image as reference."):
                        st.session_state["ts_reference_urls"] = [model_meta.get("showcase_url", "")]
                        if st.session_state.get("ts_current_output"):
                            st.session_state["ts_reference_urls"] = [st.session_state["ts_current_output"]["url"]]
                        st.session_state["ts_selected_model"] = "gpt-image-2-5-flare-image-to-image"
                        st.session_state["ts_prompt"] = "Add glowing neon laser eyes and cinematic YouTube clickbait explosion in background"
                        st.rerun()

                with act_col3:
                    if st.button("🎬 Set Video Cover", key="ts_set_video_cover_btn", use_container_width=True, help="Attach this thumbnail to one of your created videos."):
                        st.session_state["ts_show_cover_selector"] = True

                # Video cover assignment modal / expander
                if st.session_state.get("ts_show_cover_selector", False):
                    with st.expander("Attach as Cover to Task Video", expanded=True):
                        tasks_dir = utils.storage_dir("tasks")
                        available_tasks = []
                        if os.path.isdir(tasks_dir):
                            available_tasks = [d for d in os.listdir(tasks_dir) if os.path.isdir(os.path.join(tasks_dir, d))]

                        if available_tasks:
                            chosen_task = st.selectbox("Select Project Task", options=available_tasks, key="ts_task_cover_select")
                            if st.button("Save as Task Cover Image", key="ts_confirm_task_cover"):
                                try:
                                    t_dir = os.path.join(tasks_dir, chosen_task)
                                    target_cover = os.path.join(t_dir, "cover.jpg")
                                    if img_bytes:
                                        with open(target_cover, "wb") as cf:
                                            cf.write(img_bytes)
                                        st.success(f"Attached cover image to task {chosen_task}!")
                                        st.session_state["ts_show_cover_selector"] = False
                                except Exception as e:
                                    st.error(f"Failed to attach cover: {e}")
                        else:
                            st.info("No video rendering tasks found yet in storage.")

            # History Gallery Expander
            st.markdown("<div style='margin-top: 18px;'></div>", unsafe_allow_html=True)
            history_list = st.session_state.get("ts_history", [])
            with st.expander(f"🕒 Generation History ({len(history_list)} Thumbnails)", expanded=False):
                if history_list:
                    h_cols = st.columns(3)
                    for h_idx, h_item in enumerate(history_list):
                        col_target = h_cols[h_idx % 3]
                        with col_target:
                            st.image(h_item["url"], caption=f"{h_item['model'].split('/')[0]}", use_container_width=True)
                            if st.button(f"Reload #{h_idx+1}", key=f"ts_reload_h_{h_idx}"):
                                st.session_state["ts_current_output"] = h_item
                                st.session_state["ts_prompt"] = h_item["prompt"]
                                st.session_state["ts_selected_model"] = h_item["model"]
                                st.rerun()
                else:
                    st.caption("Your rendered thumbnails in this session will appear here.")

        with tab_output_json:
            # Raw Response JSON View
            if st.session_state.get("ts_raw_json_output"):
                st.code(json.dumps(st.session_state["ts_raw_json_output"], indent=2), language="json")
            else:
                st.code(
                    json.dumps(
                        {
                            "status": "ready",
                            "model": current_model_id,
                            "active_key_pool": "healthy",
                            "message": "Click 'Run / Generate' to render a live thumbnail.",
                        },
                        indent=2,
                    ),
                    language="json",
                )

        st.markdown("</div>", unsafe_allow_html=True)
