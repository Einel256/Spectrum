#!/usr/bin/env python3
"""A small, dependency-free ANSI-256 audio visualizer for the terminal."""

from __future__ import annotations

import argparse
import array
import collections
import curses
import math
import os
import random
import struct
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


RATE = 44_100
FFT_SIZE = 4_096
HOP_SIZE = 512
MODE_NAMES = (
    "SPECTRUM", "WAVEFORM", "AURORA", "ORBIT", "GEOMETRY", "PARTICLES", "SPECTROGRAM",
    "VORTEX", "KALEIDOSCOPE", "LISSAJOUS", "RIPPLE", "MIRROR", "TUNNEL", "SPECTRAL RAIN",
)


MODE_COLORS = (255, 254, 240, 153, 147, 117, 111, 105)


class AudioReader:
    """Reads interleaved signed 16-bit stereo PCM from ffmpeg or parec."""

    def __init__(self, command: list[str], label: str):
        self.label = label
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.samples: collections.deque[float] = collections.deque([0.0] * (FFT_SIZE * 2), maxlen=FFT_SIZE * 2)
        self.lock = threading.Lock()
        self.eof = False
        self.error: str | None = None
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        pending = bytearray()
        while True:
            try:
                data = self.process.stdout.read(HOP_SIZE * 4)
            except OSError as error:
                self.error = str(error)
                break
            if not data:
                break
            pending.extend(data)
            usable = len(pending) - (len(pending) % 4)
            if not usable:
                continue
            pcm = array.array("h")
            pcm.frombytes(pending[:usable])
            del pending[:usable]
            if sys.byteorder != "little":
                pcm.byteswap()
            with self.lock:
                self.samples.extend(sample / 32768.0 for sample in pcm)
        self.eof = True
        self.returncode = self.process.wait()

    def latest(self) -> list[float]:
        with self.lock:
            return list(self.samples)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.process.stdout:
            self.process.stdout.close()


