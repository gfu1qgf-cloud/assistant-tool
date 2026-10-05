"""Narration remains uncut; speech-space EQ affects music only."""
import math


def audio_graph(settings, duration, voice_index=None, music_index=None, music=None, original_audio=False):
    graph, sources = [], []
    voice = voice_index is not None
    if voice:
        graph.append(f"[{voice_index}:a]asetpts=PTS-STARTPTS,aresample=48000,"
                     f"aformat=channel_layouts=stereo,volume={settings['voice_volume']/100},"
                     f"apad=whole_dur={duration:.9f},atrim=duration={duration:.9f}[voice]")
        sources.append("[voice]")
    if music_index is not None:
        volume = settings["voice_music_volume"] if voice else settings["music_volume"]
        chain = (f"[{music_index}:a]atrim=start={music['start']:.9f}:duration={duration:.9f},asetpts=PTS-STARTPTS,"
                 f"aresample=48000,aformat=channel_layouts=stereo,volume={volume/100}")
        if voice and settings["voice_eq"]:
            chain += f",equalizer=f={settings['eq_frequency']}:t=o:w={settings['eq_width']}:g={settings['eq_gain']}"
        fade = min(float(settings["fade_seconds"]), duration/2)
        chain += f",afade=t=in:d={fade:.9f},afade=t=out:st={duration-fade:.9f}:d={fade:.9f}"
        graph.append(chain + "[music]")
        if voice and settings["voice_duck"]:
            graph[-1] = graph[-1].replace("[music]", "[music_raw]")
            graph.append("[voice]asplit=2[voice_mix][voice_side]")
            sources[0] = "[voice_mix]"
            threshold = math.pow(10, float(settings["duck_threshold_db"])/20)
            graph.append(f"[music_raw][voice_side]sidechaincompress=threshold={threshold:.9f}:"
                         f"ratio={settings['duck_ratio']}:attack={settings['duck_attack_ms']}:"
                         f"release={settings['duck_release_ms']}:makeup=1:detection=rms[music]")
        sources.append("[music]")
    if original_audio and settings["keep_audio"] and not voice:
        graph.append("[0:a]asetpts=PTS-STARTPTS[original]")
        sources.append("[original]")
    if not sources:
        return graph, False
    if len(sources) > 1:
        graph.append("".join(sources) + f"amix=inputs={len(sources)}:duration=longest:normalize=0[mixed]")
        source = "[mixed]"
    else:
        source = sources[0]
    graph.append(source + f"alimiter=limit=0.95:level=false:latency=true,atrim=duration={duration:.9f}[a]")
    return graph, True
