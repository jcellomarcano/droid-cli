"""tests/anonymize.py: determinismo y reglas de anonimizacion sobre texto sintetico."""
import pytest

from droid.net import _hex_addr
from tests import anonymize as anon


def test_anonymize_all_is_byte_deterministic_when_raw_present():
    if not anon.RAW_DIR.exists():
        pytest.skip("tests/fixtures/raw ausente")
    out1, _ip1, _pkg1 = anon.anonymize_all()
    out2, _ip2, _pkg2 = anon.anonymize_all()
    assert out1 == out2


def test_fixed_package_mapping_applies():
    text = "package: com.example.app installed, uses com.otherapp.wear.complication.Worker"
    out = anon.anonymize_text(text, {}, {})
    assert "com.example.app" in out
    # El token completo (hasta donde matchea PKG_RE) se reemplaza entero, no un
    # str.replace parcial del prefijo: la cola en mayuscula ".Worker" no forma
    # parte del token de paquete y se conserva tal cual.
    assert "com.example.other.Worker" in out
    assert "com.example.other.wear.complication.Worker" not in out
    assert "example" not in out
    assert "otherapp" not in out


def test_android_and_google_packages_are_kept():
    text = "com.android.systemui and com.google.android.gms stay put"
    out = anon.anonymize_text(text, {}, {})
    assert out == text


def test_generic_reversed_domain_package_is_anonymized():
    text = "installed io.sentry.android.core and com.acme.tracker.Worker"
    other_map = anon.build_other_pkg_mapping([text])
    out = anon.anonymize_text(text, {}, other_map)
    assert "io.sentry" not in out
    assert "com.acme" not in out
    assert "com.example.app_" in out
    assert not anon.find_violations(out)


def test_generic_reversed_domain_package_negative_control_allowlisted():
    text = "org.chromium.base.Worker and org.apache.commons.lang3.StringUtils and java.util.List"
    other_map = anon.build_other_pkg_mapping([text])
    assert other_map == {}
    out = anon.anonymize_text(text, {}, {})
    assert out == text


def test_email_with_any_tld_is_replaced():
    out = anon.anonymize_text("contact me at jesus@example.synthetic please", {}, {})
    assert out == "contact me at user@example.com please"


def test_email_negative_control_without_at_sign_is_untouched():
    text = "no email token here at all"
    assert anon.anonymize_text(text, {}, {}) == text


def test_home_database_path_is_stripped():
    out = anon.anonymize_text("database: ~/some/secret/path.db opened", {}, {})
    assert out == "database: ~ opened"


def test_ip_encode_decode_roundtrip_v4():
    hexed = anon._encode_v4_hex("203.0.113.9")
    assert anon._decode_hex_addr_ip(hexed) == "203.0.113.9"
    assert _hex_addr(f"{hexed}:1F90") == "203.0.113.9:8080"


def test_ip_encode_decode_roundtrip_v6_mapped():
    hexed = anon._encode_v6mapped_hex("203.0.113.9")
    assert anon._decode_hex_addr_ip(hexed) == "203.0.113.9"


def test_ip_allowed_ranges():
    assert anon._ip_allowed("10.0.2.200") is True
    assert anon._ip_allowed("127.5.5.5") is True
    assert anon._ip_allowed("0.0.0.0") is True
    assert anon._ip_allowed("255.255.255.255") is True
    assert anon._ip_allowed("224.0.0.1") is True
    assert anon._ip_allowed("172.217.116.4") is False
    assert anon._ip_allowed("10.0.3.1") is False


def test_ip_allowed_rejects_out_of_range_octets():
    assert anon._ip_allowed("999.1.1.1") is True
    assert "999.1.1.1" not in anon.build_ip_mapping(["reading 999.1.1.1 here"])


def test_ipv4_adjacent_to_letters_or_hyphens_is_left_alone():
    text = "build lib1.2.3.4-final and version-1.2.3.4x should stay"
    out = anon.anonymize_text(text, anon.build_ip_mapping([text]), {})
    assert out == text


