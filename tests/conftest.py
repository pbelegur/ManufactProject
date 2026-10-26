import pytest


def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true", help="Test running MCP HTTP services")
    parser.addoption("--run-compose-restart", action="store_true", help="Also restart the stateful Compose service")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: requires running MCP services")
    config.addinivalue_line("markers", "compose_restart: restarts the stateful Compose container")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "integration" in item.keywords and not config.getoption("--run-integration"):
            item.add_marker(pytest.mark.skip(reason="requires --run-integration"))
        if "compose_restart" in item.keywords and not config.getoption("--run-compose-restart"):
            item.add_marker(pytest.mark.skip(reason="requires --run-compose-restart"))
