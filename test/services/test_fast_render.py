import os
import pytest
from app.services.fast_render import hex_to_ass_color, fast_restyle_task

def test_hex_to_ass_color():
    # #ddde0d -> &H000DDEDD
    assert hex_to_ass_color("#ddde0d") == "&H000DDEDD"
    assert hex_to_ass_color("#000000") == "&H00000000"
    assert hex_to_ass_color("#ffffff") == "&H00FFFFFF"
    assert hex_to_ass_color("invalid") == "&H000DDDDE"

def test_fast_restyle_task_missing_assets(tmp_path):
    ok, msg = fast_restyle_task(str(tmp_path))
    assert not ok
    assert "not found" in msg.lower()