class CavaReader:
    """Read CAVA's stereo raw bars for the Spectrum display mode."""

    TOTAL_BARS = 96
    CHANNEL_BARS = TOTAL_BARS // 2

    def __init__(self, args: argparse.Namespace):
        executable = Path(__file__).with_name("cava")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            found = shutil.which("cava")
            if not found:
                raise RuntimeError("CAVA executable not found")
            executable = Path(found)

        self.temp_dir = tempfile.TemporaryDirectory(prefix="spectrum-cava-")
        fifo_path = Path(self.temp_dir.name) / "bars.fifo"
        config_path = Path(self.temp_dir.name) / "config"
        source = args.device or "auto"
        if args.mic and not args.device:
            try:
                source = subprocess.check_output(
                    ["pactl", "get-default-source"], text=True, timeout=2
                ).strip()
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError("could not resolve the default microphone for CAVA")
        config_path.write_text(
            "[general]\n"
            f"framerate = {args.framerate}\n"
            "autosens = 0\n"
            f"sensitivity = {args.sensitivity:g}\n"
            f"bars = {self.TOTAL_BARS}\n"
            f"lower_cutoff_freq = {args.lower_cutoff:g}\n"
            f"higher_cutoff_freq = {args.higher_cutoff:g}\n"
            "[input]\n"
            "method = pulse\n"
            f"source = {source}\n"
            "[output]\n"
            "method = raw\n"
            "channels = stereo\n"
            "data_format = binary\n"
            "bit_format = 16bit\n"
            f"raw_target = {fifo_path}\n",
            encoding="utf-8",
        )
        try:
            self.process = subprocess.Popen(
                [str(executable), "--config", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self.temp_dir.cleanup()
            raise
        try:
            deadline = time.monotonic() + 3.0
            while not fifo_path.exists():
                if self.process.poll() is not None:
                    raise RuntimeError("CAVA exited before creating its raw output")
                if time.monotonic() >= deadline:
                    raise RuntimeError("CAVA did not create its raw output")
                time.sleep(0.02)
            self.stream = os.fdopen(os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0)
        except Exception:
            self.process.terminate()
            self.process.wait(timeout=1.0)
            self.temp_dir.cleanup()
            raise

        self.lock = threading.Lock()
        self.latest_bands: tuple[list[float], list[float]] | None = None
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        frame_size = self.TOTAL_BARS * 2
        pending = bytearray()
        while True:
            try:
                chunk = self.stream.read(frame_size)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError:
                break
            if not chunk:
                if self.process.poll() is None:
                    time.sleep(0.01)
                    continue
                break
            pending.extend(chunk)
            usable = len(pending) - len(pending) % frame_size
            for offset in range(0, usable, frame_size):
                values = struct.unpack_from("<" + "H" * self.TOTAL_BARS, pending, offset)
                left = [value / 65535.0 for value in reversed(values[:self.CHANNEL_BARS])]
                right = [value / 65535.0 for value in values[self.CHANNEL_BARS:]]
                with self.lock:
                    self.latest_bands = left, right
            if usable:
                del pending[:usable]

    def latest(self) -> tuple[list[float], list[float]] | None:
        with self.lock:
            if self.latest_bands is None:
                return None
            left, right = self.latest_bands
            return left[:], right[:]

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.stream.close()
        self.thread.join(timeout=1.0)
        self.temp_dir.cleanup()


def radix2_fft(values: list[complex]) -> None:
    """In-place iterative Cooley-Tukey FFT."""
    n = len(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    size = 2
    while size <= n:
        angle = -2.0 * math.pi / size
        root = complex(math.cos(angle), math.sin(angle))
        half = size >> 1
        for start in range(0, n, size):
            factor = 1 + 0j
            for offset in range(half):
                even = values[start + offset]
                odd = factor * values[start + offset + half]
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                factor *= root
        size <<= 1


class Analyzer:
    def __init__(self, low_hz: float = 50.0, high_hz: float = 10_000.0,
                 sensitivity: float = 1.0) -> None:
        self.bands = 96
        # CAVA-style logarithmic frequency spacing gives bass notes more room
        # while keeping useful separation in the upper frequencies.
        self.low_hz = low_hz
        self.high_hz = min(high_hz, RATE * 0.49)
        self.sensitivity = sensitivity
        self.edges = [
            max(1, math.ceil((FFT_SIZE / RATE) * self.low_hz * (self.high_hz / self.low_hz) ** (i / self.bands)))
            for i in range(self.bands + 1)
        ]
        self.edges = [min(FFT_SIZE // 2, edge) for edge in self.edges]
        self.smooth_left = [0.0] * self.bands
        self.smooth_right = [0.0] * self.bands
        self.velocity_left = [0.0] * self.bands
        self.velocity_right = [0.0] * self.bands
        self.window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (FFT_SIZE - 1)) for i in range(FFT_SIZE)]

    def _analyze_channel(self, samples: list[float], smooth: list[float], velocity: list[float],
                         dt: float) -> list[float]:
        values = [complex(samples[i] * self.window[i], 0) for i in range(FFT_SIZE)]
        radix2_fft(values)
        result = []
        for band in range(self.bands):
            start = max(1, self.edges[band])
            end = max(start + 1, self.edges[band + 1])
            peak = max((abs(values[i]) for i in range(start, min(end, FFT_SIZE // 2))), default=0.0)
            db = 20 * math.log10(peak * 4 / FFT_SIZE + 1e-6)
            # A fixed edge EQ keeps bass and treble visible across different
            # mixes without changing gain based on recent overall loudness.
            edge_width = self.bands * 0.28
            low_boost = max(0.0, 1.0 - band / edge_width)
            high_boost = max(0.0, 1.0 - (self.bands - 1 - band) / edge_width)
            db += 5.0 + 8.0 * low_boost + 24.0 * high_boost
            center_bin = (start + max(start + 1, end)) * 0.5
            center_hz = center_bin * RATE / FFT_SIZE
            level = max(0.0, min(1.0, (db + 66.0) / 60.0))
            level *= self.sensitivity
            level = min(1.0, level)
            # Keep quiet signals in a narrower height range and progressively
            # widen the usable bar range as the signal gets louder.
            level = level ** 1.6
            # Ignore small FFT fluctuations so the bars do not shimmer at rest.
            level = round(level * 20) / 20
            # A critically damped spring eases bars into and out of each level.
            bass_weight = max(0.0, min(1.0, 1.0 - math.log2(max(center_hz, 150.0) / 150.0) / 2.0))
            # Stronger signals make rising bars react faster; quiet signals
            # retain the existing gentler attack.
            # A slower spring gives the bars visible acceleration and glide
            # instead of snapping toward each FFT update.
            base_omega = 20.0 + 12.0 * level if level >= smooth[band] else 8.0
            omega = base_omega * (1.0 + 0.2 * bass_weight)
            offset = smooth[band] - level
            decay = math.exp(-omega * dt)
            coefficient = velocity[band] + omega * offset
            smooth[band] = level + (offset + coefficient * dt) * decay
            velocity[band] = (velocity[band] - omega * coefficient * dt) * decay
            smooth[band] = max(0.0, min(1.0, smooth[band]))
            result.append(smooth[band])
        return result

    def analyze(self, samples: list[float], dt: float) -> tuple[list[float], list[float], list[float], list[float], float]:
        left = samples[0::2][-FFT_SIZE:]
        right = samples[1::2][-FFT_SIZE:]
        left = [0.0] * (FFT_SIZE - len(left)) + left
        right = [0.0] * (FFT_SIZE - len(right)) + right
        mono = [(l + r) * 0.5 for l, r in zip(left, right)]
        rms = math.sqrt(sum((l * l + r * r) * 0.5 for l, r in zip(left, right)) / FFT_SIZE)
        left_bands = self._analyze_channel(left, self.smooth_left, self.velocity_left, dt)
        right_bands = self._analyze_channel(right, self.smooth_right, self.velocity_right, dt)
        bands = [(l + r) * 0.5 for l, r in zip(left_bands, right_bands)]
        return left_bands, right_bands, bands, mono, rms


class Canvas:
    def __init__(self, width: int, height: int):
        self.width, self.height = max(2, width), max(4, height)
        self.pixels = bytearray(self.width * self.height)

    def set(self, x: int, y: int, intensity: float | int = 7) -> None:
        x, y = int(x), int(y)
        if 0 <= x < self.width and 0 <= y < self.height:
            self.pixels[y * self.width + x] = max(1, min(8, int(intensity)))

    def line(self, x0: float, y0: float, x1: float, y1: float, intensity: float | int = 7) -> None:
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx - dy
        while True:
            self.set(x0, y0, intensity)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled > -dy:
                error -= dy
                x0 += sx
            if doubled < dx:
                error += dx
                y0 += sy

    def polyline(self, points: list[tuple[float, float]], intensity: float | int = 7, closed: bool = False) -> None:
        for a, b in zip(points, points[1:]):
            self.line(a[0], a[1], b[0], b[1], intensity)
        if closed and len(points) > 2:
            self.line(*points[-1], *points[0], intensity)

    def circle(self, cx: float, cy: float, radius: float, intensity: float | int = 7, aspect: float = 1.0) -> None:
        count = max(16, int(radius * 5))
        previous = None
        first = None
        for i in range(count + 1):
            angle = 2 * math.pi * i / count
            point = (cx + math.cos(angle) * radius, cy + math.sin(angle) * radius * aspect)
            if first is None:
                first = point
            if previous is not None:
                self.line(*previous, *point, intensity)
            previous = point


def prepare_audio(args: argparse.Namespace) -> tuple[AudioReader, subprocess.Popen[bytes] | None]:
    player = None
    if args.file:
        filename = str(Path(args.file).expanduser().resolve())
        if not Path(filename).is_file():
            raise RuntimeError(f"Audio file not found: {filename}")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required for file input. Install ffmpeg and try again.")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-re", "-i", filename,
                   "-vn", "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", str(RATE), "pipe:1"]
        if not args.no_playback and shutil.which("ffplay"):
            player = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", filename],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return AudioReader(command, Path(filename).name), player

    if not shutil.which("parec"):
        raise RuntimeError("parec is required for live input. Install pipewire-pulse or PulseAudio.")
    device = args.device
    if not device:
        try:
            sources = subprocess.check_output(["pactl", "list", "short", "sources"], text=True, timeout=2)
            names = [line.split()[1] for line in sources.splitlines() if len(line.split()) > 1]
            if args.mic:
                device = subprocess.check_output(["pactl", "get-default-source"], text=True, timeout=2).strip()
            else:
                sink = subprocess.check_output(["pactl", "get-default-sink"], text=True, timeout=2).strip()
                device = next((name for name in names if name == f"{sink}.monitor"), None)
                device = device or next((name for name in names if name.endswith(".monitor")), None)
                if not device:
                    raise RuntimeError("No output monitor found. Use --device with a PulseAudio monitor source.")
        except (OSError, subprocess.SubprocessError, StopIteration) as error:
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError("Could not find an audio source. Check pactl and your PipeWire/PulseAudio session.") from error
    command = ["parec", "--device", device, "--format=s16le", f"--rate={RATE}", "--channels=2", "--latency-msec=35"]
    label = "MICROPHONE" if args.mic else f"SYSTEM AUDIO · {device}"
    return AudioReader(command, label), None


def current_output_label() -> str:
    """Return the friendly name for the current default output device."""
    try:
        sink = subprocess.check_output(["pactl", "get-default-sink"], text=True, timeout=2).strip()
        listing = subprocess.check_output(["pactl", "list", "sinks"], text=True, timeout=2)
        in_default_sink = False
        for line in listing.splitlines():
            if line.startswith("Sink #"):
                in_default_sink = False
            elif line.startswith("\tName: "):
                in_default_sink = line.split(": ", 1)[1] == sink
            elif in_default_sink and line.startswith("\tDescription: "):
                return line.split(": ", 1)[1].strip()
        return sink
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def draw_mode(canvas: Canvas, mode: int, bands: list[float], samples: list[float], rms: float,
              frame: int, animation_time: float, history: collections.deque[list[float]], effects: dict, detail: int,
              max_height: float = 1.0) -> None:
    w, h = canvas.width, canvas.height
    mid_y = h / 2
    bass = sum(bands[:10]) / 10
    mid = sum(bands[25:55]) / 30
    high = sum(bands[65:]) / max(1, len(bands[65:]))
    particles = effects["particles"]

    if mode == 1:  # Waveform
        previous = None
        stride = max(1, 11 - detail)
        for x in range(0, w, stride):
            index = int(x * (FFT_SIZE - 1) / max(1, w - 1))
            value = samples[index]
            y = mid_y - value * h * 0.44
            if previous:
                canvas.line(*previous, x, y, 7)
            previous = (x, y)
            canvas.set(x, mid_y, 2)
        return

    if mode == 2:  # Aurora ribbons
        for layer in range(2 + detail):
            points = []
            for x in range(w):
                energy = bands[int(x / max(1, w) * (len(bands) - 1))]
                slow_wave = math.sin(x * 0.035 + animation_time * 2.4 + layer * 0.52)
                flowing_wave = math.sin(x * 0.012 - animation_time * 1.15 + layer * 0.2)
                y = mid_y + slow_wave * (h * (0.10 + layer * 0.014))
                y += flowing_wave * h * 0.07 - energy * h * 0.25
                points.append((x, y))
            canvas.polyline(points, max(1, 7 - layer // 2))
        return

    if mode == 3:  # Orbit
        radius = min(w, h) * (0.20 + bass * 0.15)
        for ring in range(1, 2 + detail // 2):
            canvas.circle(w / 2, mid_y, radius * (0.55 + ring * 0.24), max(2, 8 - ring), 0.42)
        orbit_particles = 16 + detail * 8
        for i in range(orbit_particles):
            angle = 2 * math.pi * i / orbit_particles + frame * 0.025 * (1 if i % 2 else -1)
            energy = bands[(i * len(bands)) // orbit_particles]
            orbit = radius * (1.2 + energy * 1.2)
            canvas.set(w / 2 + math.cos(angle) * orbit, mid_y + math.sin(angle) * orbit * 0.42, 3 + int(5 * energy))
        return

    if mode == 4:  # Reactive geometry
        count = 4 + detail // 2
        for layer in range(2 + detail // 2):
            points = []
            for i in range(count):
                angle = math.pi * 2 * i / count + frame * (0.008 + layer * 0.002)
                energy = bands[(i * 13 + layer * 9) % len(bands)]
                radius = min(w, h) * (0.10 + layer * 0.045 + energy * 0.09)
                points.append((w / 2 + math.cos(angle) * radius, mid_y + math.sin(angle) * radius * 0.7))
            canvas.polyline(points, 3 + layer, closed=True)
            for i in range(count):
                canvas.line(*points[i], w / 2, mid_y, max(2, 7 - layer))
        return

    if mode == 5:  # Particles
        for particle in particles[:20 + detail * 18]:
            particle[0] += particle[2] * (0.5 + bass * 3.5)
            particle[1] += particle[3] * (0.5 + rms * 8)
            particle[4] *= 0.992
            if particle[0] < 0 or particle[0] >= w or particle[1] < 0 or particle[1] >= h or particle[4] < 0.08:
                particle[:] = [w / 2, mid_y, random.uniform(-2, 2), random.uniform(-1.5, 1.5), 1.0]
            canvas.set(particle[0], particle[1], 1 + int(particle[4] * 7))
            if particle[4] > 0.7:
                canvas.set(particle[0] + 1, particle[1], 3 + int(particle[4] * 5))
        return

    if mode == 6:  # Spectrogram
        if frame % 2 == 0:
            history.append(bands[:])
            while len(history) > w:
                history.popleft()
        visible_bands = 12 + int((detail - 1) * (len(bands) - 12) / 9)
        for x, column in enumerate(history):
            px = w - len(history) + x
            for y in range(h):
                band = len(bands) - 1 - int(y * visible_bands / h)
                band = max(0, band)
                value = column[band]
                if value > 0.035:
                    canvas.set(px, y, 1 + int(value * 7))
        return

    if mode == 7:  # Vortex: layered spirals flex with the spectrum
        arms = 1 + detail // 2
        for arm in range(arms):
            points = []
            turns = 2.4
            steps = max(60, int(w * (0.2 + detail * 0.08)))
            for i in range(steps):
                t = i / (steps - 1)
                angle = t * math.tau * turns + arm * math.tau / arms + frame * 0.018
                energy = bands[int(t * (len(bands) - 1))]
                radius = t * min(w, h) * 0.62 * (0.7 + energy * 0.48)
                points.append((w / 2 + math.cos(angle) * radius, mid_y + math.sin(angle) * radius * 0.48))
            canvas.polyline(points, 3 + arm)
        return

    if mode == 8:  # Kaleidoscope: mirrored spectral rays
        cx, cy = w / 2, mid_y
        radius = min(w, h) * 0.66
        rays = 8 + detail * 8
        for i in range(rays):
            energy = bands[i * len(bands) // rays]
            angle = (i / rays) * math.tau + frame * 0.008
            reach = radius * (0.15 + energy * 0.9)
            for mirror in range(8):
                a = angle + mirror * math.tau / 8
                x1, y1 = cx + math.cos(a) * radius * 0.12, cy + math.sin(a) * radius * 0.12 * 0.55
                x2, y2 = cx + math.cos(a) * reach, cy + math.sin(a) * reach * 0.55
                canvas.line(x1, y1, x2, y2, 2 + int(6 * energy))
        return

    if mode == 9:  # Lissajous oscilloscope
        points = []
        stride = max(1, int((FFT_SIZE // 2) / max(40, w * (0.2 + detail * 0.08))))
        for i in range(0, FFT_SIZE // 2, stride):
            x = w / 2 + samples[i] * w * 0.62
            y = mid_y - samples[i + FFT_SIZE // 2] * h * 0.62
            points.append((x, y))
        canvas.polyline(points, 7)
        return

    if mode == 10:  # Beat-triggered expanding ripples
        previous = effects["previous_rms"]
        if rms > max(0.025, previous * 1.24) and frame - effects["last_beat"] > 5:
            effects["ripples"].append([0.0, 1.0])
            effects["last_beat"] = frame
        effects["previous_rms"] = rms
        ripples = effects["ripples"]
        if frame % max(2, 12 - detail) == 0 and len(effects["ripples"]) < detail * 2:
            ripples.append([min(w, h) * (0.12 + bass * 0.16), 0.45 + bass])
        for ring in ripples:
            canvas.circle(w / 2, mid_y, ring[0], max(1, int(ring[1] * 8)), 0.56)
            ring[0] += 1.5 + ring[1] * 3
            ring[1] *= 0.965
        ripples[:] = [ring for ring in ripples if ring[0] < min(w, h) * 0.62 and ring[1] > 0.08]
        return

    if mode == 11:  # Mirror spectrum, growing out from the center
        half = w // 2
        bars = min(half, 8 + detail * 8)
        for bar in range(bars):
            x0 = int(bar * half / bars)
            x1 = int((bar + 1) * half / bars)
            band = bands[min(len(bands) - 1, bar * len(bands) // bars)]
            extent = int((0.025 + band * 0.92) * h / 2)
            for y in range(int(mid_y - extent), int(mid_y + extent)):
                level = 2 + int(6 * (1 - abs(y - mid_y) / max(1, extent)))
                for x in range(x0, max(x0 + 1, x1 - (1 if x1 - x0 > 2 else 0))):
                    canvas.set(half - x, y, level)
                    canvas.set(half + x, y, level)
        return

    if mode == 12:  # Tunnel: nested polygons rotate toward a vanishing point
        cx, cy = w / 2, mid_y
        sides = 4
        for layer in range(2 + detail):
            scale = 0.08 + layer * 0.085
            points = []
            for i in range(sides):
                angle = math.tau * i / sides + frame * (0.006 + layer * 0.0007)
                energy = bands[(i * 17 + layer * 7) % len(bands)]
                radius = min(w, h) * scale * (0.8 + energy * 0.5)
                points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius * 0.58))
            canvas.polyline(points, 2 + (layer % 6), closed=True)
        return

    # Spectral rain: short frequency bars scroll downward through a field.
    rain = effects["rain"]
    if frame % max(1, 12 - detail) == 0:
        rain.append(bands[:])
        while len(rain) > h:
            rain.popleft()
    visible_bands = 12 + int((detail - 1) * (len(bands) - 12) / 9)
    for y, row in enumerate(rain):
        py = h - len(rain) + y
        for x in range(w):
            band = min(len(bands) - 1, int(x / max(1, w) * visible_bands))
            value = row[band]
            if value > 0.07 and (x + frame // 3) % 5 < 2:
                canvas.set(x, py, 1 + int(value * 7))


def setup_pairs(stdscr: curses.window) -> None:
    for pair, color in enumerate(MODE_COLORS, start=1):
        curses.init_pair(pair, color, -1)
    curses.init_pair(9, 233, -1)


def enhance_cava_bands(bands: list[float]) -> list[float]:
    """Keep the bass and treble emphasis used by the built-in analyzer."""
    edge_width = len(bands) * 0.28
    enhanced = []
    for index, value in enumerate(bands):
        low_boost = max(0.0, 1.0 - index / edge_width)
        high_boost = max(0.0, 1.0 - (len(bands) - 1 - index) / edge_width)
        lift = 0.08 + 0.10 * low_boost + 0.22 * high_boost
        gain = 1.25 + 0.5 * low_boost + 2.0 * high_boost
        enhanced.append(min(1.0, value * gain))
    return enhanced


def draw_statusline(stdscr: curses.window, cols: int, mode: int, output_device: str,
                    overlay: bool = False) -> None:
    try:
        stdscr.move(0, 0)
        if not overlay:
            stdscr.clrtoeol()
        style = curses.color_pair(9)
        x = 0

        def segment(label: str) -> None:
            nonlocal x
            # Keep adjacent status segments visually distinct in the compact
            # overlay too (for example, "MODE SPECTRUM").
            value = f"{label} " if overlay else f" {label} "
            if x + len(value) < cols:
                stdscr.addstr(0, x, value, style)
                x += len(value)

        segment("MODE")
        segment(MODE_NAMES[mode])
        label_prefix = "OUTPUT " if overlay else " OUTPUT  "
        name_width = max(0, cols - x - len(label_prefix) - 1)
        name = output_device if len(output_device) <= name_width else output_device[:max(0, name_width - 1)] + "…"
        right = f"{label_prefix}{name}" if overlay else f"{label_prefix}{name} "
        start = max(x, cols - len(right) - 1)
        if start > x:
            stdscr.addstr(0, x, " " * (start - x), style)
        if start + len(right) < cols:
            stdscr.addstr(0, start, right, style)
    except curses.error:
        pass


def render(stdscr: curses.window, canvas: Canvas) -> None:
    # Braille has two horizontal by four vertical dots per terminal cell.
    masks = ((0, 1), (1, 2), (2, 4), (3, 64), (4, 8), (5, 16), (6, 32), (7, 128))
    for cy in range(canvas.height // 4):
        for cx in range(canvas.width // 2):
            bits = 0
            strongest = 0
            for offset, mask in masks:
                py = cy * 4 + (offset % 4)
                px = cx * 2 + (offset // 4)
                value = canvas.pixels[py * canvas.width + px]
                if value:
                    bits |= mask
                    strongest = max(strongest, value)
            if strongest:
                try:
                    stdscr.addstr(cy + 1, cx, chr(0x2800 + bits), curses.color_pair(strongest))
                except curses.error:
                    pass


def render_spectrum(stdscr: curses.window, left_bands: list[float], right_bands: list[float],
                    max_height: float, rows: int, cols: int) -> None:
    """Draw independent left and right channel spectra in separate panels."""
    top = 0
    bottom = rows
    plot_height = max(1, bottom - top)
    # Match the original proportions: two columns per bar and one empty column.
    bar_width = 2
    bar_gap = 1
    pitch = bar_width + bar_gap
    divider = cols // 2
    panels = ((0, divider, left_bands, "L"),
              (divider + 1, cols - divider - 1, right_bands, "R"))
    glyphs = " ▁▂▃▄▅▆▇█"
    style = curses.color_pair(1)
    idle_style = curses.color_pair(1) | curses.A_DIM

    for panel_x, panel_width, bands, label in panels:
        if panel_width < 1:
            continue
        count = max(1, (panel_width + bar_gap) // pitch)
        left_aligned_offset = panel_width - ((count - 1) * pitch + bar_width)
        levels = []
        for index in range(count):
            frequency_index = count - 1 - index if label == "L" else index
            band_start = frequency_index * len(bands) // count
            band_end = max(band_start + 1, (frequency_index + 1) * len(bands) // count)
            levels.append(sum(bands[band_start:band_end]) / (band_end - band_start))

        # Blend adjacent bars lightly so the connected columns form a smooth
        # staircase while each frequency band still has its own movement.
        smooth_levels = [
            levels[index] * 0.8
            + levels[max(0, index - 1)] * 0.1
            + levels[min(count - 1, index + 1)] * 0.1
            for index in range(count)
        ]
        for index, band_level in enumerate(smooth_levels):
            height = max(0.0, min(1.0, band_level * max_height)) * plot_height
            eighths = round(height * 8)
            full_cells, remainder = divmod(eighths, 8)
            x = panel_x + left_aligned_offset + index * pitch if label == "L" else panel_x + index * pitch
            if x + bar_width <= panel_x + panel_width:
                try:
                    stdscr.addstr(bottom - 1, x, "▁" * bar_width, idle_style)
                except curses.error:
                    pass

            # Ignore the analyzer's quiet tail and leave only the faint baseline.
            if band_level < 0.025:
                continue
            for offset in range(full_cells):
                y = bottom - 1 - offset
                if top <= y < bottom and x + bar_width <= panel_x + panel_width:
                    try:
                        stdscr.addstr(y, x, "█" * bar_width, style)
                    except curses.error:
                        pass
            if remainder and full_cells < plot_height:
                y = bottom - 1 - full_cells
                if top <= y < bottom and x + bar_width <= panel_x + panel_width:
                    try:
                        stdscr.addstr(y, x, glyphs[remainder] * bar_width, style)
                    except curses.error:
                        pass


def run_tui(stdscr: curses.window, reader: AudioReader, player: subprocess.Popen[bytes] | None,
            args: argparse.Namespace, cava_reader: CavaReader | None = None) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    if not curses.has_colors():
        raise RuntimeError("This terminal does not support ANSI colors.")
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    mode = 0
    setup_pairs(stdscr)
    analyzer = Analyzer(args.lower_cutoff, args.higher_cutoff, args.sensitivity / 100.0)
    history: collections.deque[list[float]] = collections.deque()
    effects = {
        "particles": [[0.0, 0.0, random.uniform(-1.8, 1.8), random.uniform(-1.3, 1.3), random.random()]
                      for _ in range(200)],
        "ripples": [], "rain": collections.deque(), "previous_rms": 0.0, "last_beat": -100,
    }
    frame = 0
    last_left_bands = [0.0] * analyzer.bands
    last_right_bands = [0.0] * analyzer.bands
    last_bands = [0.0] * analyzer.bands
    last_samples = [0.0] * FFT_SIZE
    last_rms = 0.0
    detail = 7
    last_tick = time.monotonic()
    animation_started = last_tick
    target_frame = 1 / args.framerate
    output_label = current_output_label()
    output_label_checked = last_tick

    while True:
        rows, cols = stdscr.getmaxyx()
        if rows < 9 or cols < 20:
            stdscr.erase()
            stdscr.addstr(0, 0, "Resize terminal · q to quit")
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                break
            time.sleep(0.03)
            continue

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        elif ord("1") <= key <= ord("9"):
            mode = key - ord("1")
        elif ord("a") <= key <= ord("e"):
            mode = 9 + key - ord("a")
        elif key in (curses.KEY_RIGHT, ord("n")):
            mode = (mode + 1) % len(MODE_NAMES)
        elif key in (curses.KEY_LEFT, ord("p")):
            mode = (mode - 1) % len(MODE_NAMES)
        now = time.monotonic()
        if now - output_label_checked >= 5:
            output_label = current_output_label()
            output_label_checked = now
        dt = min(0.05, now - last_tick)
        last_tick = now
        interleaved_samples = reader.latest()
        cava_bands = cava_reader.latest() if mode == 0 and cava_reader else None
        if cava_bands is not None:
            last_left_bands, last_right_bands = cava_bands
            last_left_bands = enhance_cava_bands(last_left_bands)
            last_right_bands = enhance_cava_bands(last_right_bands)
            last_bands = [(left + right) * 0.5 for left, right in zip(last_left_bands, last_right_bands)]
        else:
            last_left_bands, last_right_bands, last_bands, last_samples, last_rms = analyzer.analyze(
                interleaved_samples, dt
            )
        frame += 1

        stdscr.erase()
        try:
            available_rows = rows - 1
            if mode == 0:
                render_spectrum(stdscr, last_left_bands, last_right_bands, args.max_height / 100.0,
                                rows, cols)
                # Keep the spectrum running behind the status text; draw labels last.
                draw_statusline(stdscr, cols, mode, output_label, overlay=True)
            else:
                draw_statusline(stdscr, cols, mode, output_label)
                canvas = Canvas(cols * 2, available_rows * 4)
                draw_mode(canvas, mode, last_bands, last_samples, last_rms, frame,
                          now - animation_started, history, effects, detail,
                          args.max_height / 100.0)
                render(stdscr, canvas)
        except curses.error:
            pass
        stdscr.refresh()
        remaining = target_frame - (time.monotonic() - now)
        if remaining > 0:
            time.sleep(remaining)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualize system audio in your terminal with fourteen ANSI 256-color modes.",
        epilog="Examples: python spectrum.py | python spectrum.py --live",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("file", nargs="?", help="audio file to play and visualize")
    source.add_argument("--live", action="store_true", help="visualize currently playing system audio")
    source.add_argument("--mic", action="store_true", help="visualize the default microphone input")
    parser.add_argument("--device", help="explicit PulseAudio/PipeWire source name (parec)")
    parser.add_argument("--no-playback", action="store_true", help="analyze a file without playing it")
    parser.add_argument("--sensitivity", type=float, default=100, metavar="PERCENT",
                        help="spectrum sensitivity in percent (default: 100)")
    parser.add_argument("--max-height", type=int, default=100, metavar="PERCENT",
                        help="maximum spectrum bar height (default: 100)")
    parser.add_argument("--lower-cutoff", type=float, default=50, metavar="HZ",
                        help="lowest spectrum frequency (default: 50 Hz)")
    parser.add_argument("--higher-cutoff", type=float, default=10_000, metavar="HZ",
                        help="highest spectrum frequency (default: 10000 Hz)")
    parser.add_argument("--framerate", type=int, default=144, metavar="FPS",
                        help="target redraw rate (default: 144; maximum: 144)")
    parser.add_argument("--version", action="version", version="Spectrum TUI 1.0.0")

    args = parser.parse_args()
    if args.sensitivity < 0 or not 1 <= args.max_height <= 100 or not 1 <= args.framerate <= 144:
        parser.error("sensitivity must be non-negative; max-height must be 1-100; framerate must be 1-144")
    if args.lower_cutoff <= 0 or args.higher_cutoff <= args.lower_cutoff or args.higher_cutoff >= RATE / 2:
        parser.error(f"cutoffs must satisfy 0 < lower < higher < {RATE / 2:g} Hz")
    if args.device and args.file:
        parser.error("--device is only valid with --live or --mic")
    return args


def main() -> int:
    args = parse_args()
    reader = None
    player = None
    cava_reader = None
    try:
        reader, player = prepare_audio(args)
        if not (args.file and (args.no_playback or player is None)):
            try:
                cava_reader = CavaReader(args)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"spectrum: CAVA unavailable ({error}); using built-in spectrum analyzer", file=sys.stderr)
        curses.wrapper(run_tui, reader, player, args, cava_reader)
    except KeyboardInterrupt:
        pass
    except (RuntimeError, OSError, curses.error) as error:
        print(f"spectrum: {error}", file=sys.stderr)
        return 1
    finally:
        if reader:
            reader.close()
        if cava_reader:
            cava_reader.close()
        if player and player.poll() is None:
            player.send_signal(signal.SIGTERM)
            try:
                player.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                player.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
