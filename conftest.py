def pytest_configure(config):
    config.addinivalue_line("markers", "network: hits real YouTube; skip with -m 'not network'")
    config.addinivalue_line("markers", "slow: downloads video via yt-dlp; skip with -m 'not slow'")
