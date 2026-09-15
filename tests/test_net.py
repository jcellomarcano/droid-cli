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
    assert totals == {"rx": 0, "tx": 0, "rx_pkts": 0, "tx_pkts": 0, "by_type": {}, "source": "detail"}


def test_parse_netstats_totals_on_detail_dump():
    totals = net.parse_netstats_totals(fixture_text("netstats_detail"), 10082)
    assert totals["rx"] == 7758828
    assert totals["tx"] == 10409706
    assert totals["by_type"] == {"MOBILE": (7758828, 10409706)}


def test_parse_netstats_totals_on_uid_10001_dump_has_no_st_lines():
    # netstats_uid_10001.txt es una captura real de `dumpsys netstats --uid 10001` en Android 17: el
    # dispositivo no filtro nada y ademas la captura fue truncada antes de llegar a las lineas st=, asi
    # que para cualquier uid el resultado es todo ceros.
    totals = net.parse_netstats_totals(fixture_text("netstats_uid_10001"), 10001)
    assert totals == {"rx": 0, "tx": 0, "rx_pkts": 0, "tx_pkts": 0, "by_type": {}}


def test_uid_totals_tries_uid_form_first_then_falls_back_to_detail_on_full_dump(fake_shell):
    commands = []

    def respond(cmd):
        commands.append(cmd)
        return fixture_text("netstats_detail")

    fake_shell(respond)
    totals = net.uid_totals("S", 10082)
    # netstats_detail.txt trae bloques ident=[...] de muchos uids ademas del 10082, asi que la primera
    # respuesta ("--uid") se detecta como volcado completo y dispara un segundo shell pidiendo "detail".
    assert commands[0].startswith("dumpsys netstats --uid 10082")
    assert any("detail" in c for c in commands[1:])
    assert totals["source"] == "detail"
    assert totals["rx"] == 7758828


def test_uid_totals_does_not_call_detail_when_uid_trial_already_has_only_the_target_uid(fake_shell):
    commands = []
    filtered = "  ident=[{type=1}] uid=10082 set=ALL tag=0x0\n      st=1 rb=10 rp=1 tb=20 tp=2 op=0\n"

    def respond(cmd):
        commands.append(cmd)
        if "detail" in cmd:
            raise AssertionError("no deberia llamar a detail cuando --uid ya filtro")
        return filtered

    fake_shell(respond)
    net.uid_totals("S", 10082)
    assert len(commands) == 1


def test_uid_totals_uses_uid_form_when_it_really_filtered(fake_shell):
    filtered = "  ident=[{type=1}] uid=10082 set=ALL tag=0x0\n      st=1 rb=10 rp=1 tb=20 tp=2 op=0\n"
    fake_shell(filtered)
    totals = net.uid_totals("S", 10082)
    assert totals["source"] == "uid"
    assert totals["rx"] == 10
    assert totals["tx"] == 20


def test_uid_totals_falls_back_to_detail_when_uid_trial_is_garbage(fake_shell):
    commands = []

    def respond(cmd):
        commands.append(cmd)
        if "--uid" in cmd:
            return "not netstats output at all\njust garbage"
        return fixture_text("netstats_detail")

    fake_shell(respond)
    totals = net.uid_totals("S", 10082)
    assert any("detail" in c for c in commands)
    assert totals["source"] == "detail"
    assert totals["rx"] == 7758828


def test_uid_totals_filter_works_true_uses_only_uid_call(fake_shell):
    commands = []
    filtered = "  ident=[{type=1}] uid=10082 set=ALL tag=0x0\n      st=1 rb=10 rp=1 tb=20 tp=2 op=0\n"

    def respond(cmd):
        commands.append(cmd)
        if "detail" in cmd:
            raise AssertionError("no deberia llamar a detail cuando ya se sabe que --uid filtra")
        return filtered

    fake_shell(respond)
    totals = net.uid_totals("S", 10082, filter_works=True)
    assert len(commands) == 1
    assert totals["source"] == "uid"


def test_uid_totals_filter_works_false_uses_only_detail_call(fake_shell):
    commands = []

    def respond(cmd):
        commands.append(cmd)
        if "--uid" in cmd:
            raise AssertionError("no deberia repetir el trial de --uid cuando ya se sabe que no filtra")
        return fixture_text("netstats_detail")

    fake_shell(respond)
    totals = net.uid_totals("S", 10082, filter_works=False)
    assert len(commands) == 1
    assert totals["source"] == "detail"


def test_net_monitor_learns_uid_filter_across_cycles(fake_shell):
    """F346-C8: en el primer ciclo (filter_works aun None) uid_totals paga el trial de --uid y,
    como en este dispositivo no filtra, cae a detail (dos llamadas dumpsys); a partir del segundo
    ciclo NetMonitor ya sabe que --uid no filtra y llama a detail directamente (una sola llamada)."""
    from types import SimpleNamespace

    from droid.net import NetMonitor

    dev = SimpleNamespace(serial="S", key="s1", name="Emu")
    mon = NetMonitor(dev, "com.example.app", interval=1.0, totals_every=1.0)
    mon.uid = 10082

    calls = []

    def respond(cmd):
        calls.append(cmd)
        if "for f in tcp" in cmd:
            return ""  # sockets_for_uid: sin sockets
        return fixture_text("netstats_detail")  # tanto --uid como detail devuelven el volcado completo

    fake_shell(respond)
    mon._refresh_totals()
    assert len(calls) == 3  # sockets_for_uid + trial --uid + detail
    assert mon._uid_filter_works is False
    assert mon.totals["source"] == "detail"

    calls.clear()
    mon._refresh_totals()
    assert len(calls) == 2  # sockets_for_uid + una sola llamada dumpsys (detail, sin repetir el trial)
    dumpsys_calls = [c for c in calls if "dumpsys netstats" in c]
    assert len(dumpsys_calls) == 1
    assert "detail" in dumpsys_calls[0]


