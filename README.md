# Spectrum TUI

karl氏の「cava」をお借りしています
https://github.com/karlstav/cava

音楽に反応する14種類のビジュアライザーを、ANSI 256色のターミナルに表示する小さなツールです。スペクトラムはステレオの左右チャンネルを別々に解析し、画面を左右に分けて表示します。ほかのモードはBraille文字で描画します。

## 起動

```sh
python3 spectrum.py
```

Python 3.10 以降が必要です。Pythonパッケージへの依存はありません。リポジトリを取得した後、プロジェクトのディレクトリで実行してください。

引数なしで起動すると、PipeWire / PulseAudio の出力モニターを自動選択してシステム音声を表示します。音楽プレーヤーやブラウザーで音を再生してから起動してください。ライブ入力には `parec` と `pactl` が使える PipeWire の PulseAudio 互換 (`pipewire-pulse`) または PulseAudio が必要です。

起動時の色選択機能はありません。スペクトラムはANSI 255の単色で、それ以外のモードは固定グラデーションを使います。

音源ファイルを直接再生・表示することもできます。その場合、再生には `ffplay`、解析には `ffmpeg` が必要です。

```sh
python3 spectrum.py ./music.flac
```

```sh
python3 spectrum.py --live       # 引数なしの場合と同じ
python3 spectrum.py --mic        # デフォルトマイクを表示
python3 spectrum.py --device SOURCE_NAME --live
```

スペクトラムはCAVAのraw出力を使って50 Hz〜10,000 Hzを対数間隔で解析し、このアプリのANSI表示で描画します。低音と高音を強調し、2文字幅・1文字間隔の棒で表示します。CAVAが使えない場合は内蔵解析へ切り替えます。その他のモードは内蔵解析を使います。既定144 fpsで、感度・周波数範囲・バーの高さ・描画速度を調整できます。CAVAは任意の外部プログラムです。利用する場合はOSのパッケージマネージャーなどで別途インストールしてください。リポジトリには特定OS向けのCAVA実行ファイルを含めていません。

```sh
python3 spectrum.py --sensitivity 140 --max-height 85
python3 spectrum.py --lower-cutoff 80 --higher-cutoff 12000
python3 spectrum.py --framerate 144
```

スペクトラム以外のグラデーションは明るい順に ANSI 255, 254, 240, 153, 147, 117, 111, 105 です。

## キー操作

| キー | 操作 |
| --- | --- |
| `1`–`9`, `a`–`e` | 表示モードを選択 |
| `←` / `→` | 前後のモードへ |
| `q` | 終了 |

## 表示モード

1. **Spectrum** — 左右チャンネル別の周波数スペクトラム
2. **Waveform** — 時間軸の音声波形
3. **Aurora** — 周波数に反応する光の帯
4. **Orbit** — 脈動する軌道と粒子
5. **Geometry** — 音量で変形する幾何学模様
6. **Particles** — 音に反応する粒子
7. **Spectrogram** — 周波数の時間変化
8. **Vortex** — 周波数でうねる螺旋
9. **Kaleidoscope** — 音に反応する万華鏡状の放射模様
a. **Lissajous** — 位相差で描くベクトル波形
b. **Ripple** — ビートで広がる波紋
c. **Mirror** — 中心から左右対称に伸びるスペクトラム
d. **Tunnel** — 回転しながら迫る幾何学トンネル
e. **Spectral Rain** — 周波数の粒が流れるレイン表示

256色と Unicode Braille に対応したターミナルを使ってください。`ffmpeg`、`ffplay`、`parec` は利用する入力方法に応じて別途インストールします。

画面上端には現在の表示モードと、右側に `OUTPUT` および現在の既定出力デバイス名を表示します。背景色は付けず、ステータスラインの文字色は ANSI 233 に固定しています。

## インストール

ソースから実行する場合は追加インストール不要です。任意の場所から `spectrum` コマンドとして使いたい場合は、プロジェクトのルートで次を実行します。

```sh
python3 -m pip install .
```

開発中にソースを編集しながら使う場合は `python3 -m pip install -e .` を使えます。
