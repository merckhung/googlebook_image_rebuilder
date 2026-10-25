#include "src/audio/marasov_audio.h"

#include <cmath>
#include <cstdint>
#include <regex>
#include <sstream>
#include <stdexcept>

namespace googlebookos::audio {
namespace {

void AppendLe16(std::vector<uint8_t>* buf, uint16_t v) {
  buf->push_back(static_cast<uint8_t>(v & 0xFF));
  buf->push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
}

void AppendLe32(std::vector<uint8_t>* buf, uint32_t v) {
  buf->push_back(static_cast<uint8_t>(v & 0xFF));
  buf->push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
  buf->push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
  buf->push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}

}  // namespace

ParsedUcmAndCardSettings ParseMarasovAudioSources(
    const std::string& hifi_conf_text,
    const std::string& card_settings_text) {
  ParsedUcmAndCardSettings out;

  if (hifi_conf_text.find("PlaybackPCM \"hw:sofrt5682,0\"") ==
      std::string::npos) {
    throw std::runtime_error("HiFi.conf missing Speaker PlaybackPCM hw:sofrt5682,0");
  }
  if (hifi_conf_text.find("PlaybackPCM \"hw:sofrt5682,1\"") ==
      std::string::npos) {
    throw std::runtime_error("HiFi.conf missing Headphone PlaybackPCM hw:sofrt5682,1");
  }
  if (hifi_conf_text.find("CapturePCM \"hw:sofrt5682,99\"") ==
      std::string::npos) {
    throw std::runtime_error("HiFi.conf missing Internal Mic CapturePCM hw:sofrt5682,99");
  }
  if (hifi_conf_text.find("CaptureChannelMap \"2 3 -1 -1 -1 -1 -1 -1 -1 -1 -1\"") ==
      std::string::npos) {
    throw std::runtime_error("HiFi.conf missing Internal Mic CaptureChannelMap");
  }

  auto hp_pos = card_settings_text.find("[Headphone]");
  if (hp_pos == std::string::npos) {
    throw std::runtime_error("sof-rt5682.card_settings missing [Headphone] section");
  }
  std::string spk_sec = card_settings_text.substr(0, hp_pos);
  std::string hp_sec = card_settings_text.substr(hp_pos);

  std::regex db_re(R"(db_at_(\d+)\s*=\s*(-?\d+))");
  for (std::sregex_iterator it(spk_sec.begin(), spk_sec.end(), db_re), end;
       it != end; ++it) {
    int pct = std::stoi((*it)[1].str());
    int db = std::stoi((*it)[2].str());
    out.speaker_db_at_pct[pct] = db;
  }
  if (out.speaker_db_at_pct.size() != 101) {
    throw std::runtime_error(
        "sof-rt5682.card_settings must define db_at_0..db_at_100 (101 points)");
  }

  std::smatch m;
  if (!std::regex_search(hp_sec, m, std::regex(R"(volume_step\s*=\s*(\d+))"))) {
    throw std::runtime_error("sof-rt5682.card_settings missing volume_step");
  }
  out.headphone_volume_step = std::stoi(m[1].str());
  if (!std::regex_search(hp_sec, m, std::regex(R"(max_volume\s*=\s*(-?\d+))"))) {
    throw std::runtime_error("sof-rt5682.card_settings missing max_volume");
  }
  out.headphone_max_volume = std::stoi(m[1].str());
  return out;
}

MarasovAudioFiles GenerateMarasovDrasXmls(
    const ParsedUcmAndCardSettings& parsed,
    const std::string& reference_volume_curves_xml) {
  MarasovAudioFiles out;

  std::ostringstream ac;
  ac << "<?xml version='1.0' encoding='utf-8'?>\n"
     << "<audio_config>\n"
     << "\t<!-- MARASOV (ASUS Chromebook Plus CX34 CX3402, brya/marasov): "
        "from ChromeOS ucm-config/sof-rt5682.marasov.rt5682vs/HiFi.conf -->\n"
     << "\t<SoundCardName>" << parsed.sound_card_name << "</SoundCardName>\n"
     << "\t<HiFi>\n"
     << "\t\t<Speaker>\n"
     << "\t\t\t<PlaybackPCM>" << parsed.speaker_pcm << "</PlaybackPCM>\n"
     << "\t\t</Speaker>\n"
     << "\t\t<Headphone>\n"
     << "\t\t\t<PlaybackPCM>" << parsed.headphone_pcm << "</PlaybackPCM>\n"
     << "\t\t\t<PlaybackMixerElem>" << parsed.headphone_mixer_elem
     << "</PlaybackMixerElem>\n"
     << "\t\t\t<JackDev>" << parsed.headphone_jack_dev << "</JackDev>\n"
     << "\t\t\t<JackSwitch>" << parsed.headphone_jack_switch
     << "</JackSwitch>\n"
     << "\t\t</Headphone>\n"
     << "\t\t<InternalMic>\n"
     << "\t\t\t<CapturePCM>" << parsed.internal_mic_pcm << "</CapturePCM>\n"
     << "\t\t\t<CaptureChannelMap>" << parsed.internal_mic_channel_map
     << "</CaptureChannelMap>\n"
     << "\t\t\t<IntrinsicSensitivity>" << parsed.internal_mic_sensitivity
     << "</IntrinsicSensitivity>\n"
     << "\t\t</InternalMic>\n"
     << "\t\t<Mic>\n"
     << "\t\t\t<CapturePCM>" << parsed.headset_mic_pcm << "</CapturePCM>\n"
     << "\t\t\t<CaptureMixerElem>" << parsed.headset_mic_mixer_elem
     << "</CaptureMixerElem>\n"
     << "\t\t\t<JackDev>" << parsed.headset_mic_jack_dev << "</JackDev>\n"
     << "\t\t</Mic>\n";
  for (int i = 1; i <= 4; ++i) {
    ac << "\t\t<HDMI" << i << ">\n"
       << "\t\t\t<PlaybackPCM>hw:sofrt5682," << (i + 1) << "</PlaybackPCM>\n"
       << "\t\t\t<JackDev>sof-rt5682 HDMI/DP,pcm=" << (i + 1) << "</JackDev>\n"
       << "\t\t\t<PlaybackChannels>2</PlaybackChannels>\n"
       << "\t\t</HDMI" << i << ">\n";
  }
  ac << "\t\t<SCOLineOut>\n"
     << "\t\t\t<PlaybackPCM>hw:sofrt5682,7</PlaybackPCM>\n"
     << "\t\t</SCOLineOut>\n"
     << "\t\t<SCOLineIn>\n"
     << "\t\t\t<CapturePCM>hw:sofrt5682,7</CapturePCM>\n"
     << "\t\t</SCOLineIn>\n"
     << "\t</HiFi>\n"
     << "</audio_config>\n";
  out.audio_config_xml = ac.str();

  out.mixer_paths_xml = R"(<?xml version='1.0' encoding='utf-8'?>
<mixer enum_mixer_numeric_fallback="true">
	<!-- MARASOV: SectionVerb EnableSequence of sof-rt5682.marasov.rt5682vs/HiFi.conf (ALC5682I-VS/VD + ALC1019P) -->
	<ctl name="Stereo1 ADC L Mux" value="0" />
	<ctl name="RECMIX1L CBJ Switch" value="1" />
	<ctl name="IF1 01 ADC Swap Mux" value="2" />
	<ctl name="CBJ Boost Volume" value="28" />
	<ctl name="Stereo1 ADC L1 Mux" value="1" />
	<ctl name="Stereo1 ADC R1 Mux" value="1" />
	<ctl name="Stereo1 ADC MIXL ADC2 Switch" value="0" />
	<ctl name="Stereo1 ADC MIXR ADC2 Switch" value="0" />
	<ctl name="Stereo1 ADC MIXL ADC1 Switch" value="1" />
	<ctl name="Stereo1 ADC MIXR ADC1 Switch" value="1" />
	<ctl name="Left Spk Switch" value="0" />
	<ctl name="Right Spk Switch" value="0" />
	<ctl name="Headphone Jack Switch" value="0" />
	<ctl name="Stereo1 DAC MIXL DAC L1 Switch" value="0" />
	<ctl name="Stereo1 DAC MIXR DAC R1 Switch" value="0" />
	<ctl name="Headset Mic Switch" value="0" />
	<path name="Speaker">
		<ctl name="Left Spk Switch" value="1" />
		<ctl name="Right Spk Switch" value="1" />
	</path>
	<path name="Headphone">
		<ctl name="Headphone Jack Switch" value="1" />
		<ctl name="Stereo1 DAC MIXL DAC L1 Switch" value="1" />
		<ctl name="Stereo1 DAC MIXR DAC R1 Switch" value="1" />
	</path>
	<path name="InternalMic" />
	<path name="Mic">
		<ctl name="Headset Mic Switch" value="1" />
	</path>
	<path name="HDMI1" />
	<path name="HDMI2" />
	<path name="HDMI3" />
	<path name="HDMI4" />
	<path name="SCOLineOut" />
	<path name="SCOLineIn" />
</mixer>
)";

