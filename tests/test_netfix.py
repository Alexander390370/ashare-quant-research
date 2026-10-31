"""netfix 与本机环境适配的回归测试。

对应 tradingagents/netfix.py 里的两处修复：
1. 网络：YF_DISABLE_CURL_CFFI 必须在 yfinance 导入前设好。
2. 文件系统：运行数据目录重定向到工作区，避开家目录/AppData 的写入限制。
"""

import os
from pathlib import Path

import pytest

from tradingagents import netfix


@pytest.mark.unit
class TestNetworkFix:
    def test_curl_cffi_is_disabled(self):
        """curl_cffi 的浏览器指纹在本网络被 Yahoo 判为可疑，必须关掉。"""
        netfix.apply_network_fixes()
        assert os.environ.get("YF_DISABLE_CURL_CFFI") == "1"

    def test_apply_is_idempotent(self):
        first = netfix.apply_network_fixes()
        second = netfix.apply_network_fixes()
        assert first == second

    def test_respects_user_value_unless_forced(self):
        os.environ["YF_DISABLE_CURL_CFFI"] = "0"
        try:
            netfix.apply_network_fixes()
            assert os.environ["YF_DISABLE_CURL_CFFI"] == "0", "默认不覆盖用户显式设置"
            netfix.apply_network_fixes(force=True)
            assert os.environ["YF_DISABLE_CURL_CFFI"] == "1", "force=True 时覆盖"
        finally:
            os.environ["YF_DISABLE_CURL_CFFI"] = "1"


@pytest.mark.unit
class TestProxy:
    def test_detect_proxy_returns_none_or_valid_url(self):
        """探测结果是 None 或一个 http:// 开头的 URL，不能是别的。"""
        proxy = netfix.detect_proxy()
        assert proxy is None or proxy.startswith("http://")

    def test_apply_proxy_sets_env_when_found(self, monkeypatch):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.setattr(netfix, "detect_proxy", lambda: "http://127.0.0.1:9999")
        result = netfix.apply_proxy(force=True)
        assert result["proxy"] == "http://127.0.0.1:9999"
        assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:9999"
        # 本地地址不应走代理
        assert "127.0.0.1" in os.environ.get("NO_PROXY", "")

    def test_apply_proxy_keeps_existing(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
        result = netfix.apply_proxy()
        assert "沿用已有" in result["proxy"]

    def test_apply_proxy_handles_no_proxy_found(self, monkeypatch):
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.setattr(netfix, "detect_proxy", lambda: None)
        result = netfix.apply_proxy(force=True)
        assert "未检测到" in result["proxy"]


@pytest.mark.unit
class TestDNSErrorPatch:
    def test_dnserror_exists_after_patch(self):
        """yfinance 的降级分支引用 requests.exceptions.DNSError。"""
        import requests.exceptions as rex

        netfix.patch_requests_dnserror()
        assert hasattr(rex, "DNSError")

    def test_dnserror_inherits_connection_error(self):
        """层级要与 curl_cffi 一致：DNSError → ConnectionError → OSError。"""
        import requests.exceptions as rex

        netfix.patch_requests_dnserror()
        assert issubclass(rex.DNSError, rex.ConnectionError)
        assert issubclass(rex.DNSError, OSError)

    def test_patch_is_idempotent(self):
        netfix.patch_requests_dnserror()
        assert netfix.patch_requests_dnserror() is False


@pytest.mark.unit
class TestDataDirFix:
    def test_all_three_dirs_are_redirected_into_project(self):
        result = netfix.apply_data_dir_fixes(force=True)
        project_root = Path(__file__).resolve().parent.parent
        for key, value in result.items():
            assert Path(value).is_relative_to(project_root), (
                f"{key} 应落在项目目录内，实际 {value}"
            )

    def test_dirs_are_created(self):
        result = netfix.apply_data_dir_fixes(force=True)
        for key, value in result.items():
            path = Path(value)
            target = path.parent if path.suffix else path
            assert target.exists(), f"{key} 的目录 {target} 应已创建"

    def test_respects_user_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom_cache"
        monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(custom))
        result = netfix.apply_data_dir_fixes()
        assert result["TRADINGAGENTS_CACHE_DIR"] == str(custom)
        assert custom.exists()

    def test_describe_mentions_both_fixes(self):
        netfix.apply_network_fixes()
        netfix.apply_data_dir_fixes()
        text = netfix.describe()
        assert "YF_DISABLE_CURL_CFFI" in text
        assert "数据目录" in text
