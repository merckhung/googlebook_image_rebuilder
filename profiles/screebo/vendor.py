"""SCREEBO (ASUS ExpertBook CX54 Chromebook Plus Model CX5403CMA, Core Ultra 5 115U, Meteor Lake) vendor edits that need
more than profile data (hooks.vendor in profile.toml). builder/vendor.sh calls each hook at its place in the vendor
rebuild with a context: ctx.tree (the extracted vendor tree), ctx.vmap (path -> erofs metadata; new paths copy the
metadata of a stock file), ctx.prof (the validated profile) and ctx.audio_src (the fetched audio sources).
expected(prof) tells vendor.sh which vendor paths the hooks add and change, so its final tree comparison stays exact."""
import re
import xml.dom.minidom as md

ID = "SCREEBO"
AUDIO_REF = "sof-soundwire_cs42l43_cs35l56"     # lapis' own audio SKU: licence header and volume-curve layout
KB_OLD = "/sys/devices/platform/GOOG0002:00/leds"  # where lapis expects the keyboard backlight LED

# PCM devices of the Meteor Lake sof-rt5682 card (sof-mtl-rt1019-rt5682.tplg), from ChromeOS
# ucm-config/sof-rt5682.screebo/HiFi.conf. They differ from the Alder Lake (marasov) card: headset 0, speaker 1, SCO 2,
# HDMI/DP 5..7 (three ports), DMIC 99.
PCM_HEADSET = 0
PCM_SPEAKER = 1
PCM_SCO = 2
PCM_HDMI = (5, 6, 7)
PCM_DMIC = 99