def test_ipv4_version_context_is_left_alone_even_when_delimited():
    # N2/N5: a diferencia del caso "adjacente" de arriba, aqui el numero SI
    # esta delimitado por espacios (matchea IPV4_RE), pero el contexto de 12
    # caracteres hacia atras lo identifica como numero de version, no como IP.
    text = "versionName=1.2.3.4 and libfoo 1.2.3.4 should stay untouched"
    ip_map = anon.build_ip_mapping([text])
    assert ip_map == {}
    out = anon.anonymize_text(text, ip_map, {})
    assert out == text
    assert anon.find_violations(text) == []


def test_ipv4_connected_prefix_is_still_rewritten():
    text = "connected 8.8.8.8 ok"
    ip_map = anon.build_ip_mapping([text])
    assert "8.8.8.8" in ip_map
    out = anon.anonymize_text(text, ip_map, {})
    assert "8.8.8.8" not in out
    assert ("ipv4", "8.8.8.8") in anon.find_violations(text)


def test_ipv4_not_adjacent_is_anonymized():
    text = "server at 172.217.116.4 responded"
    ip_map = anon.build_ip_mapping([text])
    out = anon.anonymize_text(text, ip_map, {})
    assert "172.217.116.4" not in out
    assert "203.0.113." in out


def test_build_ip_mapping_is_stable_hash_derived_assignment():
    mapping = anon.build_ip_mapping(["seen at 8.8.8.8 and 9.9.9.9 and 8.8.8.8 again"])
    expected = {
        ip: f"203.0.113.{1 + (anon._stable_int(ip, 4) % 250)}"
        for ip in ("8.8.8.8", "9.9.9.9")
    }
    assert mapping == expected


def test_ip_mapping_is_order_independent_across_corpus():
    # N6: el placeholder de un valor depende solo de ese valor (hash estable),
    # no de que otros ficheros/valores esten presentes en el corpus.
    alone = anon.build_ip_mapping(["only 172.217.116.4 here"])
    with_more = anon.build_ip_mapping([
        "only 172.217.116.4 here",
        "also 1.1.1.1 and 2.2.2.2 and 3.3.3.3 and 4.4.4.4 in a bigger corpus",
    ])
    assert alone["172.217.116.4"] == with_more["172.217.116.4"]


def test_native_ipv6_text_is_anonymized_stably():
    text = "connected to 2607:f8b0:4005:80a::200e twice: 2607:f8b0:4005:80a::200e"
    ip6_map = anon.build_ip6_mapping([text])
    out = anon.anonymize_text(text, {}, {}, ip6_map=ip6_map)
    assert "2607:f8b0" not in out
    assert out.count(ip6_map["2607:f8b0:4005:80a::200e"]) == 2
    assert ip6_map["2607:f8b0:4005:80a::200e"].startswith("2001:db8::")


def test_native_ipv6_negative_control_loopback_and_link_local_untouched():
    text = "loopback ::1 and link-local fe80::1 and unmapped ::ffff:10.0.2.5"
    ip6_map = anon.build_ip6_mapping([text])
    assert ip6_map == {}
    out = anon.anonymize_text(text, {}, {}, ip6_map=ip6_map)
    assert out == text


def test_ipv6_hex_proc_net_form_round_trips_through_droid_net_hex_addr():
    ip6 = "2001:db8::4"
    hexed = anon._encode_v6_hex(ip6)
    assert len(hexed) == 32
    decoded = _hex_addr(f"{hexed}:1F90")
    assert decoded == "[2001:0db8:0000:0000:0000:0000:0000:0004]:8080"
    assert anon._decode_hex_addr_ip(hexed) == ip6


