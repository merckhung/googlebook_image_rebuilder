"""LG 15U50U: display, AX203, ACPI power and physically identified ALC256 HDA audio."""
import xml.etree.ElementTree as ET

AUDIO_REF = "sof-soundwire_cs42l43_cs35l56"

def display(ctx):
    """build.prop: the profile's property changes and the hardware composer backend. On the GOP framebuffer
    (gbos_efifb) drm_hwcomposer must hand every layer to SurfaceFlinger's client composition; the framebuffer device
    has one plane and no colour pipeline."""
    bp = ctx.tree / "build.prop"
    bt = bp.read_text()
    for k, old, new in ctx.prof["display"]["build_prop_set"]:
        line = f"{k}={old}"
        if bt.splitlines().count(line) != 1 or sum(1 for l in bt.splitlines() if l.startswith(k + "=")) != 1:
            raise SystemExit("prop not exactly once: " + line)
        bt = bt.replace("\n" + line + "\n", "\n" + f"{k}={new}" + "\n", 1)
    if ctx.prof["display"]["hwc_backend"] == "client":
        if any(l.startswith("vendor.hwc.backend_override=") for l in bt.splitlines()):
            raise SystemExit("backend_override already set")
        bt = bt.rstrip("\n") + "\n# generic-x86: one GOP framebuffer plane; compose everything into the client target\nvendor.hwc.backend_override=client\n"
    bp.write_text(bt)
    print("hwc backend:", [l for l in bt.splitlines() if "backend_override" in l])