def test_probe_qtaguid_true_when_readable(fake_shell):
    fake_shell("yes")
    assert net.probe_qtaguid("S") is True


def test_probe_qtaguid_false_when_missing(fake_shell):
    fake_shell("")
    assert net.probe_qtaguid("S") is False


def test_qtaguid_totals_sums_untagged_rows_for_uid(fake_shell):
    fake_shell(fixture_text("xt_qtaguid_stats"))
    totals = net.qtaguid_totals("S", 10082)
    assert totals == {"rx": 2000000, "tx": 1250000}


def test_qtaguid_totals_ignores_tagged_and_other_uid_rows(fake_shell):
    fake_shell(fixture_text("xt_qtaguid_stats"))
    totals = net.qtaguid_totals("S", 10093)
    assert totals == {"rx": 50000, "tx": 30000}


def test_qtaguid_totals_none_without_the_proc_file(fake_shell):
    fake_shell("")
    assert net.qtaguid_totals("S", 10082) is None


def _sock(proto, state, local, remote, uid=10082):
    return net.Socket(proto, state, local, remote, uid)


def test_host_table_aggregates_by_remote_ip_port():
    class FakeDns:
        def lookup(self, ip):
            return {"203.0.113.56": "api.example.com"}.get(ip)

    socks = [
        _sock("tcp", "ESTAB", "10.0.2.15:1", "203.0.113.56:443"),
        _sock("tcp", "ESTAB", "10.0.2.15:2", "203.0.113.56:443"),
        _sock("tcp", "TIME_WAIT", "10.0.2.15:3", "203.0.113.56:443"),
        _sock("udp", "UDP", "10.0.2.15:4", "203.0.113.212:53"),
    ]
    table = net.host_table(socks, FakeDns(), now=1000.0)
    assert len(table) == 2
    top = table[0]
    assert top["ip"] == "203.0.113.56"
    assert top["host"] == "api.example.com"
    assert top["connections"] == 3
    assert top["states"] == {"ESTAB": 2, "TIME_WAIT": 1}
    assert top["first_seen"] == 1000.0
    assert top["last_seen"] == 1000.0
    other = table[1]
    assert other["ip"] == "203.0.113.212"
    assert other["host"] is None


def test_connection_events_detects_new_and_closed():
    prev = [_sock("tcp", "ESTAB", "10.0.2.15:1", "203.0.113.56:443"),
            _sock("tcp", "ESTAB", "10.0.2.15:2", "203.0.113.99:80")]
    cur = [_sock("tcp", "ESTAB", "10.0.2.15:1", "203.0.113.56:443"),
           _sock("tcp", "ESTAB", "10.0.2.15:3", "203.0.113.5:22")]
    events = net.connection_events(prev, cur, now=42.0)
    kinds = {(e["kind"], e["remote"]) for e in events}
    assert kinds == {("new", "203.0.113.5:22"), ("closed", "203.0.113.99:80")}
    for e in events:
        assert e["t"] == 42.0
        assert e["proto"] == "tcp"


def test_reverse_dns_success_failure_and_cache(monkeypatch):
    calls = []

    def fake_gethostbyaddr(ip):
        calls.append(ip)
        if ip == "203.0.113.1":
            return ("host1.example.com", [], [ip])
        raise OSError("no address associated")

    monkeypatch.setattr(net.socketmod, "gethostbyaddr", fake_gethostbyaddr)
    dns = net.ReverseDns()
    assert dns.lookup("203.0.113.1") == "host1.example.com"
    assert dns.lookup("203.0.113.1") == "host1.example.com"
    assert calls == ["203.0.113.1"]  # segunda llamada vino de cache, no del socket real

    assert dns.lookup("203.0.113.2") is None
    assert dns.lookup("203.0.113.2") is None
    assert calls == ["203.0.113.1", "203.0.113.2"]  # el fallo tambien se cachea


def test_reverse_dns_shutdown_stops_executor():
    dns = net.ReverseDns()
    dns.shutdown()
    assert dns._executor._shutdown is True


def test_net_monitor_stop_shuts_down_dns():
    from types import SimpleNamespace

    from droid.net import NetMonitor

    dev = SimpleNamespace(serial="S", key="s1", name="Emu")
    mon = NetMonitor(dev, "com.example.app", interval=1.0)
    mon.stop()
    assert mon.dns._executor._shutdown is True


def test_net_monitor_first_refresh_reports_no_events(fake_shell):
    """F346-C8c: el primer refresco de sockets establece la linea base sin reportar altas ficticias
    por conexiones que ya existian antes de que empezara la sesion."""
    from types import SimpleNamespace

    from droid.net import NetMonitor

    dev = SimpleNamespace(serial="S", key="s1", name="Emu")
    mon = NetMonitor(dev, "com.example.app", interval=1.0, totals_every=1.0)
    mon.uid = 10082

    def respond(cmd):
        if "for f in tcp" in cmd:
            return fixture_text("proc_net_sockets")
        return fixture_text("netstats_detail")

    fake_shell(respond)
    mon._refresh_totals()
    assert len(mon.sockets) == 3  # los sockets del uid 10082 en la fixture (ver test_sockets_for_uid_decodes_and_filters)
    assert len(mon.events) == 0  # linea base: nada reportado como "new" todavia

    mon._refresh_totals()
    assert len(mon.events) == 0  # el mismo socket sigue ahi, sin cambios


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