def test_net_line_local_column_global_ipv6_is_violation_and_gets_anonymized():
    # N1: la columna LOCAL (no solo la remota) de una linea /proc/net/tcp6
    # tambien debe detectarse y anonimizarse.
    local_hex = anon._encode_v6_hex("2607:f8b0:4005:80a::200e")
    remote_hex = anon._encode_v6_hex("::1")  # loopback: excluido, no es violacion
    line = f"   1: {local_hex}:1F90 {remote_hex}:0050 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 20 0 0 10 0"

    before = anon.find_violations(line)
    assert ("ipv6_hex", f"{local_hex}:1F90") in before

    ip6_map = anon.build_ip6_mapping([line])
    assert "2607:f8b0:4005:80a::200e" in ip6_map
    out = anon.anonymize_text(line, {}, {}, ip6_map=ip6_map)
    assert local_hex not in out
    assert anon.find_violations(out) == []

    new_local_hex = out.split()[1].split(":")[0]
    decoded = _hex_addr(f"{new_local_hex}:1F90")
    assert decoded.startswith("[2001:0db8:")


def test_net_line_local_column_private_ipv4_hex_is_violation_and_gets_anonymized():
    # N1: columna LOCAL con una IPv4 privada (fuera de los rangos permitidos)
    # en forma hex tambien debe detectarse y anonimizarse.
    local_hex = anon._encode_v4_hex("10.0.3.1")  # 10.0.3.0/24: NO esta en ALLOWED_V4_NETS
    remote_hex = anon._encode_v4_hex("203.0.113.5")  # ya anonimizado: excluido
    line = f"   2: {local_hex}:0050 {remote_hex}:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 12346 1 0000000000000000 20 0 0 10 0"

    before = anon.find_violations(line)
    assert ("ipv4_hex", f"{local_hex}:0050") in before

    ip_map = anon.build_ip_mapping([line])
    assert "10.0.3.1" in ip_map
    out = anon.anonymize_text(line, ip_map, {})
    assert local_hex not in out
    assert anon.find_violations(out) == []

    new_local_hex = out.split()[1].split(":")[0]
    decoded = _hex_addr(f"{new_local_hex}:0050")
    assert decoded.startswith("203.0.113.")


def test_mac_address_is_anonymized_stably():
    text = "sta mac 3a:4b:5c:6d:7e:8f seen twice 3a:4b:5c:6d:7e:8f"
    mac_map = anon.build_mac_mapping([text])
    expected = mac_map["3a:4b:5c:6d:7e:8f"]
    out = anon.anonymize_text(text, {}, {}, mac_map=mac_map)
    assert "3a:4b:5c:6d:7e:8f" not in out
    assert out.count(expected) == 2
    assert expected.startswith("02:00:00:")


def test_mac_address_negative_control_already_locally_administered():
    text = "iface 02:00:00:00:00:05 configured"
    mac_map = anon.build_mac_mapping([text])
    out = anon.anonymize_text(text, {}, {}, mac_map=mac_map)
    assert out == text


def test_mac_address_negative_control_all_zero_is_untouched():
    text = "bssid 00:00:00:00:00:00 (none)"
    mac_map = anon.build_mac_mapping([text])
    assert mac_map == {}
    out = anon.anonymize_text(text, {}, {}, mac_map=mac_map)
    assert out == text
    assert anon.find_violations(text) == []


def test_mac_address_embedded_in_longer_hex_run_is_not_matched():
    # La MAC debe estar delimitada por espacios/inicio-fin de linea, no ser
    # una porcion de una cadena hex:hex:... mas larga. Se usan 9 grupos (en vez
    # de 8) para que ademas no sea una IPv6 valida y no dispare esa regla.
    text = "chain aa:bb:cc:dd:ee:ff:11:22:33 not a mac token"
    assert anon.collect_macs(text) == []
    assert anon.find_violations(text) == []


def test_mac_address_delimited_by_spaces_is_matched():
    text = "mac aa:bb:cc:dd:ee:ff seen"
    assert anon.collect_macs(text) == ["aa:bb:cc:dd:ee:ff"]
    assert ("mac", "aa:bb:cc:dd:ee:ff") in anon.find_violations(text)


