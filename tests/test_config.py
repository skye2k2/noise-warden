"""
Tests for noise_warden.config — YAML loading and validation.

Uses temp files for load testing and direct dict manipulation for
validation testing. No mocking needed.
"""
import os

import pytest
import yaml

from noise_warden.config import ConfigError, load_yaml, validate_config, save_yaml_text_validated


# ---------------------------------------------------------------------------
# validate_config — valid inputs
# ---------------------------------------------------------------------------

class TestValidateConfigValid:

    def test_base_cfg_passes(self, base_cfg):
        """The base fixture config should always pass validation."""
        validate_config(base_cfg)  # Should not raise

    def test_all_three_detection_modes(self, base_cfg):
        for mode in ["continuous", "intermittent", "continuous_music_focus"]:
            base_cfg["detection"]["mode"] = mode
            validate_config(base_cfg)  # Should not raise


# ---------------------------------------------------------------------------
# validate_config — invalid inputs
# ---------------------------------------------------------------------------

class TestValidateConfigInvalid:

    def test_missing_section_raises(self, base_cfg):
        del base_cfg["audio"]
        with pytest.raises(ConfigError, match="audio"):
            validate_config(base_cfg)

    def test_section_not_dict_raises(self, base_cfg):
        base_cfg["plugins"] = "not a dict"
        with pytest.raises(ConfigError, match="plugins"):
            validate_config(base_cfg)

    def test_zero_sample_rate_raises(self, base_cfg):
        base_cfg["audio"]["sample_rate"] = 0
        with pytest.raises(ConfigError, match="sample_rate"):
            validate_config(base_cfg)

    def test_negative_block_seconds_raises(self, base_cfg):
        base_cfg["audio"]["block_seconds"] = -1
        with pytest.raises(ConfigError, match="block_seconds"):
            validate_config(base_cfg)

    def test_invalid_night_start_hour(self, base_cfg):
        base_cfg["detection"]["night_start_hour"] = 25
        with pytest.raises(ConfigError, match="night_start_hour"):
            validate_config(base_cfg)

    def test_night_hour_not_int(self, base_cfg):
        base_cfg["detection"]["night_end_hour"] = 7.5
        with pytest.raises(ConfigError, match="night_end_hour"):
            validate_config(base_cfg)

    def test_invalid_detection_mode(self, base_cfg):
        base_cfg["detection"]["mode"] = "invalid_mode"
        with pytest.raises(ConfigError, match="mode"):
            validate_config(base_cfg)

    def test_enable_daytime_response_not_bool(self, base_cfg):
        base_cfg["response"]["enable_daytime_response"] = "yes"
        with pytest.raises(ConfigError, match="enable_daytime_response"):
            validate_config(base_cfg)


# ---------------------------------------------------------------------------
# load_yaml
# ---------------------------------------------------------------------------

class TestLoadYaml:

    def test_loads_valid_yaml_file(self, base_cfg, tmp_path):
        cfg_path = str(tmp_path / "test_config.yaml")
        with open(cfg_path, "w") as f:
            yaml.dump(base_cfg, f)
        loaded = load_yaml(cfg_path)
        assert loaded["app"]["port"] == 8787
        assert loaded["detection"]["mode"] == "continuous_music_focus"

    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_yaml("/tmp/definitely_does_not_exist_xyz.yaml")

    def test_invalid_yaml_content_raises(self, tmp_path):
        cfg_path = str(tmp_path / "bad.yaml")
        with open(cfg_path, "w") as f:
            f.write("just a string, not a mapping")
        with pytest.raises(ConfigError, match="Top-level"):
            load_yaml(cfg_path)


# ---------------------------------------------------------------------------
# save_yaml_text_validated
# ---------------------------------------------------------------------------

class TestSaveYamlTextValidated:

    def test_saves_valid_yaml(self, base_cfg, tmp_path):
        cfg_path = str(tmp_path / "save_test.yaml")
        raw = yaml.dump(base_cfg)
        save_yaml_text_validated(cfg_path, raw)
        # File should exist and be loadable
        assert os.path.exists(cfg_path)
        loaded = load_yaml(cfg_path)
        assert loaded["app"]["port"] == 8787

    def test_rejects_invalid_yaml(self, base_cfg, tmp_path):
        cfg_path = str(tmp_path / "reject_test.yaml")
        base_cfg["detection"]["mode"] = "bogus"
        raw = yaml.dump(base_cfg)
        with pytest.raises(ConfigError):
            save_yaml_text_validated(cfg_path, raw)
        # File should NOT have been written
        assert not os.path.exists(cfg_path)

    def test_rejects_non_mapping(self, tmp_path):
        cfg_path = str(tmp_path / "nonmap.yaml")
        with pytest.raises(ConfigError, match="mapping"):
            save_yaml_text_validated(cfg_path, "- a list\n- not a mapping\n")
