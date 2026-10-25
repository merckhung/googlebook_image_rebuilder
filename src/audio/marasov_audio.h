#ifndef GOOGLEBOOKOS_SRC_AUDIO_MARASOV_AUDIO_H_
#define GOOGLEBOOKOS_SRC_AUDIO_MARASOV_AUDIO_H_

#include <filesystem>
#include <map>
#include <string>
#include <vector>

namespace googlebookos::audio {

struct MarasovAudioFiles {
  std::string audio_config_xml;
  std::string mixer_paths_xml;
  std::string volume_curves_xml;
};

struct ParsedUcmAndCardSettings {
  std::string sound_card_name = "hw:sofrt5682";
  std::string speaker_pcm = "hw:sofrt5682,0";
  std::string headphone_pcm = "hw:sofrt5682,1";
  std::string headphone_mixer_elem = "DAC1";
  std::string headphone_jack_dev = "sof-rt5682 Headset Jack";
  int headphone_jack_switch = 2;
  std::string internal_mic_pcm = "hw:sofrt5682,99";
  std::string internal_mic_channel_map = "2 3 -1 -1 -1 -1 -1 -1 -1 -1 -1";
  int internal_mic_sensitivity = -2600;
  std::string headset_mic_pcm = "hw:sofrt5682,1";
  std::string headset_mic_mixer_elem = "Headset Mic";
  std::string headset_mic_jack_dev = "sof-rt5682 Headset Jack";
  std::map<int, int> speaker_db_at_pct;  // 0..100 -> centibels (e.g. -250..-6400)
  int headphone_volume_step = 70;
  int headphone_max_volume = -650;
};

// Parses the ChromeOS marasov UCM (`HiFi.conf`) and CRAS (`sof-rt5682.card_settings`)
// files and verifies all expected PCMs, mixer controls, and 101-point speaker curves.
ParsedUcmAndCardSettings ParseMarasovAudioSources(
    const std::string& hifi_conf_text,
    const std::string& card_settings_text);

// Generates the three Android DRAS XML configurations (`audio_config.xml`,
// `mixer_paths.xml`, `volume_curves.xml`) for `sof-rt5682_marasov`.
MarasovAudioFiles GenerateMarasovDrasXmls(
    const ParsedUcmAndCardSettings& parsed,
    const std::string& reference_volume_curves_xml = "");

// Parses the ChromeOS screebo (ASUS ExpertBook CX54 CX5403CMA, Meteor Lake)
// UCM (`HiFi.conf`) and CRAS (`sof-rt5682.card_settings`) files and verifies
// all expected PCMs (Speaker=1, Headphone/Mic=0, SCO=2, HDMI1..3=5..7, DMIC=99)
// and 101-point speaker curves.
ParsedUcmAndCardSettings ParseScreeboAudioSources(
    const std::string& hifi_conf_text,
    const std::string& card_settings_text);

// Generates the three Android DRAS XML configurations (`audio_config.xml`,
// `mixer_paths.xml`, `volume_curves.xml`) for `sof-rt5682_screebo`.
MarasovAudioFiles GenerateScreeboDrasXmls(
    const ParsedUcmAndCardSettings& parsed,
    const std::string& reference_volume_curves_xml = "");

// Generates a 48 kHz 16-bit stereo 1 kHz test WAV file (matches kano-tone-1k.wav).
std::vector<uint8_t> GenerateTestToneWav1kHz(int duration_seconds = 3);

}  // namespace googlebookos::audio

#endif  // GOOGLEBOOKOS_SRC_AUDIO_MARASOV_AUDIO_H_