  std::ostringstream vc;
  vc << "<?xml version='1.0' encoding='utf-8'?>\n"
     << "<volume_curves>\n"
     << "\t<reference name=\"Speaker\">\n";
  for (int pct = 0; pct <= 100; ++pct) {
    vc << "\t\t<point>" << pct << "," << parsed.speaker_db_at_pct.at(pct)
       << "</point>\n";
  }
  vc << "\t</reference>\n"
     << "\t<reference name=\"Headphone\">\n";
  for (int pct = 0; pct <= 100; ++pct) {
    int db = parsed.headphone_max_volume -
             parsed.headphone_volume_step * (100 - pct);
    vc << "\t\t<point>" << pct << "," << db << "</point>\n";
  }
  vc << "\t</reference>\n"
     << "</volume_curves>\n";
  out.volume_curves_xml =
      reference_volume_curves_xml.empty() ? vc.str() : vc.str();

  return out;
}

ParsedUcmAndCardSettings ParseScreeboAudioSources(
    const std::string& hifi_conf_text,
    const std::string& card_settings_text) {
  ParsedUcmAndCardSettings out;
  out.speaker_pcm = "hw:sofrt5682,1";
  out.headphone_pcm = "hw:sofrt5682,0";
  out.headset_mic_pcm = "hw:sofrt5682,0";
  out.internal_mic_sensitivity = -2300;

  if (hifi_conf_text.find("PlaybackPCM \"hw:sofrt5682,1\"") ==
      std::string::npos) {
    throw std::runtime_error("screebo HiFi.conf missing Speaker PlaybackPCM hw:sofrt5682,1");
  }
  if (hifi_conf_text.find("PlaybackPCM \"hw:sofrt5682,0\"") ==
      std::string::npos) {
    throw std::runtime_error("screebo HiFi.conf missing Headphone PlaybackPCM hw:sofrt5682,0");
  }
  if (hifi_conf_text.find("PlaybackPCM \"hw:sofrt5682,2\"") ==
      std::string::npos) {
    throw std::runtime_error("screebo HiFi.conf missing Bluetooth SCO PlaybackPCM hw:sofrt5682,2");
  }
  if (hifi_conf_text.find("CapturePCM \"hw:sofrt5682,99\"") ==
      std::string::npos) {
    throw std::runtime_error("screebo HiFi.conf missing Internal Mic CapturePCM hw:sofrt5682,99");
  }
  if (hifi_conf_text.find("CaptureChannelMap \"2 3 -1 -1 -1 -1 -1 -1 -1 -1 -1\"") ==
      std::string::npos) {
    throw std::runtime_error("screebo HiFi.conf missing Internal Mic CaptureChannelMap");
  }

  auto hp_pos = card_settings_text.find("[Headphone]");
  if (hp_pos == std::string::npos) {
    throw std::runtime_error("sof-rt5682.card_settings missing [Headphone] section");
  }
  std::string spk_sec = card_settings_text.substr(0, hp_pos);
  std::string hp_sec = card_settings_text.substr(hp_pos);

  std::regex db_re(R"(db_at_(\d+)\s*=\s*(-?\d+))");
  for (std::sregex_iterator it(spk_sec.begin(), spk_sec.end(), db_re), end;
       it != end; ++it) {
    int pct = std::stoi((*it)[1].str());
    int db = std::stoi((*it)[2].str());
    out.speaker_db_at_pct[pct] = db;
  }
  if (out.speaker_db_at_pct.size() != 101) {
    throw std::runtime_error(
        "sof-rt5682.card_settings must define db_at_0..db_at_100 (101 points)");
  }

  std::smatch m;
  if (!std::regex_search(hp_sec, m, std::regex(R"(volume_step\s*=\s*(\d+))"))) {
    throw std::runtime_error("sof-rt5682.card_settings missing volume_step");
  }
  out.headphone_volume_step = std::stoi(m[1].str());
  if (!std::regex_search(hp_sec, m, std::regex(R"(max_volume\s*=\s*(-?\d+))"))) {
    throw std::runtime_error("sof-rt5682.card_settings missing max_volume");
  }
  out.headphone_max_volume = std::stoi(m[1].str());
  return out;
}

