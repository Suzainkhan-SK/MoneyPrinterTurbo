"""
Unit tests for app.services.thumbnail_service
"""

import pytest
from app.services.thumbnail_service import (
    KeyPoolManager,
    MODEL_CATALOG,
    VIRAL_PRESET_TEMPLATES,
    enhance_viral_prompt,
)


def test_key_pool_rotation_and_exhaustion():
    keys = ["key_1", "key_2", "key_3"]
    pool = KeyPoolManager(keys)

    # 1. Round-robin rotation
    k1 = pool.get_active_key()
    k2 = pool.get_active_key()
    assert k1 == "key_1"
    assert k2 == "key_2"

    # 2. Mark key_2 exhausted
    pool.mark_exhausted("key_2", "quota exceeded")
    active = [pool.get_active_key() for _ in range(4)]
    assert "key_2" not in active
    assert set(active) == {"key_1", "key_3"}

    # 3. Status report
    stats = pool.get_pool_status()
    assert len(stats) == 3
    assert stats[1]["status"] == "Exhausted"
    assert stats[0]["status"] == "Healthy"


def test_enhance_viral_prompt():
    prompt = "man discovers lost treasure"
    enhanced = enhance_viral_prompt(prompt, "mrbeast")
    assert "extreme wide angle" in enhanced
    assert "shocked facial expression" in enhanced
    assert enhanced.startswith("man discovers lost treasure")

    # Crime preset
    enhanced_crime = enhance_viral_prompt(prompt, "crime")
    assert "cinematic noir" in enhanced_crime

    # Wealth preset
    enhanced_wealth = enhance_viral_prompt(prompt, "wealth")
    assert "hundred dollar cash" in enhanced_wealth


def test_model_catalog_integrity():
    required_keys = ["label", "mode", "credits", "supported_aspect_ratios", "example_prompt"]
    assert len(MODEL_CATALOG) >= 5

    for model_id, meta in MODEL_CATALOG.items():
        for rk in required_keys:
            assert rk in meta, f"Model {model_id} missing {rk}"
        assert meta["credits"] in (4, 6)
        assert meta["mode"] in ("text-to-image", "image-edit")
