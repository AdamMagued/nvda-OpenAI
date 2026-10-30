"""
Audio utilities - silence trimming, resampling for voice recordings.
Uses only Python standard library (wave, struct).
"""
import os
import struct
import tempfile
import wave

from logHandler import log

from .consts import MIN_SAMPLES_FOR_TRIM, TEMP_DIR, ensure_temp_dir

# Target for voice: 16 kHz mono, ~32 KB/sec (reasonable for speech APIs)
VOICE_TARGET_RATE = 16000


def write_pcm16_wav(path: str, data: bytes, sample_rate: int = 24000, channels: int = 1) -> None:
	"""Write raw 16-bit little-endian PCM samples as a playable WAV file."""
	with wave.open(path, "wb") as wav:
		wav.setnchannels(channels)
		wav.setsampwidth(2)
		wav.setframerate(sample_rate)
		wav.writeframes(data)


def downsample_to_voice_wav(input_path: str, target_rate: int = VOICE_TARGET_RATE) -> str:
	"""
	Downsample WAV to target_rate mono 16-bit. Reduces file size for voice
	(e.g. 48kHz stereo 3 sec ~576KB -> 16kHz mono ~96KB).
	Returns input_path if already OK or on error.
	"""
	if not os.path.exists(input_path) or os.path.getsize(input_path) < 100:
		return input_path
	try:
		with wave.open(input_path, "rb") as wav_in:
			nchannels = wav_in.getnchannels()
			sampwidth = wav_in.getsampwidth()
			framerate = wav_in.getframerate()
			nframes = wav_in.getnframes()
			if sampwidth != 2 or (framerate == target_rate and nchannels == 1):
				return input_path
			frames = wav_in.readframes(nframes)
	except Exception as e:
		log.debug(f"downsample_to_voice: could not read {input_path}: {e}")
		return input_path

	all_samples = list(struct.unpack(f"<{len(frames)//2}h", frames))
	if nchannels == 2:
		samples = [(all_samples[i] + all_samples[i + 1]) // 2 for i in range(0, len(all_samples), 2)]
	else:
		samples = all_samples

	if framerate == target_rate:
		out_samples = samples
	else:
		ratio = framerate / target_rate
		out_len = int(len(samples) / ratio)
		out_samples = [samples[int(i * ratio)] for i in range(out_len)]

	frame_data = struct.pack(f"<{len(out_samples)}h", *out_samples)
	ensure_temp_dir()
	fd, write_path = tempfile.mkstemp(suffix=".wav", dir=TEMP_DIR)
	os.close(fd)
	try:
		with wave.open(write_path, "wb") as wav_out:
			wav_out.setnchannels(1)
			wav_out.setsampwidth(2)
			wav_out.setframerate(target_rate)
			wav_out.writeframes(frame_data)
		os.replace(write_path, input_path)
		return input_path
	except Exception as e:
		log.debug(f"downsample_to_voice: could not write: {e}")
		if os.path.exists(write_path):
			try:
				os.remove(write_path)
			except Exception:
				pass
		return input_path


def trim_silence_wav(
	input_path: str,
	output_path: str = None,
	min_silence_sec: float = 2.0,
	threshold_ratio: float = 0.02,
	window_ms: int = 30,
) -> str:
	"""
	Remove silence from WAV file:
	- Trim leading and trailing silence (any length)
	- Remove silence segments longer than min_silence_sec in the middle

	Returns output_path if successful, input_path if skipped/failed.
	Uses 16-bit PCM only.
	"""
	if output_path is None:
		output_path = input_path
	if not os.path.exists(input_path) or os.path.getsize(input_path) < 100:
		return input_path
	try:
		with wave.open(input_path, "rb") as wav_in:
			nchannels = wav_in.getnchannels()
			sampwidth = wav_in.getsampwidth()
			framerate = wav_in.getframerate()
			nframes = wav_in.getnframes()
			if sampwidth != 2:
				return input_path
			frames = wav_in.readframes(nframes)
	except Exception as e:
		log.debug(f"trim_silence: could not read {input_path}: {e}")
		return input_path

	all_samples = list(struct.unpack(f"<{len(frames)//2}h", frames))
	# For classification: mono (avg of L/R) or mono as-is
	if nchannels == 2:
		samples = [(all_samples[i] + all_samples[i+1]) // 2 for i in range(0, len(all_samples), 2)]
	else:
		samples = all_samples
	nsamples = len(samples)
	if nsamples < int(framerate * MIN_SAMPLES_FOR_TRIM):
		return input_path

	window_size = max(1, framerate * window_ms // 1000)
	threshold = max(100, int(32768 * threshold_ratio))
	min_silence_samples = int(framerate * min_silence_sec)

	# Classify each window as silence or speech
	is_silence = []
	for i in range(0, nsamples, window_size):
		chunk = samples[i:i+window_size]
		if not chunk:
			break
		peak = max(abs(s) for s in chunk)
		is_silence.append(peak < threshold)

	# Build kept intervals: trim edges + remove long middle silence
	keep_start = 0
	keep_end = len(is_silence)
	for i, sil in enumerate(is_silence):
		if not sil:
			keep_start = i
			break
	for i in range(len(is_silence) - 1, -1, -1):
		if not is_silence[i]:
			keep_end = i + 1
			break

	if keep_start >= keep_end:
		return input_path

	# Middle: merge speech segments, drop silence > min_silence_sec
	segments = []
	in_speech = False
	speech_start = 0
	silence_start = 0
	for i in range(keep_start, keep_end):
		if is_silence[i]:
			if in_speech:
				in_speech = False
				silence_start = i
			else:
				silence_len = (i - silence_start + 1) * window_size
				if silence_len >= min_silence_samples:
					segments.append((speech_start, silence_start))
					speech_start = i + 1
		else:
			if not in_speech:
				in_speech = True
				silence_start = i
	if in_speech:
		segments.append((speech_start, keep_end))

	if not segments:
		return input_path

	# Convert window indices to sample indices and extract from original
	out_samples = []
	for start_win, end_win in segments:
		start_samp = start_win * window_size
		end_samp = min(end_win * window_size, nsamples)
		if nchannels == 2:
			for i in range(start_samp, end_samp):
				idx = i * 2
				if idx + 1 < len(all_samples):
					out_samples.append(all_samples[idx])
					out_samples.append(all_samples[idx + 1])
		else:
			for i in range(start_samp, end_samp):
				if i < len(all_samples):
					out_samples.append(all_samples[i])

	if not out_samples:
		return input_path

	frame_data = struct.pack(f"<{len(out_samples)}h", *out_samples)

	write_path = output_path
	if output_path == input_path:
		ensure_temp_dir()
		fd, write_path = tempfile.mkstemp(suffix=".wav", dir=TEMP_DIR)
		os.close(fd)
	try:
		with wave.open(write_path, "wb") as wav_out:
			wav_out.setnchannels(nchannels)
			wav_out.setsampwidth(sampwidth)
			wav_out.setframerate(framerate)
			wav_out.writeframes(frame_data)
		if write_path != input_path:
			os.replace(write_path, input_path)
		return input_path if output_path == input_path else write_path
	except Exception as e:
		log.debug(f"trim_silence: could not write: {e}")
		if write_path != input_path and os.path.exists(write_path):
			try:
				os.remove(write_path)
			except Exception:
				pass
		return input_path