MarasovAudioFiles GenerateScreeboDrasXmls(
    const ParsedUcmAndCardSettings& parsed,
    const std::string& reference_volume_curves_xml) {
  MarasovAudioFiles out;

  std::ostringstream ac;
  ac << "<?xml version='1.0' encoding='utf-8'?>\n"
     << "<audio_config>\n"
     << "\t<!-- SCREEBO (ASUS ExpertBook CX54 CX5403CMA, rex/screebo): "
        "from ChromeOS ucm-config/sof-rt5682.screebo/HiFi.conf -->\n"
     << "\t<SoundCardName>" << parsed.sound_card_name << "</SoundCardName>\n"
     << "\t<HiFi>\n"
     << "\t\t<Speaker>\n"
     << "\t\t\t<PlaybackPCM>" << parsed.speaker_pcm << "</PlaybackPCM>\n"
     << "\t\t</Speaker>\n"
     << "\t\t<Headphone>\n"
     << "\t\t\t<PlaybackPCM>" << parsed.headphone_pcm << "</PlaybackPCM>\n"
     << "\t\t\t<PlaybackMixerElem>" << parsed.headphone_mixer_elem
     << "</PlaybackMixerElem>\n"
     << "\t\t\t<JackDev>" << parsed.headphone_jack_dev << "</JackDev>\n"
     << "\t\t\t<JackSwitch>" << parsed.headphone_jack_switch
     << "</JackSwitch>\n"
     << "\t\t</Headphone>\n"
     << "\t\t<InternalMic>\n"
     << "\t\t\t<CapturePCM>" << parsed.internal_mic_pcm << "</CapturePCM>\n"
     << "\t\t\t<CaptureChannelMap>" << parsed.internal_mic_channel_map
     << "</CaptureChannelMap>\n"
     << "\t\t\t<IntrinsicSensitivity>" << parsed.internal_mic_sensitivity
     << "</IntrinsicSensitivity>\n"
     << "\t\t</InternalMic>\n"
     << "\t\t<Mic>\n"
     << "\t\t\t<CapturePCM>" << parsed.headset_mic_pcm << "</CapturePCM>\n"
     << "\t\t\t<CaptureMixerElem>" << parsed.headset_mic_mixer_elem
     << "</CaptureMixerElem>\n"
     << "\t\t\t<JackDev>" << parsed.headset_mic_jack_dev << "</JackDev>\n"
     << "\t\t</Mic>\n";
  for (int i = 1; i <= 3; ++i) {
    int pcm = i + 4;  // HDMI1..3 -> PCM 5..7
    ac << "\t\t<HDMI" << i << ">\n"
       << "\t\t\t<PlaybackPCM>hw:sofrt5682," << pcm << "</PlaybackPCM>\n"
       << "\t\t\t<JackDev>sof-rt5682 HDMI/DP,pcm=" << pcm << "</JackDev>\n"
       << "\t\t\t<PlaybackChannels>2</PlaybackChannels>\n"
       << "\t\t</HDMI" << i << ">\n";
  }
  ac << "\t\t<SCOLineOut>\n"
     << "\t\t\t<PlaybackPCM>hw:sofrt5682,2</PlaybackPCM>\n"
     << "\t\t</SCOLineOut>\n"
     << "\t\t<SCOLineIn>\n"
     << "\t\t\t<CapturePCM>hw:sofrt5682,2</CapturePCM>\n"
     << "\t\t</SCOLineIn>\n"
     << "\t</HiFi>\n"
     << "</audio_config>\n";
  out.audio_config_xml = ac.str();

  out.mixer_paths_xml = R"(<?xml version='1.0' encoding='utf-8'?>
<mixer enum_mixer_numeric_fallback="true">
	<!-- SCREEBO: SectionVerb EnableSequence of sof-rt5682.screebo/HiFi.conf (ALC5682I + ALC1019P, Meteor Lake) -->
	<ctl name="Stereo1 ADC L Mux" value="0" />
	<ctl name="STO1 ADC Capture Switch" value="1" />
	<ctl name="RECMIX1L CBJ Switch" value="1" />
	<ctl name="IF1 01 ADC Swap Mux" value="2" />
	<ctl name="CBJ Boost Volume" value="28" />
	<ctl name="Stereo1 ADC L1 Mux" value="1" />
	<ctl name="Stereo1 ADC R1 Mux" value="1" />
	<ctl name="Stereo1 ADC MIXL ADC2 Switch" value="0" />
	<ctl name="Stereo1 ADC MIXR ADC2 Switch" value="0" />
	<ctl name="Stereo1 ADC MIXL ADC1 Switch" value="1" />
	<ctl name="Stereo1 ADC MIXR ADC1 Switch" value="1" />
	<ctl name="DAC1 Playback Volume" value="127" />
	<ctl name="Dmic0 Capture Switch" value="1" />
	<ctl name="Left Spk Switch" value="0" />
	<ctl name="Right Spk Switch" value="0" />
	<ctl name="Headphone Jack Switch" value="0" />
	<ctl name="Stereo1 DAC MIXL DAC L1 Switch" value="0" />
	<ctl name="Stereo1 DAC MIXR DAC R1 Switch" value="0" />
	<ctl name="Headset Mic Switch" value="0" />
	<path name="Speaker">
		<ctl name="Left Spk Switch" value="1" />
		<ctl name="Right Spk Switch" value="1" />
	</path>
	<path name="Headphone">
		<ctl name="Headphone Jack Switch" value="1" />
		<ctl name="Stereo1 DAC MIXL DAC L1 Switch" value="1" />
		<ctl name="Stereo1 DAC MIXR DAC R1 Switch" value="1" />
	</path>
	<path name="InternalMic" />
	<path name="Mic">
		<ctl name="Headset Mic Switch" value="1" />
	</path>
	<path name="HDMI1" />
	<path name="HDMI2" />
	<path name="HDMI3" />
	<path name="SCOLineOut" />
	<path name="SCOLineIn" />
</mixer>
)";

  std::ostringstream vc;
  vc << "<?xml version='1.0' encoding='utf-8'?>\n"
     << "<volume_curves>\n"
     << "\t<reference name=\"Speaker\">\n";
  for (int pct = 0; pct <= 100; ++pct) {
    vc << "\t\t<point>" << pct << "," << parsed.speaker_db_at_pct.at(pct)
       << "</point>\n";
  }
  vc << "\t</reference>\n"
     << "\t<reference name=\"Headphone\">\n";
  for (int pct = 0; pct <= 100; ++pct) {
    int db = parsed.headphone_max_volume -
             parsed.headphone_volume_step * (100 - pct);
    vc << "\t\t<point>" << pct << "," << db << "</point>\n";
  }
  vc << "\t</reference>\n"
     << "</volume_curves>\n";
  out.volume_curves_xml =
      reference_volume_curves_xml.empty() ? vc.str() : vc.str();

  return out;
}

