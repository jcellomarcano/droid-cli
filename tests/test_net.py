"""droid.net contra capturas reales anonimizadas: totales por uid, sockets, interfaces, decodificacion hex."""
from tests.helpers import fixture_text

from droid import net


def test_uid_of_reads_appid_from_dumpsys(fake_shell):
    fake_shell(fixture_text("dumpsys_package_uid"))
    assert net.uid_of("S", "com.example.app") == 10093


def test_uid_of_returns_none_when_not_found(fake_shell):
    fake_shell("")
    assert net.uid_of("S", "com.example.app") is None


def test_uid_totals_sums_rx_tx_for_uid(fake_shell):
    fake_shell(fixture_text("netstats_detail"))
    totals = net.uid_totals("S", 10082)
    assert totals["rx"] == 7758828
    assert totals["tx"] == 10409706
    assert totals["by_type"] == {"MOBILE": (7758828, 10409706)}


def test_uid_totals_empty_for_unknown_uid(fake_shell):
    fake_shell(fixture_text("netstats_detail"))
    totals = net.uid_totals("S", 999999)
    assert totals == {"rx": 0, "tx": 0, "rx_pkts": 0, "tx_pkts": 0, "by_type": {}}


def test_iface_counters_parses_proc_net_dev(fake_shell):
    fake_shell(fixture_text("proc_net_dev"))
    counters = net.iface_counters("S")
    assert counters["eth0"] == (137646336, 9997970)
    assert "lo" not in counters


def test_iface_counters_garbage_input_returns_empty(fake_shell):
    fake_shell("not /proc/net/dev output\nat all")
    assert net.iface_counters("S") == {}


def test_sockets_for_uid_decodes_and_filters(fake_shell):
    fake_shell(fixture_text("proc_net_sockets"))
    socks = net.sockets_for_uid("S", 10082)
    assert len(socks) == 3
    tcp = next(s for s in socks if s.proto == "tcp")
    assert tcp.remote == "203.0.113.56:443"
    assert tcp.local == "10.0.2.15:42568"
    tcp6 = next(s for s in socks if s.proto == "tcp6" and s.state == "ESTAB")
    assert tcp6.remote == "203.0.113.212:5228"


def test_sockets_for_uid_none_matches_returns_empty(fake_shell):
    fake_shell(fixture_text("proc_net_sockets"))
    assert net.sockets_for_uid("S", 424242) == []


def test_hex_addr_decodes_ipv4():
    assert net._hex_addr("0F02000A:A648") == "10.0.2.15:42568"


def test_hex_addr_decodes_v4_mapped_v6():
    assert net._hex_addr("0000000000000000FFFF0000017100CB:146C") == "203.0.113.1:5228"


def test_hex_addr_falls_back_on_garbage_length():
    assert net._hex_addr("ZZ:80") == "ZZ:128"


def test_hex_addr_raises_on_non_hex_port():
    import pytest

    with pytest.raises(ValueError):
        net._hex_addr("0F02000A:ZZZZ")