def test_anonymize_text_is_deterministic_on_synthetic_input_and_differs_by_input():
    text = "io.sentry.android.core reporting to 172.217.116.4 from 3a:4b:5c:6d:7e:8f"
    pkg_map = anon.build_other_pkg_mapping([text])
    ip_map = anon.build_ip_mapping([text])
    mac_map = anon.build_mac_mapping([text])
    out1 = anon.anonymize_text(text, ip_map, pkg_map, mac_map=mac_map)
    out2 = anon.anonymize_text(text, ip_map, pkg_map, mac_map=mac_map)
    assert out1 == out2

    other_text = "org.other.thing at 9.9.9.9"
    other_pkg_map = anon.build_other_pkg_mapping([other_text])
    other_ip_map = anon.build_ip_mapping([other_text])
    out3 = anon.anonymize_text(other_text, other_ip_map, other_pkg_map)
    assert out3 != out1


def test_serial_ssid_android_id_negative_control_untouched_when_absent():
    text = "no identifying fields on this line"
    assert anon.anonymize_text(text, {}, {}) == text


def test_serial_and_ssid_and_android_id_are_redacted():
    text = "ro.serialno: R3CX90ABCDE\nSSID: MyHomeWifi-5G\nandroidId: 0123456789abcdef\nSerial: R3CX90ABCDE"
    out = anon.anonymize_text(text, {}, {})
    assert "R3CX90ABCDE" not in out
    assert "MyHomeWifi-5G" not in out
    assert "0123456789abcdef" not in out


def test_find_violations_flags_generic_packages_and_ips_and_macs():
    text = "io.sentry.android.core and 172.217.116.4 and mac 3a:4b:5c:6d:7e:8f and bob@corp.example and owner was here"
    violations = anon.find_violations(text)
    rules = {rule for rule, _snippet in violations}
    assert "package" in rules
    assert "ipv4" in rules
    assert "mac" in rules
    assert "email" in rules
    assert "forbidden_substring" in rules


def test_find_violations_flags_previously_unlisted_tlds():
    # N2/N5: antes solo se marcaban paquetes cuya primera etiqueta estuviera en
    # una tabla cerrada de TLDs; eso dejaba pasar paquetes reales con ccTLDs
    # menos comunes.
    text = "ru.yandex.searchapp and se.bankid.client and tv.twitch.android.app"
    violations = {snippet for rule, snippet in anon.find_violations(text) if rule == "package"}
    assert "ru.yandex.searchapp" in violations
    assert "se.bankid.client" in violations
    assert "tv.twitch.android.app" in violations

    other_map = anon.build_other_pkg_mapping([text])
    out = anon.anonymize_text(text, {}, other_map)
    assert "ru.yandex" not in out
    assert "se.bankid" not in out
    assert "tv.twitch" not in out
    assert anon.find_violations(out) == []


def test_find_violations_negative_control_platform_namespaces_untouched():
    text = "android.os.Handler, com.google.gms.x, kotlin.coroutines.Job, java.lang.String"
    assert anon.find_violations(text) == []
    assert anon.anonymize_text(text, {}, {}) == text


def test_package_whole_token_replacement_not_partial_str_replace():
    # N2/N5: el reemplazo debe operar sobre el token COMPLETO que matchea
    # PKG_RE, nunca como un str.replace() del prefijo de 2 etiquetas dentro de
    # un token mas largo (eso producia 'com.example.appN.app' en vez de
    # 'com.example.appN').
    text = "installed com.foo.app here"
    other_map = anon.build_other_pkg_mapping([text])
    placeholder = other_map["com.foo"]
    out = anon.anonymize_text(text, {}, other_map)
    assert out == f"installed {placeholder} here"
    assert not placeholder.endswith(".app")
    assert ".app" not in out.replace(placeholder, "")


def test_find_violations_empty_for_clean_text():
    text = "nothing sensitive here, 10.0.2.5 is fine, com.android.systemui too, user@example.com too"
    assert anon.find_violations(text) == []


def test_find_violations_is_independent_of_anonymizer_tables():
    text = "com.example.app should also be flagged by the broad checker"
    violations = anon.find_violations(text)
    assert ("forbidden_substring", "example") in violations
