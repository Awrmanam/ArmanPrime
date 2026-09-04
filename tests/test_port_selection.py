import socket
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

spec = spec_from_file_location("select_port", Path("scripts/select_port.py"))
module = module_from_spec(spec)
spec.loader.exec_module(module)
select_port = module.select_port


def test_select_port_keeps_free_preference():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert select_port(port) == port


def test_select_port_skips_an_occupied_port_without_stopping_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = listener.getsockname()[1]
        selected = select_port(occupied)
        assert selected > occupied
        assert listener.getsockname()[1] == occupied


@pytest.mark.parametrize("port", (0, 1023, 65536))
def test_select_port_rejects_unsafe_values(port):
    with pytest.raises(ValueError, match="between 1024 and 65535"):
        select_port(port)