def expected(prof):
    """(new paths, changed paths) in the vendor tree, relative to /vendor."""
    new, changed = [], []
    a = prof["audio"]
    if a["config_dir"]:
        d = "etc/audio/" + a["config_dir"]
        new += [d] + [f"{d}/{f}" for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml")]
        changed.append("etc/data/hal_configs/audio_default.xml")
    if prof["input"]["kbd_backlight_leds"]:
        new.append(f"etc/ueventd/ueventd.{prof['model']['id']}.rc")
        changed.append("usr/idc/AT_Translated_Set_2_keyboard.idc")
    if prof["input"]["power_button"] == "acpi":
        changed.append("etc/excluded-input-devices.xml")
    return new, changed


def display(ctx):
    """build.prop: the profile's property changes and the hardware composer backend."""
    bp = ctx.tree / "build.prop"
    bt = bp.read_text()
    for k, old, new in ctx.prof["display"]["build_prop_set"]:
        line = f"{k}={old}"
        if bt.splitlines().count(line) != 1 or sum(1 for l in bt.splitlines() if l.startswith(k + "=")) != 1:
            raise SystemExit("prop not exactly once: " + line)
        bt = bt.replace("\n" + line + "\n", "\n" + f"{k}={new}" + "\n", 1)
    bp.write_text(bt)
    if ctx.prof["display"]["hwc_backend"] == "client":
        bt = bp.read_text()
        if any(l.startswith("vendor.hwc.backend_override=") for l in bt.splitlines()):
            raise SystemExit("backend_override already set")
        bt = bt.rstrip("\n") + (f"\n# {ID}: device composition is not proven on Meteor Lake with this xe build; "
                                "compose everything into the client target\nvendor.hwc.backend_override=client\n")
        bp.write_text(bt)
    print("hwc backend:", [l for l in bt.splitlines() if "backend_override" in l])
    print("colour props:", [l for l in bt.splitlines() if "color" in l or "HDR" in l or "gamma" in l])


def wifi_rc(ctx):
    """Head of etc/init/kano-hw.rc: the Wi-Fi op-mode modules lapis does not load itself."""
    mods = ctx.prof["wifi"]["modprobe"]
    if not mods:
        return ""
    return (f"# {ID}: the Meteor Lake CNVi AX211 needs the iwlmvm op-mode (the image only loads iwlmld, for the lapis BE200/BE211).\n"
            "on early-boot\n"
            + "".join(f"    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe --dirname=/vendor/lib/modules {m}\n" for m in mods)
            + "\n")


def audio(ctx):
    """DRAS (the desktop audio HAL) configuration for the SCREEBO card: sof-rt5682 (Realtek ALC5682I headset +
    ALC1019P speaker amp, SOF IPC4 on Meteor Lake). Translated from ChromeOS board-overlays
    overlay-rex/chromeos-base/chromeos-bsp-rex/files/screebo/audio
    (ucm-config/sof-rt5682.screebo/HiFi.conf, cras-config/sof-rt5682.card_settings)."""
    tree, vmap, src_audio = ctx.tree, ctx.vmap, ctx.audio_src
    card, cdir = ctx.prof["audio"]["card"], ctx.prof["audio"]["config_dir"]
    lic = (tree / f"etc/audio/{AUDIO_REF}/audio_config.xml").read_text().split("<!--", 1)[1].split("-->", 1)[0]
    adir = tree / "etc/audio" / cdir
    if adir.exists():
        raise SystemExit("screebo audio dir already present")
    # the UCM must be the one these PCM numbers were taken from
    ucm = (src_audio / "HiFi.conf").read_text()
    for want in (f'PlaybackPCM "hw:sofrt5682,{PCM_SPEAKER}"', f'PlaybackPCM "hw:sofrt5682,{PCM_HEADSET}"',
                 f'CapturePCM "hw:sofrt5682,{PCM_DMIC}"', f'PlaybackPCM "hw:sofrt5682,{PCM_SCO}"'):
        if want not in ucm:
            raise SystemExit("screebo HiFi.conf does not match the PCM map: " + want)
    # DRAS reads named XML channel positions, whereas CRAS card_settings uses
    # a text vector. The two populated DMIC channels on SCREEBO are 2 and 3.
    # Mixer values below also need one value per ALSA control element: DRAS
    # does not broadcast a scalar to the stereo DAC/ADC or four-channel DMIC.
    adir.mkdir(); adir.chmod(0o755)
    vmap["etc/audio/" + cdir] = dict(vmap["etc/audio/" + AUDIO_REF])
    hdmi = "".join(f"""\t\t<HDMI{i}>
\t\t\t<PlaybackPCM>hw:sofrt5682,{pcm}</PlaybackPCM>
\t\t\t<JackDev>sof-rt5682 HDMI/DP,pcm={pcm}</JackDev>
\t\t\t<PlaybackChannels>2</PlaybackChannels>
\t\t</HDMI{i}>
""" for i, pcm in enumerate(PCM_HDMI, 1))
    (adir / "audio_config.xml").write_text(f"""<?xml version='1.0' encoding='utf-8'?>
<audio_config>
\t<!--{lic}-->
\t<!-- SCREEBO (ASUS ExpertBook CX54 CX5403CMA, rex/screebo): from ChromeOS ucm-config/sof-rt5682.screebo/HiFi.conf -->
\t<SoundCardName>hw:sofrt5682</SoundCardName>
\t<HiFi>
\t\t<Speaker>
\t\t\t<PlaybackPCM>hw:sofrt5682,{PCM_SPEAKER}</PlaybackPCM>
\t\t</Speaker>
\t\t<Headphone>
\t\t\t<PlaybackPCM>hw:sofrt5682,{PCM_HEADSET}</PlaybackPCM>
\t\t\t<PlaybackMixerElem>DAC1</PlaybackMixerElem>
\t\t\t<JackDev>sof-rt5682 Headset Jack</JackDev>
\t\t\t<JackSwitch>2</JackSwitch>
\t\t</Headphone>
\t\t<InternalMic>
\t\t\t<CapturePCM>hw:sofrt5682,{PCM_DMIC}</CapturePCM>
\t\t\t<CaptureChannelMap>
\t\t\t\t<FL>2</FL>
\t\t\t\t<FR>3</FR>
\t\t\t</CaptureChannelMap>
\t\t\t<IntrinsicSensitivity>-2300</IntrinsicSensitivity>
\t\t</InternalMic>
\t\t<Mic>
\t\t\t<CapturePCM>hw:sofrt5682,{PCM_HEADSET}</CapturePCM>
\t\t\t<CaptureMixerElem>Headset Mic</CaptureMixerElem>
\t\t\t<JackDev>sof-rt5682 Headset Jack</JackDev>
\t\t</Mic>
{hdmi}\t\t<SCOLineOut>
\t\t\t<PlaybackPCM>hw:sofrt5682,{PCM_SCO}</PlaybackPCM>
\t\t</SCOLineOut>
\t\t<SCOLineIn>
\t\t\t<CapturePCM>hw:sofrt5682,{PCM_SCO}</CapturePCM>
\t\t</SCOLineIn>
\t</HiFi>
</audio_config>
""")
    hdmi_paths = "".join(f'\t<path name="HDMI{i}" />\n' for i in range(1, len(PCM_HDMI) + 1))
    (adir / "mixer_paths.xml").write_text(f"""<?xml version='1.0' encoding='utf-8'?>
<mixer enum_mixer_numeric_fallback="true">
\t<!--{lic}-->
\t<!-- SCREEBO: SectionVerb EnableSequence of sof-rt5682.screebo/HiFi.conf (ALC5682I + ALC1019P, Meteor Lake) -->
\t<ctl name="Stereo1 ADC L Mux" value="0" />
\t<ctl name="STO1 ADC Capture Switch" value="1 1" />
\t<ctl name="RECMIX1L CBJ Switch" value="1" />
\t<ctl name="IF1 01 ADC Swap Mux" value="2" />
\t<ctl name="CBJ Boost Volume" value="28" />
\t<ctl name="Stereo1 ADC L1 Mux" value="1" />
\t<ctl name="Stereo1 ADC R1 Mux" value="1" />
\t<ctl name="Stereo1 ADC MIXL ADC2 Switch" value="0" />
\t<ctl name="Stereo1 ADC MIXR ADC2 Switch" value="0" />
\t<ctl name="Stereo1 ADC MIXL ADC1 Switch" value="1" />
\t<ctl name="Stereo1 ADC MIXR ADC1 Switch" value="1" />
\t<ctl name="DAC1 Playback Volume" value="127 127" />
\t<ctl name="Dmic0 Capture Switch" value="1 1 1 1" />
\t<!-- device switches start off; each path turns its own on -->
\t<ctl name="Left Spk Switch" value="0" />
\t<ctl name="Right Spk Switch" value="0" />
\t<ctl name="Headphone Jack Switch" value="0" />
\t<ctl name="Stereo1 DAC MIXL DAC L1 Switch" value="0" />
\t<ctl name="Stereo1 DAC MIXR DAC R1 Switch" value="0" />
\t<ctl name="Headset Mic Switch" value="0" />
\t<path name="Speaker">
\t\t<ctl name="Left Spk Switch" value="1" />
\t\t<ctl name="Right Spk Switch" value="1" />
\t</path>
\t<path name="Headphone">
\t\t<ctl name="Headphone Jack Switch" value="1" />
\t\t<ctl name="Stereo1 DAC MIXL DAC L1 Switch" value="1" />
\t\t<ctl name="Stereo1 DAC MIXR DAC R1 Switch" value="1" />
\t</path>
\t<path name="InternalMic" />
\t<path name="Mic">
\t\t<ctl name="Headset Mic Switch" value="1" />
\t</path>
{hdmi_paths}\t<path name="SCOLineOut" />
\t<path name="SCOLineIn" />
</mixer>
""")
    # Volume curves: keep lapis' point indices, take the dB values from screebo's CRAS card_settings.
    cs = (src_audio / "sof-rt5682.card_settings").read_text()
    spk = {int(m.group(1)): int(m.group(2)) for m in re.finditer(r"db_at_(\d+) = (-?\d+)", cs.split("[Headphone]")[0])}
    assert sorted(spk) == list(range(101)), "speaker curve"
    hp = cs.split("[Headphone]")[1]
    hp_step = int(re.search(r"volume_step = (\d+)", hp).group(1))
    hp_max = int(re.search(r"max_volume = (-?\d+)", hp).group(1))

    def screebo_db(ref, pct):
        return spk[pct] if ref == "Speaker" else hp_max - hp_step * (100 - pct)

    vc = (tree / f"etc/audio/{AUDIO_REF}/volume_curves.xml").read_text()

    def fix_ref(m):
        ref, body = m.group(1), m.group(2)
        pts = [int(p) for p in re.findall(r"<point>(\d+),-?\d+</point>", body)]
        if ref not in ("Speaker", "Headphone"):
            return m.group(0)
        top = max(pts)
        new = body
        for p in pts:
            new = re.sub(rf"<point>{p},-?\d+</point>", f"<point>{p},{screebo_db(ref, round(p * 100 / top))}</point>", new, count=1)
        return f'<reference name="{ref}">{new}</reference>'

    vc2, nref = re.subn(r'<reference name="([^"]+)">(.*?)</reference>', fix_ref, vc, flags=re.S)
    assert nref >= 2 and vc2 != vc, "volume curves"
    (adir / "volume_curves.xml").write_text(vc2)
    for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
        (adir / f).chmod(0o644)
        vmap[f"etc/audio/{cdir}/" + f] = dict(vmap[f"etc/audio/{AUDIO_REF}/audio_config.xml"])
    # Point the default audio hal_config at the SCREEBO card (lapis' own cs42l43 SKU file stays as it is).
    hc = tree / "etc/data/hal_configs/audio_default.xml"
    old_hc = f"<soundcard>sof-soundwire</soundcard>\n  <audio-config-dir>{AUDIO_REF}</audio-config-dir>"
    if hc.read_text().count(old_hc) != 1:
        raise SystemExit("audio_default.xml not as expected")
    hc.write_text(hc.read_text().replace(old_hc, f"<soundcard>{card}</soundcard>\n  <audio-config-dir>{cdir}</audio-config-dir>"))
    for f in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
        md.parse(str(adir / f))
    md.parse(str(hc))
    print("screebo audio:", sorted(p.name for p in adir.iterdir()), hc.read_text())


def kbd_backlight(ctx):
    """Keyboard backlight: on the rex baseboard, as on brya, ACPI GOOG0002 (\\_SB_.CREC.KBLT) sits under the EC node,
    so cros_kbd_led_backlight registers /sys/devices/platform/GOOG0004:00/GOOG0002:00/leds/chromeos::kbd_backlight."""
    tree, vmap = ctx.tree, ctx.vmap
    KB_NEW = ctx.prof["input"]["kbd_backlight_leds"]
    if not KB_NEW:
        return
    idc = tree / "usr/idc/AT_Translated_Set_2_keyboard.idc"
    it = idc.read_text()
    if it.count("device.additionalSysfsLedsNode = " + KB_OLD + "/chromeos::kbd_backlight") != 1:
        raise SystemExit("keyboard idc not as expected")
    idc.write_text(it.replace(KB_OLD, KB_NEW))
    ue = tree / f"etc/ueventd/ueventd.{ctx.prof['model']['id']}.rc"
    ue.write_text(f"# {ID}: keyboard backlight LED (GOOG0002 under the EC node GOOG0004)\n" + KB_NEW + "/* brightness 0660 system system\n")
    ue.chmod(0o644)
    vmap[f"etc/ueventd/{ue.name}"] = dict(vmap["etc/ueventd/ueventd.fatcat.rc"])
    cil2 = tree / "etc/selinux/vendor_sepolicy.cil"
    ct2 = cil2.read_text()
    gline = '(genfscon sysfs "/devices/platform/GOOG0002:00/leds" (u object_r sysfs_keyboard_backlight ((s0) (s0))))'
    if ct2.count(gline) != 1:
        raise SystemExit("keyboard genfscon not as expected")
    cil2.write_text(ct2.rstrip("\n") + f"\n;; {ID} keyboard backlight LED path (GOOG0002 under GOOG0004)\n" + gline.replace("/devices/platform/GOOG0002:00/leds", KB_NEW[4:]) + "\n")
    print("kbd idc:", [l for l in idc.read_text().splitlines() if "LedsNode" in l])
    print("kbd ueventd:", ue.read_text())


def power_button(ctx):
    """lapis excludes the ACPI "Power Button" input device because its power key arrives through cros_ec_buttons
    (cros_ec_keyb). On the SCREEBO cros_ec_keyb is blacklisted and the i8042 keyboard carries no power key, so the ACPI
    device is the only source: stop excluding it."""
    if ctx.prof["input"]["power_button"] != "acpi":
        return
    ex = ctx.tree / "etc/excluded-input-devices.xml"
    et = ex.read_text()
    if et.count('<device name="Power Button"/>') != 1:
        raise SystemExit("excluded-input-devices.xml not as expected")
    ex.write_text(et.replace('    <device name="Power Button"/>\n',
                             f'    <!-- {ID}: cros_ec_keyb (cros_ec_buttons) is blacklisted, so the ACPI Power Button is the only power key -->\n', 1))
    print("excluded-input-devices:", ex.read_text())
