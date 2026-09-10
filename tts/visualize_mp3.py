#!/usr/bin/env python3
import argparse, os, subprocess, tempfile
import numpy as np
import cv2
import librosa
import librosa.display

# ---------- 绘制核心 ----------
def draw_bars(img, mag, width, height):
    bars = 64
    h, w = img.shape[:2]
    segment = int(w / bars)
    for i, v in enumerate(mag):
        bar_h = int(v * h * 0.8)
        cv2.rectangle(img, (i * segment, h - bar_h),
                      ((i + 1) * segment - 2, h),
                      (80, 200, 255), -1)

def draw_wave(img, wave, width, height):
    h, w = img.shape[:2]
    pts = []
    for x, y in enumerate(wave):
        px = int(x / (len(wave) - 1) * w)
        py = int(h / 2 + y * h * 0.4)
        pts.append((px, py))
    cv2.polylines(img, [np.array(pts)], False, (0, 255, 180), 2, cv2.LINE_AA)

def draw_circle(img, mag, width, height):
    center = (width // 2, height // 2)
    max_radius = min(width, height) // 2 - 20
    for i, v in enumerate(mag):
        radius = int(v * max_radius)
        color = (int(255 - i * 20), int(120 + i * 10), 255)
        cv2.circle(img, center, radius, color, 2, cv2.LINE_AA)

def draw_particle(img, energy, width, height, rng):
    count = int(energy * 60)
    for _ in range(count):
        x = int(width/2 + (rng.random() - 0.5) * width * 0.6)
        y = int(height/2 + (rng.random() - 0.5) * height * 0.6)
        r = int(2 + rng.random() * 4)
        cv2.circle(img, (x, y), r, (100, 255, 255), -1)

# ---------- 主流程 ----------
def generate(audio_path, subtitle_path, output_path, style,
             width=1280, height=720, fps=30, font_size=36):
    # 1. 读取音频与STFT
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    hop = 256
    D = librosa.stft(y, n_fft=1024, hop_length=hop)
    mag_db = librosa.amplitude_to_db(np.abs(D), ref=np.max)
    mag_norm = np.clip((mag_db + 60) / 60, 0, 1)          # 0~1
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms = rms / (rms.max() + 1e-9)

    # 2. 无音频视频缓存
    tmp_video = tempfile.mktemp(suffix='.mp4')
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(tmp_video, fourcc, fps, (width, height))

    duration = len(y) / sr
    frames = int(duration * fps)
    rng = np.random.default_rng(42)
    win = int(sr / fps)                                  # 每帧样本长度

    for t in range(frames):
        start = int(t * win)
        idx = int(t / fps * sr / hop)                    # STFT帧索引
        idx = min(idx, mag_norm.shape[1] - 1)

        img = np.zeros((height, width, 3), dtype=np.uint8)
        img[:] = (24, 20, 25)                            # 深色背景

        mag_slice = mag_norm[:, idx]
        if style == 'bars':
            # 将全频谱压缩到64段
            seg = np.array([mag_slice[i::64].mean() for i in range(64)])
            draw_bars(img, seg, width, height)
        elif style == 'wave':
            seg = y[start:start + win] if start + win < len(y) else y[start:]
            seg = seg / (np.abs(seg).max() + 1e-9)
            draw_wave(img, seg, width, height)
        elif style == 'circle':
            rings = 24
            vals = np.array([mag_slice[i::rings].mean() for i in range(rings)])
            draw_circle(img, vals, width, height)
        elif style == 'particle':
            e = rms[min(idx, len(rms)-1)]
            draw_particle(img, e, width, height, rng)

        out.write(img)
    out.release()

    # 3. 合成音频 + 烧录字幕
    cmd = [
        'ffmpeg', '-y',
        '-i', tmp_video,
        '-i', audio_path,
        '-vf', f"subtitles='{subtitle_path}':force_style='FontSize={font_size},MarginV=40'",
        '-c:v', 'libx264', '-preset', 'medium', '-crf', '20',
        '-c:a', 'aac', '-b:a', '192k',
        '-shortest', output_path
    ]
    subprocess.run(cmd, check=True)
    os.remove(tmp_video)

# ---------- CLI ----------
if __name__ == '__main__':
    p = argparse.ArgumentParser(description='MP3 → 可视化 MP4（含字幕）')
    p.add_argument('--audio', required=True, help='输入MP3文件')
    p.add_argument('--subtitle', required=True, help='SRT字幕文件')
    p.add_argument('--output', required=True, help='输出MP4路径')
    p.add_argument('--style', choices=['bars', 'wave', 'circle', 'particle'],
                   default='bars', help='可视化风格')
    p.add_argument('--width', type=int, default=1280)
    p.add_argument('--height', type=int, default=720)
    p.add_argument('--fps', type=int, default=30)
    p.add_argument('--font-size', type=int, default=18,
                   help='字幕字号（按分辨率自动适配）')
    args = p.parse_args()

    generate(args.audio, args.subtitle, args.output, args.style,
             args.width, args.height, args.fps, args.font_size)
    print(f'完成：{args.output}')
