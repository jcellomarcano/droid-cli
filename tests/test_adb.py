"""droid.adb: parseo de `adb devices -l`, clasificacion de transporte y descubrimiento del binario."""
import pytest

import droid.adb as adbmod
from droid.adb import AdbError, Device, parse_devices_output
from tests.helpers import fixture_text

USB_LINE = "39181FDJG003LT    device usb:1-1 product:redfin model:Pixel_5 device:redfin transport_id:5"
WIFI_LINE = "192.168.1.102:5555    device product:sdk_gphone64_arm64 model:Pixel_8_Pro device:emu64a transport_id:9"
MDNS_LINE = "adb-39181FDJG003LT-AbCdEf._adb-tls-connect._tcp.    device product:redfin model:Pixel_5 device:redfin transport_id:11"


def test_parse_devices_output_from_fixture():
    devices = parse_devices_output(fixture_text("adb_devices_l"))
    assert len(devices) == 1
    d = devices[0]
    assert d.serial == "emulator-5554"
    assert d.state == "device"
    assert d.transport == "emu"
    assert d.model == "sdk gwear arm64"


def test_parse_devices_output_classifies_usb_wifi_and_mdns_transport():
    text = "List of devices attached\n" + USB_LINE + "\n" + WIFI_LINE + "\n" + MDNS_LINE + "\n"
    devices = parse_devices_output(text)
    assert len(devices) == 3
    by_serial = {d.serial: d for d in devices}
    assert by_serial["39181FDJG003LT"].transport == "usb"
    assert by_serial["192.168.1.102:5555"].transport == "wifi"
    assert by_serial["adb-39181FDJG003LT-AbCdEf._adb-tls-connect._tcp."].transport == "wifi"


def test_device_key_is_stable_across_usb_and_mdns_serial():
    usb_dev = Device(serial="39181FDJG003LT", state="device", transport="usb")
    usb_dev.hw_serial = "39181FDJG003LT"
    mdns_dev = Device(serial="adb-39181FDJG003LT-AbCdEf._adb-tls-connect._tcp.", state="device", transport="wifi")
    assert usb_dev.key == mdns_dev.key


def test_adb_path_raises_when_nothing_found(monkeypatch):
    monkeypatch.setattr(adbmod, "_ADB", None)
    monkeypatch.setattr(adbmod.config, "get", lambda key: "" if key == "adb" else None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(adbmod.shutil, "which", lambda name: None)
    monkeypatch.setattr(adbmod.os.path, "isfile", lambda p: False)
    with pytest.raises(AdbError) as exc:
        adbmod.adb_path()
    assert "platform-tools" in str(exc.value)