def expected(prof):
    new, changed = [], ["etc/excluded-input-devices.xml"]
    if prof["audio"]["config_dir"]:
        directory = "etc/audio/" + prof["audio"]["config_dir"]
        new = [directory] + [directory + "/" + f for f in
                             ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml")]
        changed.append("etc/data/hal_configs/audio_default.xml")
    return new, changed


def audio(ctx):
    """ALC256, subsystem 1854:057f, with sof-hda-generic IPC4 topology.

    Physical probe: analog playback/capture PCM 0, HDMI PCM 3/4/5, deepbuffer
    PCM 31. NHLT exposes no DMIC array; the internal stereo mic is codec pin
    0x12 through PCM 0. Do not route it to the topology's unused DMIC PCM 6.
    """
    tree, vmap = ctx.tree, ctx.vmap
    card, cdir = ctx.prof["audio"]["card"], ctx.prof["audio"]["config_dir"]
    reference = tree / "etc/audio" / AUDIO_REF
    lic = (reference / "audio_config.xml").read_text().split("<!--", 1)[1].split("-->", 1)[0]
    directory = tree / "etc/audio" / cdir
    directory.mkdir(mode=0o755)
    vmap["etc/audio/" + cdir] = dict(vmap["etc/audio/" + AUDIO_REF])
    hdmi = "".join(f"""        <HDMI{i}>
            <PlaybackPCM>hw:sofhdadsp,{pcm}</PlaybackPCM>
            <JackDev>sof-hda-dsp HDMI/DP,pcm={pcm}</JackDev>
            <PlaybackChannels>2</PlaybackChannels>
        </HDMI{i}>
""" for i, pcm in enumerate((3, 4, 5), 1))
    (directory / "audio_config.xml").write_text(f"""<?xml version='1.0' encoding='utf-8'?>
<audio_config>
    <!--{lic}-->
    <SoundCardName>hw:sofhdadsp</SoundCardName>
    <HiFi>
        <Speaker>
            <PlaybackPCM>hw:sofhdadsp,0</PlaybackPCM>
        </Speaker>
        <Headphone>
            <PlaybackPCM>hw:sofhdadsp,0</PlaybackPCM>
            <JackDev>sof-hda-dsp Headphone</JackDev>
            <JackSwitch>2</JackSwitch>
        </Headphone>
        <InternalMic>
            <CapturePCM>hw:sofhdadsp,0</CapturePCM>
            <CaptureChannelMap><FL>0</FL><FR>1</FR></CaptureChannelMap>
            <!-- Provisional: unity software capture gain, not factory calibration. -->
            <IntrinsicSensitivity>-600</IntrinsicSensitivity>
        </InternalMic>
{hdmi}    </HiFi>
</audio_config>
""")
    # Codec amp offsets: playback 0x57 and capture 0x17 are unity gain.
    # Master is a single virtual control; the codec controls are stereo.
    (directory / "mixer_paths.xml").write_text(f"""<?xml version='1.0' encoding='utf-8'?>
<mixer enum_mixer_numeric_fallback="true">
    <!--{lic}-->
    <ctl name="Master Playback Volume" value="87" />
    <ctl name="Master Playback Switch" value="1" />
    <ctl name="Speaker Playback Volume" value="87 87" />
    <ctl name="Headphone Playback Volume" value="87 87" />
    <ctl name="Pre Mixer Analog Playback Volume" value="45 45" />
    <ctl name="Post Mixer Analog Playback Volume" value="45 45" />
    <ctl name="Capture Volume" value="23 23" />
    <ctl name="Mic Boost Volume" value="0 0" />
    <ctl name="Speaker Playback Switch" value="0 0" />
    <ctl name="Headphone Playback Switch" value="0 0" />
    <ctl name="Capture Switch" value="0 0" />
    <path name="Speaker">
        <ctl name="Speaker Playback Switch" value="1 1" />
    </path>
    <path name="Headphone">
        <ctl name="Headphone Playback Switch" value="1 1" />
    </path>
    <path name="InternalMic">
        <ctl name="Capture Switch" value="1 1" />
    </path>
    <path name="HDMI1" />
    <path name="HDMI2" />
    <path name="HDMI3" />
</mixer>
""")
    # Retain Android's volume index layout. Provisional curves attenuate the
    # maximum by 6 dB; these are not LG factory speaker/headphone calibration.
    curves = ET.parse(reference / "volume_curves.xml",
                      parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    for ref in curves.findall("reference"):
        if ref.attrib.get("name") not in ("Speaker", "Headphone"):
            continue
        points = ref.findall("point")
        top = max(int(point.text.split(",")[0]) for point in points)
        floor = -6000 if ref.attrib["name"] == "Speaker" else -7000
        for point in points:
            index = int(point.text.split(",")[0])
            point.text = f"{index},{round(floor + (-600 - floor) * index / top)}"
    curves.write(directory / "volume_curves.xml", encoding="utf-8", xml_declaration=True)
    for filename in ("audio_config.xml", "mixer_paths.xml", "volume_curves.xml"):
        path = directory / filename
        path.chmod(0o644)
        vmap[f"etc/audio/{cdir}/{filename}"] = dict(vmap[f"etc/audio/{AUDIO_REF}/audio_config.xml"])
        ET.parse(path)
    default = tree / "etc/data/hal_configs/audio_default.xml"
    old = f"<soundcard>sof-soundwire</soundcard>\n  <audio-config-dir>{AUDIO_REF}</audio-config-dir>"
    text = default.read_text()
    if text.count(old) != 1:
        raise SystemExit("LG: audio_default.xml not as expected")
    default.write_text(text.replace(old, f"<soundcard>{card}</soundcard>\n  <audio-config-dir>{cdir}</audio-config-dir>"))
    ET.parse(default)


def wifi_rc(ctx):
    return ("# LG 15U50U: Intel AX203 uses the iwlmvm op-mode.\n"
            "on early-boot\n" + "".join(
                "    exec_background u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe "
                "--dirname=/vendor/lib/modules " + name + "\n"
                for name in ctx.prof["wifi"]["modprobe"]) + "\n")


def power_button(ctx):
    path = ctx.tree / "etc/excluded-input-devices.xml"
    text = path.read_text()
    old = '    <device name="Power Button"/>\n'
    if text.count(old) != 1:
        raise SystemExit("LG: expected one excluded ACPI Power Button")
    path.write_text(text.replace(
        old, "    <!-- LG 15U50U uses the standard ACPI Power Button. -->\n", 1))
