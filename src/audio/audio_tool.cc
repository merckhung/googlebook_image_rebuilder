#include <fstream>
#include <iostream>
#include <sstream>

#include "src/audio/marasov_audio.h"

namespace {
std::string ReadText(const std::string& p) {
  std::ifstream f(p);
  if (!f) throw std::runtime_error("Cannot open: " + p);
  std::ostringstream ss;
  ss << f.rdbuf();
  return ss.str();
}
}  // namespace

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "Usage: audio_tool <HiFi.conf> <sof-rt5682.card_settings>\n";
    return 2;
  }
  try {
    auto parsed = googlebookos::audio::ParseMarasovAudioSources(
        ReadText(argv[1]), ReadText(argv[2]));
    auto xmls = googlebookos::audio::GenerateMarasovDrasXmls(parsed);
    std::cout << "SoundCardName: " << parsed.sound_card_name << "\n"
              << "Speaker PCM:   " << parsed.speaker_pcm << "\n"
              << "Headphone PCM: " << parsed.headphone_pcm << "\n"
              << "DMIC PCM:      " << parsed.internal_mic_pcm << " (chmap "
              << parsed.internal_mic_channel_map << ")\n"
              << "Generated XML sizes: audio_config="
              << xmls.audio_config_xml.size()
              << " mixer_paths=" << xmls.mixer_paths_xml.size()
              << " volume_curves=" << xmls.volume_curves_xml.size() << "\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "audio_tool error: " << e.what() << "\n";
    return 1;
  }
}