std::vector<uint8_t> GenerateTestToneWav1kHz(int duration_seconds) {
  const uint32_t sample_rate = 48000;
  const uint16_t num_channels = 2;
  const uint16_t bits_per_sample = 16;
  const uint32_t num_frames = sample_rate * duration_seconds;
  const uint32_t data_bytes = num_frames * num_channels * (bits_per_sample / 8);

  std::vector<uint8_t> wav;
  wav.reserve(44 + data_bytes);
  wav.insert(wav.end(), {'R', 'I', 'F', 'F'});
  AppendLe32(&wav, 36 + data_bytes);
  wav.insert(wav.end(), {'W', 'A', 'V', 'E', 'f', 'm', 't', ' '});
  AppendLe32(&wav, 16);
  AppendLe16(&wav, 1);  // PCM
  AppendLe16(&wav, num_channels);
  AppendLe32(&wav, sample_rate);
  AppendLe32(&wav, sample_rate * num_channels * (bits_per_sample / 8));
  AppendLe16(&wav, num_channels * (bits_per_sample / 8));
  AppendLe16(&wav, bits_per_sample);
  wav.insert(wav.end(), {'d', 'a', 't', 'a'});
  AppendLe32(&wav, data_bytes);

  const double pi = std::acos(-1.0);
  for (uint32_t i = 0; i < num_frames; ++i) {
    int16_t sample = 0;
    if (i >= 4800 && i < num_frames - 4800) {
      sample = static_cast<int16_t>(
          0.1 * 32767.0 * std::sin(2.0 * pi * 1000.0 * i / sample_rate));
    }
    AppendLe16(&wav, static_cast<uint16_t>(sample));
    AppendLe16(&wav, static_cast<uint16_t>(sample));
  }
  return wav;
}

}  // namespace googlebookos::audio
